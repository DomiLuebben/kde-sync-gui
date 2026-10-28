#!/bin/bash

# ==============================================================================
# KDE Plasma Backup & Restore Script
#
# - Sichert Konfigurationen, Themes, Icons, Kvantum-, GTK- und Latte-Dock-Daten
#   sowie die tatsächlich aktiven Hintergrundbilder (Desktop + Sperrbildschirm),
#   auch wenn diese außerhalb der Standardordner liegen.
# - Erkennt beim Restore automatisch die Distribution/den Paketmanager
#   (pacman, apt, dnf, zypper, sowie apk/xbps/emerge als Basis-Fallback) und
#   installiert fehlende Abhängigkeiten (Kvantum-Engine, Latte Dock sowie
#   bekannte Icon-/GTK-Theme-Pakete) automatisch.
# ==============================================================================

BACKUP_NAME="kde_plasma_backup_$(date +%Y%m%d_%H%M%S).tar.gz"
MANIFEST_FILE="$HOME/.kde_sync_manifest"

# Pfade (relativ zu $HOME, Glob-Muster), die nie ins Archiv gehören und beim
# Einspielen auch aus älteren Archiven übersprungen werden:
# - kdeconnect: privater Schlüssel + Zertifikat, auf zwei Rechnern gleiche Geräte-ID
# - Monitoranordnung, Energieprofile, Indexer- und Akonadi-Konten sind rechnergebunden
# - der eigene Zustand der GUI (Zeitstempel, Automatik-Schalter, Vergleichsstand)
# Zusätzliche Muster, eines pro Zeile, stehen in ~/.config/kde-sync-gui/exclude.list
EXCLUDE_PATTERNS=(
    ".config/kdeconnect" ".config/kdeconnect/*"
    ".config/kwinoutputconfig.json"
    ".config/powerdevilrc" ".config/powermanagementprofilesrc"
    ".config/baloofilerc" ".config/baloofileinformationrc"
    ".config/akonadi*"
    ".config/kde-sync-gui" ".config/kde-sync-gui/*"
)

load_exclude_patterns() {
    local extra="$HOME/.config/kde-sync-gui/exclude.list" line
    [ -f "$extra" ] || return 0
    while IFS= read -r line; do
        line="${line%%#*}"
        line="${line#"${line%%[![:space:]]*}"}"
        line="${line%"${line##*[![:space:]]}"}"
        [ -n "$line" ] && EXCLUDE_PATTERNS+=("$line")
    done < "$extra"
}

is_excluded() {
    local rel="$1" pat
    for pat in "${EXCLUDE_PATTERNS[@]}"; do
        # shellcheck disable=SC2053
        [[ "$rel" == $pat ]] && return 0
    done
    return 1
}

show_help() {
    echo "Verwendung:"
    echo "  $0 backup                   - Erstellt ein Backup der aktuellen KDE-Einstellungen"
    echo "  $0 restore <datei>          - Stellt ein Backup aus der .tar.gz Datei wieder her"
    echo "  $0 fingerprint              - Berechnet den Konfigurations-Fingerabdruck (1 Zeile SHA-256)"
    echo "  $0 prune <ordner> <anzahl>  - Rotiert alte Backups und behält die <anzahl> neuesten"
}

# ------------------------------------------------------------------------
# Kleine INI-Hilfsfunktion: liest einen Key aus einer bestimmten Sektion,
# unabhängig von der Reihenfolge der Keys in der Datei.
# ------------------------------------------------------------------------
get_ini_value() {
    local file="$1" section="$2" key="$3"
    [ -f "$file" ] || return
    awk -F'=' -v section="[$section]" -v key="$key" '
        $0 == section { insec=1; next }
        /^\[/ { insec=0 }
        insec && $1 == key { print substr($0, index($0, "=") + 1); exit }
    ' "$file" 2>/dev/null
}

# ------------------------------------------------------------------------
# Paketmanager-Erkennung (funktioniert unabhängig von /etc/os-release,
# da rein anhand der verfügbaren Tools entschieden wird)
# ------------------------------------------------------------------------
detect_pkg_manager() {
    if command -v pacman &>/dev/null; then PKG_MANAGER="pacman"
    elif command -v zypper &>/dev/null; then PKG_MANAGER="zypper"
    elif command -v dnf &>/dev/null; then PKG_MANAGER="dnf"
    elif command -v apt-get &>/dev/null; then PKG_MANAGER="apt"
    elif command -v apk &>/dev/null; then PKG_MANAGER="apk"
    elif command -v xbps-install &>/dev/null; then PKG_MANAGER="xbps"
    elif command -v emerge &>/dev/null; then PKG_MANAGER="emerge"
    else PKG_MANAGER="unknown"
    fi
    echo "🔍 Erkannter Paketmanager: $PKG_MANAGER"
}

# Logische Abhängigkeit -> Paketname(n) je Paketmanager.
# apk/xbps/emerge sind bewusst nur mit den wichtigsten Basis-Paketen befüllt.
declare -A PKG_pacman=(
    [kvantum]="kvantum"
    [lattedock]="latte-dock"
    [papirus]="papirus-icon-theme"
    [numix-icons]="numix-icon-theme"
    [numix-gtk]="numix-gtk-theme"
    [arc]="arc-gtk-theme"
    [breeze-gtk]="breeze-gtk"
    [tela]="tela-icon-theme"
)
declare -A PKG_apt=(
    [kvantum]="qt5-style-kvantum qt5-style-kvantum-themes qt6-style-kvantum"
    [lattedock]="latte-dock"
    [papirus]="papirus-icon-theme"
    [numix-icons]="numix-icon-theme"
    [numix-gtk]="numix-gtk-theme"
    [arc]="arc-theme"
    [breeze-gtk]="breeze-gtk-theme"
)
declare -A PKG_dnf=(
    [kvantum]="kvantum kvantum-qt5"
    [lattedock]="latte-dock"
    [papirus]="papirus-icon-theme"
    [numix-icons]="numix-icon-theme-circle"
    [arc]="arc-theme"
    [breeze-gtk]="breeze-gtk"
)
declare -A PKG_zypper=(
    [kvantum]="kvantum-manager kvantum-qt5"
    [lattedock]="latte-dock"
    [papirus]="papirus-icon-theme"
    [arc]="arc-theme"
    [breeze-gtk]="breeze-gtk"
)
declare -A PKG_apk=(
    [kvantum]="kvantum"
    [lattedock]="latte-dock"
)
declare -A PKG_xbps=(
    [kvantum]="kvantum"
    [lattedock]="latte-dock"
)
declare -A PKG_emerge=(
    [kvantum]="kde-apps/kvantum"
    [lattedock]="kde-apps/latte-dock"
)

pkg_install() {
    local key="$1"

    # Beim stillen Auto-Einspielen darf kein Passwortdialog aus dem Nichts erscheinen.
    if [ "${KDE_SYNC_NO_SUDO:-0}" = "1" ]; then
        echo "ℹ️  Automatisches Einspielen ohne Rückfrage: '$key' wird nicht installiert - bitte bei Bedarf manuell nachholen."
        return 1
    fi

    if [ "$PKG_MANAGER" = "unknown" ]; then
        echo "❌ Unbekannter Paketmanager - bitte '$key' manuell installieren."
        return 1
    fi

    local -n arr="PKG_${PKG_MANAGER}"
    local pkgs="${arr[$key]:-}"

    if [ -z "$pkgs" ]; then
        echo "ℹ️  Kein Paket für '$key' auf '$PKG_MANAGER' hinterlegt - bitte ggf. manuell installieren."
        return 1
    fi

    echo "📦 Installiere ($PKG_MANAGER): $pkgs"
    case "$PKG_MANAGER" in
        pacman) sudo pacman -S --needed --noconfirm $pkgs ;;
        apt)    sudo apt-get update -qq && sudo apt-get install -y $pkgs ;;
        dnf)    sudo dnf install -y $pkgs ;;
        zypper) sudo zypper --non-interactive install $pkgs ;;
        apk)    sudo apk add $pkgs ;;
        xbps)   sudo xbps-install -y $pkgs ;;
        emerge) sudo emerge --ask=n $pkgs ;;
    esac
}

# Versucht, ein referenziertes Icon-/GTK-Theme über den Paketmanager
# nachzuinstallieren - aber nur, wenn es nicht bereits lokal vorhanden ist
# (z.B. weil es gerade aus dem Backup wiederhergestellt wurde). Themes von
# store.kde.org/GitHub, die keine Distro-Pakete sind, werden übersprungen -
# für die reicht die Wiederherstellung der mitgesicherten Theme-Dateien.
try_theme_pkg() {
    local name="$1"
    [ -z "$name" ] && return

    if [ -d "$HOME/.local/share/icons/$name" ] || [ -d "$HOME/.icons/$name" ] || \
       [ -d "/usr/share/icons/$name" ] || [ -d "/usr/share/themes/$name" ]; then
        return
    fi

    local key=""
    case "$(echo "$name" | tr '[:upper:]' '[:lower:]')" in
        *papirus*)               key="papirus" ;;
        *numix*icon*)            key="numix-icons" ;;
        *numix*)                 key="numix-gtk" ;;
        arc|arc-*|arc_*|*-arc|*_arc|*-arc-*) key="arc" ;;
        *breeze-gtk*|*breeze_gtk*) key="breeze-gtk" ;;
        *tela*)                  key="tela" ;;
    esac

    if [ -n "$key" ]; then
        echo "🔍 Theme '$name' nicht lokal gefunden, versuche Installation über Paketmanager..."
        pkg_install "$key"
    else
        echo "ℹ️  Theme '$name' ist kein bekanntes Distro-Paket - es wurde (falls in .local/share bzw. .icons enthalten) bereits aus dem Backup wiederhergestellt. Andernfalls bitte manuell von store.kde.org/GitHub laden."
    fi
}

# Liest das im Backup enthaltene Manifest (aktive Themes zum Zeitpunkt des
# Backups) und installiert daraus abgeleitete fehlende Abhängigkeiten.
install_dependencies() {
    detect_pkg_manager

    if [ ! -f "$MANIFEST_FILE" ]; then
        echo "⚠️  Kein Theme-Manifest im Backup gefunden - installiere nur die Kvantum-Basis."
        command -v kvantummanager &>/dev/null || pkg_install kvantum
        return
    fi

    local icon_theme gtk_theme gtk_icon_theme cursor_theme kvantum_theme widget_style uses_latte
    icon_theme=$(grep '^ICON_THEME=' "$MANIFEST_FILE" | cut -d= -f2-)
    gtk_theme=$(grep '^GTK_THEME=' "$MANIFEST_FILE" | cut -d= -f2-)
    gtk_icon_theme=$(grep '^GTK_ICON_THEME=' "$MANIFEST_FILE" | cut -d= -f2-)
    cursor_theme=$(grep '^CURSOR_THEME=' "$MANIFEST_FILE" | cut -d= -f2-)
    kvantum_theme=$(grep '^KVANTUM_THEME=' "$MANIFEST_FILE" | cut -d= -f2-)
    widget_style=$(grep '^WIDGET_STYLE=' "$MANIFEST_FILE" | cut -d= -f2-)
    uses_latte=$(grep '^USES_LATTE=' "$MANIFEST_FILE" | cut -d= -f2-)

    echo "📋 Erkannte Konfiguration: Icon-Theme=$icon_theme | GTK-Theme=$gtk_theme | Kvantum-Theme=$kvantum_theme | Cursor=$cursor_theme"

    if [ -n "$kvantum_theme" ] || [[ "$widget_style" == *[Kk]vantum* ]]; then
        if command -v kvantummanager &>/dev/null; then
            echo "✅ Kvantum-Engine ist bereits installiert."
        else
            pkg_install kvantum
        fi
    fi

    if [ "$uses_latte" = "1" ] && ! command -v latte-dock &>/dev/null; then
        pkg_install lattedock
    fi

    try_theme_pkg "$icon_theme"
    try_theme_pkg "$gtk_icon_theme"
    try_theme_pkg "$gtk_theme"
}

# Dekodiert file://-URIs (z.B. %20 -> Leerzeichen). Nur gültige %XX-Folgen werden
# ersetzt; Backslashes und ein einzelnes % bleiben unverändert.
decode_uri() {
    local rest="$1" out="" ch
    while [[ "$rest" =~ ^([^%]*)%([0-9A-Fa-f]{2})(.*)$ ]]; do
        out+="${BASH_REMATCH[1]}"
        printf -v ch "\\x${BASH_REMATCH[2]}"
        out+="$ch"
        rest="${BASH_REMATCH[3]}"
    done
    printf '%s' "$out$rest"
}

# Ermittelt die aktuell gesetzten Hintergrundbilder (Desktop-Wallpaper und
# Sperrbildschirm) aus den Plasma-Configs und nimmt sie unabhängig von ihrem
# Speicherort (z.B. ~/Bilder, ~/Nextcloud/...) in die Backup-Liste auf.
collect_wallpapers() {
    local file_list="$1"
    local quiet="${2:-false}"
    [ "$quiet" = "true" ] || echo "🖼  Suche aktive Hintergrundbilder..."

    local configs=(
        "$HOME/.config/plasma-org.kde.plasma.desktop-appletsrc"
        "$HOME/.config/kscreenlockerrc"
    )
    local cfg raw path

    for cfg in "${configs[@]}"; do
        [ -f "$cfg" ] || continue
        while IFS= read -r raw; do
            path="$raw"
            [[ "$path" == file://* ]] && path="$(decode_uri "${path#file://}")"
            if [[ "$path" == "$HOME"/* && -f "$path" ]]; then
                echo "$path" >> "$file_list"
            fi
        done < <(grep -oE '^(Image|PreviewImage)=.*' "$cfg" | cut -d= -f2-)
    done
}

# Erfasst die aktuell aktiven Themes/Einstellungen in einem Manifest, damit
# restore() weiß, welche Abhängigkeiten installiert werden müssen.
generate_manifest() {
    local target="${1:-$MANIFEST_FILE}"
    local quiet="${2:-false}"
    [ "$quiet" = "true" ] || echo "🔎 Erfasse aktive Themes/Einstellungen für den Abhängigkeits-Check..."

    local icon_theme gtk_theme gtk_icon_theme cursor_theme kvantum_theme color_scheme widget_style uses_latte=0
    local wallpaper lock_wallpaper plasma_theme decoration

    icon_theme=$(get_ini_value "$HOME/.config/kdeglobals" "Icons" "Theme")
    widget_style=$(get_ini_value "$HOME/.config/kdeglobals" "KDE" "widgetStyle")
    color_scheme=$(get_ini_value "$HOME/.config/kdeglobals" "General" "ColorScheme")

    gtk_theme=$(get_ini_value "$HOME/.config/gtk-3.0/settings.ini" "Settings" "gtk-theme-name")
    gtk_icon_theme=$(get_ini_value "$HOME/.config/gtk-3.0/settings.ini" "Settings" "gtk-icon-theme-name")
    cursor_theme=$(get_ini_value "$HOME/.config/gtk-3.0/settings.ini" "Settings" "gtk-cursor-theme-name")
    [ -z "$cursor_theme" ] && cursor_theme=$(get_ini_value "$HOME/.config/kcminputrc" "Mouse" "cursorTheme")

    kvantum_theme=$(get_ini_value "$HOME/.config/Kvantum/kvantum.kvconfig" "General" "theme")

    if [ -d "$HOME/.config/latte" ] || command -v latte-dock &>/dev/null; then
        uses_latte=1
    fi

    # Zusätzliche Werte rein zur Beschreibung der Änderung in der GUI
    # ("Du hast das Hintergrundbild geändert"). install_dependencies liest sie nicht.
    wallpaper=$(get_ini_value "$HOME/.config/plasma-org.kde.plasma.desktop-appletsrc" "Wallpaper" "Image")
    [ -z "$wallpaper" ] && wallpaper=$(grep -m1 -oE '^Image=.*' "$HOME/.config/plasma-org.kde.plasma.desktop-appletsrc" 2>/dev/null | cut -d= -f2-)
    lock_wallpaper=$(grep -m1 -oE '^Image=.*' "$HOME/.config/kscreenlockerrc" 2>/dev/null | cut -d= -f2-)
    plasma_theme=$(get_ini_value "$HOME/.config/plasmarc" "Theme" "name")
    decoration=$(get_ini_value "$HOME/.config/kwinrc" "org.kde.kdecoration2" "library")

    cat > "$target" <<EOF
ICON_THEME=$icon_theme
GTK_ICON_THEME=$gtk_icon_theme
GTK_THEME=$gtk_theme
CURSOR_THEME=$cursor_theme
KVANTUM_THEME=$kvantum_theme
COLOR_SCHEME=$color_scheme
WIDGET_STYLE=$widget_style
USES_LATTE=$uses_latte
WALLPAPER=$wallpaper
LOCK_WALLPAPER=$lock_wallpaper
PLASMA_THEME=$plasma_theme
DECORATION=$decoration
EOF
}

# Gemeinsame Sammellogik (von backup und fingerprint genutzt)
collect_backup_set() {
    local file_list="$1"
    local manifest_path="${2:-$MANIFEST_FILE}"
    local quiet="${3:-false}"

    [ "$quiet" = "true" ] || echo "::PROGRESS::1::4::Plasma Desktop::Sammle KDE-Konfigurationen, Widgets und Panels..."
    [ "$quiet" = "true" ] || echo "Sammle KDE-Konfigurationen, Themes und Daten..."

    # 1. Basis-Konfigurationsdateien
    find "$HOME/.config" -maxdepth 1 -name "plasma*" >> "$file_list"
    find "$HOME/.config" -maxdepth 1 -name "kde*" >> "$file_list"
    find "$HOME/.config" -maxdepth 1 -name "kwin*" >> "$file_list"
    find "$HOME/.config" -maxdepth 1 -name "*rc" >> "$file_list"

    # 2. Spezifische Theme-/Config-Ordner
    for confdir in Kvantum gtk-3.0 gtk-4.0 xsettingsd fontconfig latte; do
        if [ -d "$HOME/.config/$confdir" ]; then
            echo "$HOME/.config/$confdir" >> "$file_list"
        fi
    done
    [ -f "$HOME/.gtkrc-2.0" ] && echo "$HOME/.gtkrc-2.0" >> "$file_list"

    # 3. Lokale Daten und Theme-Dateien
    for dir in plasma wallpapers aurorae color-schemes icons fonts konsole kxmlgui5 kservices5 Kvantum latte; do
        if [ -d "$HOME/.local/share/$dir" ]; then
            echo "$HOME/.local/share/$dir" >> "$file_list"
        fi
    done
    [ -d "$HOME/.icons" ] && echo "$HOME/.icons" >> "$file_list"

    # Ausschlussliste (siehe EXCLUDE_PATTERNS). Für den eigenen Zustand der GUI gilt:
    # 1. Nach jedem Snapshot schreibt die GUI hier ihren Zeitstempel hinein — läge der
    #    Ordner im Fingerabdruck, löste jeder Snapshot den nächsten aus.
    # 2. Beim Einspielen auf einem zweiten Rechner würde er dort backup_dir und die
    #    Automatik-Schalter mit den Werten des Erstrechners überschreiben.
    load_exclude_patterns
    local entry
    : > "$file_list.tmp"
    while IFS= read -r entry; do
        is_excluded "${entry#"$HOME"/}" || printf '%s\n' "$entry" >> "$file_list.tmp"
    done < "$file_list"
    mv "$file_list.tmp" "$file_list"

    # 4. Individuell gesetzte Hintergrundbilder (Desktop + Sperrbildschirm),
    #    auch außerhalb der Standardordner (z.B. ~/Bilder, ~/Nextcloud/...)
    [ "$quiet" = "true" ] || echo "::PROGRESS::2::4::Hintergrundbild::Suche aktive Desktop- und Sperrbildschirm-Hintergründe..."
    collect_wallpapers "$file_list" "$quiet"

    # 5. Manifest der aktiven Themes für den Abhängigkeits-Check beim Restore
    [ "$quiet" = "true" ] || echo "::PROGRESS::3::4::Themes & Design::Erfasse Farbschemata, Kvantum-, GTK- und Icon-Themes..."
    generate_manifest "$manifest_path" "$quiet"
    echo "$manifest_path" >> "$file_list"

    sort -u -o "$file_list" "$file_list"
}

# Sammellogik ausschließlich für den Plasma-Desktop (vom Fingerabdruck genutzt)
# Reagiert NUR auf:
# - Hintergrundbild (Desktop & Sperrbildschirm)
# - Widgets hinzugefügt/gelöscht/geändert & Widget-Einstellungen
# - Plasma-Panels & Bildschirmanordnung
# - Plasma-Desktop-Design, Farbschema, Icon-Design, Fensterdekoration
# Schließt alle Hintergrunddienste (Baloo, Akonadi, Dolphin-Verlauf, KActivityManagerd) aus!
collect_plasma_desktop_set() {
    local file_list="$1"
    local manifest_path="${2:-$MANIFEST_FILE}"
    local quiet="${3:-false}"

    # 1. Konfigurationen für Plasma Desktop, Widgets, Panels & Lockscreen
    local configs=(
        "$HOME/.config/plasma-org.kde.plasma.desktop-appletsrc"
        "$HOME/.config/plasmashellrc"
        "$HOME/.config/plasmarc"
        "$HOME/.config/kscreenlockerrc"
    )

    for cfg in "${configs[@]}"; do
        if [ -f "$cfg" ]; then
            echo "$cfg" >> "$file_list"
        fi
    done

    # 2. Individuell gesetzte Hintergrundbilder (Desktop + Sperrbildschirm)
    collect_wallpapers "$file_list" "$quiet"

    # 3. Lokale Plasma-Widgets (Plasmoids) und Plasma-Desktop-Themes
    for dir in plasma/plasmoids plasma/desktoptheme wallpapers; do
        if [ -d "$HOME/.local/share/$dir" ]; then
            echo "$HOME/.local/share/$dir" >> "$file_list"
        fi
    done

    # 4. Manifest der aktiven Plasma-/Desktop-Designs (sauber extrahierte Werte,
    #    ignoriert MRU-Listen und Verläufe in kdeglobals)
    generate_manifest "$manifest_path" "$quiet"
    echo "$manifest_path" >> "$file_list"

    sort -u -o "$file_list" "$file_list"
}

backup() {
    FILE_LIST=$(mktemp)
    local rc
    ERR_LOG=$(mktemp)
    # Das Archiv entsteht unter einem versteckten Teildatei-Namen und bekommt seinen
    # endgültigen Namen erst, wenn tar fehlerfrei durchlief und das Archiv lesbar ist.
    # Sonst lädt der Cloud-Client halbfertige Dateien hoch, und ein fehlgeschlagenes
    # Backup würde als Erfolg gemeldet und die Rotation räumte gute Snapshots ab.
    PART_FILE=".${BACKUP_NAME}.part"
    trap 'rm -f "$FILE_LIST" "$ERR_LOG" "$PART_FILE" "$MANIFEST_FILE"' EXIT
    trap 'exit 143' TERM INT

    # Liegengebliebene Teildateien früherer, abgebrochener Läufe aufräumen
    find . -maxdepth 1 -name '.kde_plasma_backup_*.tar.gz.part' -mmin +1440 -delete 2>/dev/null

    collect_backup_set "$FILE_LIST" "$MANIFEST_FILE" false

    # Pfade relativ machen
    sed -i "s|^$HOME/||" "$FILE_LIST"

    echo "::PROGRESS::4::4::Archivierung::Komprimiere Backup und speichere im Sync-Ordner..."
    echo "Erstelle Archiv: $BACKUP_NAME ..."
    # --ignore-failed-read: Dateien, die zwischen Sammeln und Packen verschwinden
    # (Temp-Dateien), sind nur Warnungen. Exit 1 = "Datei änderte sich beim Lesen".
    tar -czf "$PART_FILE" --ignore-failed-read -C "$HOME" -T "$FILE_LIST" 2>"$ERR_LOG"
    rc=$?

    if [ "$rc" -ge 2 ] || [ ! -s "$PART_FILE" ] || ! tar -tzf "$PART_FILE" >/dev/null 2>&1; then
        echo "❌ Backup fehlgeschlagen (tar Exit $rc). Letzte Meldungen:"
        tail -n 8 "$ERR_LOG"
        exit 1
    fi
    if [ "$rc" -eq 1 ]; then
        echo "⚠️  tar meldete Warnungen (Dateien änderten sich während der Sicherung):"
        tail -n 5 "$ERR_LOG"
    fi

    if ! mv -f "$PART_FILE" "$BACKUP_NAME"; then
        echo "❌ Backup fehlgeschlagen: Archiv konnte nicht unter dem Endnamen abgelegt werden."
        exit 1
    fi
    echo "::PROGRESS::DONE::Backup erfolgreich erstellt!::Datei: $(pwd)/$BACKUP_NAME"
    echo "✅ Backup erfolgreich erstellt! Datei: $(pwd)/$BACKUP_NAME"
}

# Normalisiert volatile / flüchtige KDE-Einträge für den Fingerabdruck.
# Filtert flüchtige Dialog- und Popup-Größen, Geometrie-Caches, System-Tray-Item-Listen
# und Verläufe heraus, damit normale Desktop-Nutzung (Klicks, geöffnete Menüs,
# gestartete Hintergrund-Apps mit Tray-Icon) keine Fehlalarme auslösen.
hash_file_normalized() {
    local file="$1"
    local base
    base=$(basename "$file")
    case "$base" in
        plasma-org.kde.plasma.desktop-appletsrc|plasmashellrc|plasmarc|kscreenlockerrc)
            awk -v ignore_dock="${KDE_SYNC_IGNORE_DOCK:-0}" '
            BEGIN { in_volatile_section = 0 }
            /^\[/ {
                if ($0 ~ /^\[ScreenMapping/ || $0 ~ /^\[PIMEventsPlugin/ || $0 ~ /ConfigDialog\]/) {
                    in_volatile_section = 1
                } else {
                    in_volatile_section = 0
                }
            }
            in_volatile_section { next }
            /^(DialogHeight|DialogWidth|popupHeight|popupWidth|currentDetailsTab|lastScreen|ItemGeometries.*|knownItems|hiddenItems|shownItems|calendars|performed|usersWallpapers)=/ { next }
            ignore_dock == "1" && /^(launchers|appGroups|folderUrls)=/ { next }
            NF == 0 { next }
            { print }
            ' "$file" 2>/dev/null | sha256sum | cut -d' ' -f1
            ;;
        *)
            sha256sum "$file" 2>/dev/null | cut -d' ' -f1
            ;;
    esac
}

# Signatur der installierten Plasmoids, Desktop-Themes und Wallpapers: je Top-Level-Eintrag
# "<pfad> <signatur>". Die Signatur hängt nur von Dateinamen, Größen und (bei Ordnern)
# metadata.json ab, nicht von Zeitstempeln. Eine Neuinstallation ohne Inhaltsänderung
# (kpackagetool, install.sh) ändert den Fingerabdruck also nicht.
b_entries_signature() {
    local base="$1" e sig m
    [ -d "$base" ] || return 0
    while IFS= read -r -d '' e; do
        if [ -d "$e" ]; then
            sig=$( {
                (cd "$e" && find . -type f -printf '%P\t%s\n' 2>/dev/null | LC_ALL=C sort)
                for m in metadata.json metadata.desktop; do
                    [ -f "$e/$m" ] && sha256sum < "$e/$m"
                done
            } | sha256sum | cut -c1-16)
        else
            sig=$(sha256sum < "$e" 2>/dev/null | cut -c1-16)
        fi
        printf '%s %s\n' "$e" "$sig"
    done < <(find "$base" -mindepth 1 -maxdepth 1 -print0 2>/dev/null)
}

# fingerprint [zustandsdatei]
#   Gibt immer genau eine Zeile aus: den Gesamt-Hash des Plasma-Desktops.
#   Ist ein Pfad angegeben, wird dort zusätzlich der Einzelzustand abgelegt
#   (je Datei eine Zeile). Daraus baut die GUI den Satz "Du hast XY geändert".
fingerprint() {
    local state_out="$1"
    local manifest_tmp file_list ebene_a_files ebene_b_out
    manifest_tmp=$(mktemp)
    file_list=$(mktemp)
    ebene_a_files=$(mktemp)
    ebene_b_out=$(mktemp)

    trap 'rm -f "$manifest_tmp" "$file_list" "$ebene_a_files" "$ebene_b_out"' EXIT

    collect_plasma_desktop_set "$file_list" "$manifest_tmp" true

    local ebene_b_pattern="^$HOME/\.local/share/(plasma/plasmoids|plasma/desktoptheme|wallpapers)($|/)"
    local entry

    while IFS= read -r entry; do
        [ -e "$entry" ] || [ -L "$entry" ] || continue
        if [[ "$entry" =~ $ebene_b_pattern ]]; then
            # Ebene B: je installiertem Eintrag eine Inhaltssignatur (siehe b_entries_signature)
            b_entries_signature "$entry" >> "$ebene_b_out"
        else
            # Ebene A: Manifest deterministisch behandeln, sonstige reguläre Dateien
            if [ "$entry" = "$manifest_tmp" ]; then
                continue
            elif [ -d "$entry" ]; then
                find "$entry" -type f -print0 2>/dev/null >> "$ebene_a_files"
            elif [ -f "$entry" ]; then
                printf '%s\0' "$entry" >> "$ebene_a_files"
            fi
        fi
    done < "$file_list"

    # Ebene A hashen:
    # 1. Alle regulären Dateien sortiert und normalisiert hashen
    # 2. Manifest mit deterministischem virtuellem Pfad einberechnen
    local hash_a
    hash_a=$( (
        while IFS= read -r -d '' file; do
            [ -f "$file" ] || continue
            h=$(hash_file_normalized "$file")
            printf "%s  %s\n" "$h" "$file"
        done < <(LC_ALL=C sort -z "$ebene_a_files")
        if [ -f "$manifest_tmp" ]; then
            printf "%s  %s/.kde_sync_manifest\n" "$(sha256sum "$manifest_tmp" | cut -d' ' -f1)" "$HOME"
        fi
    ) | sha256sum | cut -d' ' -f1 )

    # Ebene B hashen: Top-Level mtimes sortiert
    local hash_b
    hash_b=$(LC_ALL=C sort "$ebene_b_out" | sha256sum | cut -d' ' -f1)

    # Finaler Hash über Ebene A + Ebene B
    local final_hash
    final_hash=$(printf "%s\n%s\n" "$hash_a" "$hash_b" | sha256sum | cut -d' ' -f1)

    # Einzelzustand für die Klartext-Beschreibung, falls angefordert
    if [ -n "$state_out" ]; then
        {
            printf "#HASH\t%s\n" "$final_hash"
            # Semantische Werte (Hintergrundbild, Themes, ...) als S-Zeilen
            while IFS= read -r line; do
                [ -n "$line" ] && printf "S\t%s\n" "$line"
            done < "$manifest_tmp"
            # Ebene A: eine Zeile je Datei mit normalisiertem Hash
            while IFS= read -r -d '' file; do
                [ -f "$file" ] || continue
                h=$(hash_file_normalized "$file")
                printf "A\t%s\t%s\n" "$h" "$file"
            done < <(LC_ALL=C sort -z "$ebene_a_files")
            # Ebene B: Top-Level-Einträge, Rohzeile "<pfad> <mtime>"
            # (nicht selbst zerlegen — Theme-Ordner können Leerzeichen enthalten)
            LC_ALL=C sort "$ebene_b_out" | while IFS= read -r line; do
                printf "B\t%s\n" "$line"
            done
        } > "$state_out" 2>/dev/null
    fi

    rm -f "$manifest_tmp" "$file_list" "$ebene_a_files" "$ebene_b_out"
    trap - EXIT

    printf "%s\n" "$final_hash"
}

prune() {
    local dir="$1"
    local keep="$2"

    [ -d "$dir" ] || return 0
    [[ "$keep" =~ ^[0-9]+$ ]] || return 0
    [ "$keep" -gt 0 ] || return 0

    # Nur echte Snapshots zählen. Das lose Muster kde_plasma_backup_*.tar.gz
    # würde auch Konfliktkopien des Cloud-Clients ("… (conflicted copy …).tar.gz")
    # mitzählen — die belegten dann einen der behaltenen Plätze.
    local files=()
    while IFS= read -r f; do
        [ -n "$f" ] && files+=("$f")
    done < <(find "$dir" -maxdepth 1 -regextype posix-extended \
                  -regex '.*/kde_plasma_backup_[0-9]{8}_[0-9]{6}\.tar\.gz' \
                  -printf "%f\n" | LC_ALL=C sort)

    local total="${#files[@]}"
    if [ "$total" -gt "$keep" ]; then
        local to_delete=$((total - keep))
        for ((i=0; i<to_delete; i++)); do
            rm -f "$dir/${files[i]}"
        done
    fi
}

stop_plasmashell() {
    systemctl --user stop plasma-plasmashell.service 2>/dev/null && return 0
    if command -v kquitapp6 &>/dev/null; then
        kquitapp6 plasmashell 2>/dev/null && { sleep 1; return 0; }
    fi
    killall plasmashell 2>/dev/null
    return 0
}

start_plasmashell() {
    systemctl --user start plasma-plasmashell.service 2>/dev/null || { kstart plasmashell >/dev/null 2>&1 & }
}

restore() {
    if [ -z "$1" ]; then
        echo "❌ Fehler: Bitte gib die Backup-Datei an."
        show_help
        exit 1
    fi

    BACKUP_FILE="$1"

    if [ ! -f "$BACKUP_FILE" ]; then
        echo "❌ Fehler: Datei '$BACKUP_FILE' nicht gefunden!"
        exit 1
    fi

    echo "::PROGRESS::1::4::Vorbereitung::Prüfe Archiv, lese Theme-Manifest und System-Abhängigkeiten..."
    # 0. Archiv zuerst prüfen. Ein beschädigtes Archiv darf weder Dateien anfassen
    #    noch die Plasma-Oberfläche neu starten.
    local listing
    listing=$(mktemp)
    if ! tar -tzf "$BACKUP_FILE" > "$listing" 2>/dev/null; then
        rm -f "$listing"
        echo "❌ Das Archiv ist beschädigt oder kein gültiges Backup. Es wurde nichts verändert."
        exit 1
    fi

    # 1. Manifest vorab extrahieren und Abhängigkeiten prüfen/installieren
    tar -xzf "$BACKUP_FILE" -C "$HOME" ".kde_sync_manifest" 2>/dev/null
    install_dependencies

    # Ausschlussliste auch beim Einspielen anwenden, damit ältere Archive (z. B. mit
    # KDE-Connect-Schlüsseln) auf diesem Rechner nichts Rechnergebundenes überschreiben.
    load_exclude_patterns
    local tar_excludes=() pat
    for pat in "${EXCLUDE_PATTERNS[@]}"; do
        tar_excludes+=("--exclude=$pat")
    done

    echo "::PROGRESS::2::4::Dateien einspielen::Entpacke Konfigurationen, Widgets und Themes..."
    echo "Stelle Backup aus '$BACKUP_FILE' her..."

    # 2. plasmashell vor dem Entpacken beenden: Es schreibt seine Konfiguration beim
    #    Beenden zurück und würde sonst das frisch eingespielte Layout überschreiben.
    stop_plasmashell

    local extract_rc
    tar -xzf "$BACKUP_FILE" -C "$HOME" "${tar_excludes[@]}"
    extract_rc=$?

    rm -f "$MANIFEST_FILE"

    if [ "$extract_rc" -ne 0 ]; then
        rm -f "$listing"
        echo "❌ Entpacken fehlgeschlagen (tar Exit $extract_rc). Plasma wird mit dem bisherigen Stand wieder gestartet."
        start_plasmashell
        exit 1
    fi

    echo "::PROGRESS::3::4::Hintergrundbild & Schriften::Richte Hintergrundbild ein und aktualisiere Font-Cache..."
    # 3. Font-Cache nur aktualisieren, wenn das Archiv Schriften enthielt
    if grep -q '^\.local/share/fonts/' "$listing" && command -v fc-cache &>/dev/null; then
        fc-cache -f &>/dev/null
    fi
    rm -f "$listing"

    echo "✅ Backup erfolgreich entpackt!"
    echo "::PROGRESS::4::4::Plasma Desktop::Starte Plasma Desktop neu und wende Layouts an..."
    echo "🔄 Starte KDE Plasma neu, um Theme, Wallpaper und Layouts anzuwenden..."

    # 4. KDE wieder starten
    start_plasmashell
    qdbus org.kde.KWin /KWin reconfigure 2>/dev/null || qdbus6 org.kde.KWin /KWin reconfigure 2>/dev/null

    echo "::PROGRESS::DONE::Wiederherstellung erfolgreich abgeschlossen!::Plasma Desktop & Layouts geladen."
    echo "========================================================================="
    echo "🎉 Fertig! Alle Einstellungen, Plasmoids, Wallpaper und Themes wurden geladen."
    echo "WICHTIG: Bitte melde dich einmal vom System ab (Logout) und wieder an,"
    echo "damit die Kvantum-Engine und die GTK-Themes vollständig greifen!"
    echo "========================================================================="
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    case "$1" in
        backup)
            backup
            ;;
        restore)
            restore "$2"
            ;;
        fingerprint)
            fingerprint "$2"
            ;;
        prune)
            prune "$2" "$3"
            ;;
        *)
            show_help
            exit 1
            ;;
    esac
fi

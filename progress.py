"""Decode stable backend progress events into GUI presentation data."""

# Keep identifiers in sync with kde_sync.sh; presentation text belongs in the GUI.
PROGRESS_EVENTS = {
    "backup.desktop": (1, "🖥️", "Plasma Desktop",
                       "Sammle KDE-Konfigurationen, Widgets und Panels..."),
    "backup.wallpapers": (2, "🖼️", "Hintergrundbild",
                          "Suche aktive Desktop- und Sperrbildschirm-Hintergründe..."),
    "backup.themes": (3, "🎨", "Themes & Design",
                     "Erfasse Farbschemata, Kvantum-, GTK- und Icon-Themes..."),
    "backup.archive": (4, "📦", "Archivierung",
                      "Komprimiere Backup und speichere im Sync-Ordner..."),
    "restore.prepare": (1, "🔍", "Vorbereitung",
                       "Prüfe Archiv, lese Theme-Manifest und System-Abhängigkeiten..."),
    "restore.extract": (2, "📦", "Dateien einspielen",
                       "Entpacke Konfigurationen, Widgets und Themes..."),
    "restore.wallpapers": (3, "🖼️", "Hintergrundbild & Schriften",
                          "Richte Hintergrundbild ein und aktualisiere Font-Cache..."),
    "restore.desktop": (4, "🖥️", "Plasma Desktop",
                       "Starte Plasma Desktop neu und wende Layouts an..."),
    "backup.done": (None, "✅", "Backup erfolgreich erstellt!",
                    "Datei: {path}"),
    "restore.done": (None, "✅", "Wiederherstellung erfolgreich abgeschlossen!",
                     "Plasma Desktop & Layouts geladen."),
}


def decode_progress(line):
    """Return (step, total, icon, title, detail), or None for unrecognized lines.

    V2 uses ::PROGRESS_V2::<event>[::<path>]. Only backup.done has a payload;
    partition preserves spaces and any further '::' in the archive path.
    """
    prefix = "::PROGRESS_V2::"
    if not line.startswith(prefix):
        return None
    event, separator, payload = line[len(prefix):].partition("::")
    if event not in PROGRESS_EVENTS:
        return None
    if event == "backup.done":
        if not separator or not payload:
            return None
    elif separator:
        return None
    step, icon, title, detail = PROGRESS_EVENTS[event]
    if event == "backup.done":
        detail = detail.format(path=payload)
    return step, 4 if step is not None else None, icon, title, detail

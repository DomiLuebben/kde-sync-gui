"""Translate stable backend progress events without localizing the wire format."""

from PyQt6.QtCore import QCoreApplication, QT_TRANSLATE_NOOP


# Keep identifiers in sync with kde_sync.sh. Text is translated at display time,
# not import time, because the application installs its translator in main().
PROGRESS_EVENTS = {
    "backup.desktop": (1, "🖥️", QT_TRANSLATE_NOOP("Progress", "Plasma Desktop"),
                       QT_TRANSLATE_NOOP("Progress", "Sammle KDE-Konfigurationen, Widgets und Panels...")),
    "backup.wallpapers": (2, "🖼️", QT_TRANSLATE_NOOP("Progress", "Hintergrundbild"),
                          QT_TRANSLATE_NOOP("Progress", "Suche aktive Desktop- und Sperrbildschirm-Hintergründe...")),
    "backup.themes": (3, "🎨", QT_TRANSLATE_NOOP("Progress", "Themes & Design"),
                     QT_TRANSLATE_NOOP("Progress", "Erfasse Farbschemata, Kvantum-, GTK- und Icon-Themes...")),
    "backup.archive": (4, "📦", QT_TRANSLATE_NOOP("Progress", "Archivierung"),
                      QT_TRANSLATE_NOOP("Progress", "Komprimiere Backup und speichere im Sync-Ordner...")),
    "restore.prepare": (1, "🔍", QT_TRANSLATE_NOOP("Progress", "Vorbereitung"),
                       QT_TRANSLATE_NOOP("Progress", "Prüfe Archiv, lese Theme-Manifest und System-Abhängigkeiten...")),
    "restore.extract": (2, "📦", QT_TRANSLATE_NOOP("Progress", "Dateien einspielen"),
                       QT_TRANSLATE_NOOP("Progress", "Entpacke Konfigurationen, Widgets und Themes...")),
    "restore.wallpapers": (3, "🖼️", QT_TRANSLATE_NOOP("Progress", "Hintergrundbild & Schriften"),
                          QT_TRANSLATE_NOOP("Progress", "Richte Hintergrundbild ein und aktualisiere Font-Cache...")),
    "restore.desktop": (4, "🖥️", QT_TRANSLATE_NOOP("Progress", "Plasma Desktop"),
                       QT_TRANSLATE_NOOP("Progress", "Starte Plasma Desktop neu und wende Layouts an...")),
    "backup.done": (None, "✅", QT_TRANSLATE_NOOP("Progress", "Backup erfolgreich erstellt!"),
                    QT_TRANSLATE_NOOP("Progress", "Datei: {path}")),
    "restore.done": (None, "✅", QT_TRANSLATE_NOOP("Progress", "Wiederherstellung erfolgreich abgeschlossen!"),
                     QT_TRANSLATE_NOOP("Progress", "Plasma Desktop & Layouts geladen.")),
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
    title = QCoreApplication.translate("Progress", title)
    detail = QCoreApplication.translate("Progress", detail)
    if event == "backup.done":
        detail = detail.format(path=payload)
    return step, 4 if step is not None else None, icon, title, detail

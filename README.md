# KDE Plasma Sync GUI

PyQt6 tool that backs up and syncs KDE Plasma settings (panels, widgets, wallpapers, themes) through any
cloud-synced folder (Nextcloud, Syncthing, OneDrive, ...). It can create snapshots automatically when the
Plasma desktop changes and apply snapshots from another machine.

- `main.py`: GUI, tray icon, change detection
- `kde_sync.sh`: backup, restore, fingerprint, rotation
- `tests/`: unit tests (`QT_QPA_PLATFORM=offscreen python3 -m unittest discover tests`); they run in a temporary HOME
- `PKGBUILD`: Arch package (`makepkg -f`)

Files that never go into a snapshot (KDE Connect keys, monitor layout, power profiles, Baloo, Akonadi, the tool's
own state) are listed in `EXCLUDE_PATTERNS` in `kde_sync.sh`. Add more, one pattern per line, in
`~/.config/kde-sync-gui/exclude.list`.

## Interface language

German and English are available under **Settings → Appearance → Language**
(**Einstellungen → Darstellung → Sprache**). Save your choice, quit through the tray
menu, and reopen the application to apply it.

For running from source or contributing translations, see [TRANSLATING.md](TRANSLATING.md).

See `REVIEW_2026-10-28.md` for the review behind version 1.4.0.

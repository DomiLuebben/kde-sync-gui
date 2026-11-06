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

The interface supports German and English. Under **Settings → Appearance → Language**
(German: **Einstellungen → Darstellung → Sprache**), choose the system language, German, or English, then save
and restart the application. Use **Quit / Beenden** in the tray menu to exit completely;
closing the window normally leaves the application running in the tray.

The default follows Qt's ordered system UI language preferences. If none of them is
supported, English is used. The preference is stored as `language` (`system`, `de`, or
`en`) in `~/.config/kde-sync-gui/config.json`; existing configurations gain `system`
automatically. German is the source language and the fallback if the English catalog
is missing or cannot be loaded (a warning is printed).

The GUI, notifications, change summaries, and structured backup/restore progress are
translated. Raw `kde_sync.sh` diagnostic logs and command-line help remain German;
messages from external commands use those commands' own locale settings. Backup
formats, configuration keys, filenames, and fingerprints are not translated.

### Running from a checkout

Install Python/PyQt6 and Qt 6 Linguist tools. On Arch Linux these are provided by
`python-pyqt6` and `qt6-tools`; install `qt6-translations` for Qt's standard German
dialogs and buttons. Compile the English catalog before starting:

```sh
lrelease6 kde_sync_en.ts -qm kde_sync_en.qm
python3 main.py
```

Some distributions call the compiler `lrelease` instead of `lrelease6`; use the Qt 6
version. `makepkg -f` compiles and installs the catalog automatically. Generated `.qm`
files are not committed.

## Contributing translations

See [TRANSLATING.md](TRANSLATING.md) for marking strings, updating catalogs, adding
languages, and testing translations.

See `REVIEW_2026-10-28.md` for the review behind version 1.4.0.

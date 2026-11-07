# Contributing translations

The application uses Qt's translation system. German is the source language; English
translations are maintained in `kde_sync_en.ts`. Catalogs live alongside the Python
modules, matching the flat local-source layout used by `PKGBUILD`.

## Tools

Install Python/PyQt6 and Qt 6 Linguist tools. On Arch Linux these are provided by
`python-pyqt6` and `qt6-tools`; install `qt6-translations` for Qt's standard German
dialogs and buttons. Some distributions call the compiler `lrelease` instead of
`lrelease6`; use the Qt 6 version. Run the commands below from the repository root.

## Marking strings

German source text is marked with `self.tr()` in widgets, or
`QCoreApplication.translate(context, source)` in helpers. Module-level text uses
`QT_TRANSLATE_NOOP` and is translated when displayed, after startup. Translate
complete templates before inserting values with `.format(...)`; do not put f-strings
inside translation calls. Keep placeholders such as `{filename}` and `%n` unchanged,
and provide all plural forms for numerus messages.

## Updating the English catalog

After changing UI text, update the catalog:

```sh
pylupdate6 main.py progress.py --ts kde_sync_en.ts
```

Open the `.ts` file in Qt Linguist (or edit its XML) and complete the translations.
Compile the catalog before starting the application:

```sh
lrelease6 kde_sync_en.ts -qm kde_sync_en.qm
python3 main.py
```

Choose a language under **Settings → Appearance → Language**, save, and restart the
application. Use **Quit / Beenden** in the tray menu to exit completely; closing the
window normally leaves the application running in the tray.

Include source and `.ts` changes together in the contribution. Generated `.qm` files
are not committed. `makepkg -f` compiles and installs the catalog automatically.

## Testing

Run the tests before submitting:

```sh
QT_QPA_PLATFORM=offscreen python3 -m unittest discover tests
```

Tests use temporary homes and compile a temporary English catalog. English runtime
tests are skipped if `lrelease6`/`lrelease` is unavailable, so install the tools to
validate a translation contribution fully. Catalog tests check coverage, placeholders,
and plural forms; GUI tests cover both languages and restart-only language changes.

## Adding another language

Create `kde_sync_<code>.ts`, add its code to `i18n.SUPPORTED_LANGUAGES` and the settings
selector in `main.py`, and update `PKGBUILD` to compile and install its catalog. Set
the catalog's target language and provide its required plural forms. German currently
needs no application catalog because it is the source language.

## Backend output

The GUI, notifications, change summaries, and structured backup/restore progress are
translated. Raw `kde_sync.sh` diagnostic logs and command-line help remain German;
messages from external commands use those commands' own locale settings. Backup
formats, configuration keys, filenames, and fingerprints must not be translated.

Backend progress events use language-independent `::PROGRESS_V2::<event>[::<path>]`
records. Keep event IDs in `kde_sync.sh` and `progress.py` in sync; only the Python
presentation strings are translated. The GUI still accepts the legacy progress format.

"""Load Qt translation catalogs before constructing any widgets."""

import logging
from pathlib import Path

from PyQt6.QtCore import QLibraryInfo, QLocale, QTranslator


TRANSLATIONS_DIR = Path(__file__).resolve().parent
SUPPORTED_LANGUAGES = ("de", "en")


def resolve_language(language="system"):
    """Use Qt's ordered UI language preferences, falling back to English."""
    if language in SUPPORTED_LANGUAGES:
        return language
    for preference in QLocale.system().uiLanguages():
        code = preference.replace("_", "-").split("-", 1)[0].lower()
        if code in SUPPORTED_LANGUAGES:
            return code
    return "en"


def install_translations(app, language="system", translations_dir=None):
    """Install application and Qt catalogs; return the effective UI language.

    German is the source language and needs no application catalog. A missing
    English catalog falls back to German rather than preventing startup. Keep
    Python references on the application so translators live through app.exec().
    Call this before constructing widgets; live language switching is not used.
    """
    for translator in getattr(app, "_kde_sync_translators", []):
        app.removeTranslator(translator)
        translator.deleteLater()
    app._kde_sync_translators = []

    language = resolve_language(language)
    directory = Path(translations_dir) if translations_dir is not None else TRANSLATIONS_DIR
    if language != "de":
        translator = QTranslator(app)
        catalog = directory / f"kde_sync_{language}.qm"
        if translator.load(str(catalog)):
            app.installTranslator(translator)
            app._kde_sync_translators.append(translator)
        else:
            logging.getLogger(__name__).warning(
                "Cannot load translation catalog %s; using German. "
                "Compile the catalogs with Qt's lrelease before starting the application.",
                catalog,
            )
            translator.deleteLater()
            language = "de"

    # Qt's built-in strings are English. Its German catalog translates standard
    # dialog buttons and file dialogs, including when overriding an English OS.
    if language != "en":
        translator = QTranslator(app)
        directory = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
        if translator.load(f"qtbase_{language}", directory):
            app.installTranslator(translator)
            app._kde_sync_translators.append(translator)
        else:
            logging.getLogger(__name__).warning(
                "Cannot load Qt's %s translation catalog; standard dialogs may use English.",
                language,
            )
            translator.deleteLater()

    return language

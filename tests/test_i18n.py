#!/usr/bin/env python3
"""Translation contracts; English integration tests compile their own Qt catalog."""

import ast
import atexit
from collections import Counter
import gc
import os
from pathlib import Path
import re
import shutil
from string import Formatter
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import weakref
import xml.etree.ElementTree as ET

os.environ["QT_QPA_PLATFORM"] = "offscreen"
PROJECT_DIR = Path(__file__).resolve().parents[1]
CATALOG = PROJECT_DIR / "kde_sync_en.ts"
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

# main/config_manager capture HOME at import time. Restore the previous environment
# afterwards, particularly when test_gui has already installed its own global HOME.
_IMPORT_HOME = tempfile.mkdtemp(prefix="kde-sync-i18n-home-")
atexit.register(shutil.rmtree, _IMPORT_HOME, True)
with patch.dict(os.environ, {
    "HOME": _IMPORT_HOME,
    "XDG_CONFIG_HOME": str(Path(_IMPORT_HOME) / ".config"),
    "XDG_DATA_HOME": str(Path(_IMPORT_HOME) / ".local" / "share"),
}):
    import config_manager
    import main

from PyQt6.QtCore import QCoreApplication, QEvent
from PyQt6.QtWidgets import QApplication, QLabel
import i18n
import progress


def source_messages(path):
    """Extract literal Qt translation calls without requiring lupdate at test time."""
    messages = set()

    class Visitor(ast.NodeVisitor):
        context = None

        def visit_ClassDef(self, node):
            previous = self.context
            self.context = node.name
            self.generic_visit(node)
            self.context = previous

        def visit_Call(self, node):
            func = node.func
            args = node.args
            context = source = None
            numerus = False
            if (isinstance(func, ast.Attribute) and func.attr == "tr"
                    and isinstance(func.value, ast.Name) and func.value.id == "self"
                    and args):
                context, source = self.context, args[0]
                numerus = len(args) >= 3
            elif ((isinstance(func, ast.Attribute) and func.attr == "translate")
                  or (isinstance(func, ast.Name) and func.id == "QT_TRANSLATE_NOOP")):
                if len(args) >= 2 and isinstance(args[0], ast.Constant):
                    context, source = args[0].value, args[1]
                    numerus = len(args) >= 4
            if context and isinstance(source, ast.Constant) and isinstance(source.value, str):
                numerus = numerus or any(kw.arg == "n" for kw in node.keywords)
                messages.add((context, source.value, numerus))
            self.generic_visit(node)

    Visitor().visit(ast.parse(path.read_text(encoding="utf-8")))
    return messages


def placeholders(text):
    fields = Counter(
        (field, spec, conversion)
        for _, field, spec, conversion in Formatter().parse(text)
        if field is not None
    )
    qt_fields = Counter(re.findall(r"%(?:L?n|L?[1-9][0-9]*)", text))
    return fields, qt_fields


class TestSourceCatalog(unittest.TestCase):
    def setUp(self):
        self.catalog = ET.parse(CATALOG).getroot()

    def test_all_marked_source_messages_have_english_translations(self):
        self.assertEqual(self.catalog.get("language"), "en")
        self.assertEqual(self.catalog.get("sourcelanguage"), "de")
        catalog_messages = {
            (context.findtext("name"), message.findtext("source"),
             message.get("numerus") == "yes")
            for context in self.catalog.findall("context")
            for message in context.findall("message")
        }
        expected = source_messages(PROJECT_DIR / "main.py") | source_messages(PROJECT_DIR / "progress.py")
        self.assertTrue(expected)
        self.assertTrue(any(context == "Progress" for context, _, _ in expected))
        self.assertFalse(expected - catalog_messages,
                         f"Missing catalog messages: {sorted(expected - catalog_messages)!r}")

    def test_translations_are_finished_and_preserve_placeholders(self):
        seen = set()
        for context in self.catalog.findall("context"):
            for message in context.findall("message"):
                source = message.findtext("source")
                key = (context.findtext("name"), source)
                with self.subTest(context=key[0], source=source):
                    self.assertNotIn(key, seen, "Duplicate catalog entry")
                    seen.add(key)
                    translation = message.find("translation")
                    self.assertIsNotNone(translation)
                    self.assertNotIn(translation.get("type"),
                                     ("unfinished", "vanished", "obsolete"))
                    forms = translation.findall("numerusform")
                    if message.get("numerus") == "yes":
                        self.assertEqual(len(forms), 2, "English needs singular and plural forms")
                    else:
                        self.assertFalse(forms)
                        self.assertNotIn("%n", source)
                        forms = [translation]
                    for form in forms:
                        text = "".join(form.itertext())
                        self.assertTrue(text.strip(), "Empty English translation")
                        self.assertEqual(placeholders(source), placeholders(text))


class TestLanguageResolution(unittest.TestCase):
    def test_system_uses_first_supported_qt_ui_preference(self):
        cases = [
            (["fr-FR", "de-DE", "en-US"], "de"),
            (["en-GB", "de-DE"], "en"),
            (["de_AT", "en-US"], "de"),
            (["fr", "EN_us", "de"], "en"),
            (["zh-Hant-TW", "DE-de"], "de"),
            (["fr-FR", "ja-JP"], "en"),
            (["C"], "en"),
            ([], "en"),
        ]
        for preferences, expected in cases:
            with self.subTest(preferences=preferences), patch.object(i18n, "QLocale") as locale:
                locale.system.return_value.uiLanguages.return_value = preferences
                self.assertEqual(i18n.resolve_language("system"), expected)
                locale.system.return_value.uiLanguages.assert_called_once_with()

    def test_explicit_supported_language_overrides_system(self):
        with patch.object(i18n, "QLocale") as locale:
            for language in ("de", "en"):
                self.assertEqual(i18n.resolve_language(language), language)
            locale.system.assert_not_called()

    def test_invalid_setting_falls_back_to_system_then_english(self):
        for preferences, expected in [(["de-DE"], "de"), (["fr-FR"], "en")]:
            for setting in (None, "", "fr", "en-US", "invalid", 42):
                with self.subTest(setting=setting, preferences=preferences), patch.object(i18n, "QLocale") as locale:
                    locale.system.return_value.uiLanguages.return_value = preferences
                    self.assertEqual(i18n.resolve_language(setting), expected)


class QtTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.app = QApplication.instance() or QApplication(["test-i18n"])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="kde-sync-i18n-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.backup_dir = self.root / "backups with spaces"
        self.backup_dir.mkdir()
        env = patch.dict(os.environ, {
            "HOME": str(self.root),
            "XDG_CONFIG_HOME": str(self.root / ".config"),
            "XDG_DATA_HOME": str(self.root / ".local" / "share"),
        })
        env.start()
        self.addCleanup(env.stop)
        for module, name, value in [
            (config_manager, "CONFIG_DIR", str(self.root / "config")),
            (config_manager, "CONFIG_FILE", str(self.root / "config" / "config.json")),
            (main, "STATE_FILE", str(self.root / "state.fp")),
            (main, "STATE_FILE_NEW", str(self.root / "state.fp.new")),
            (main, "STATE_FILE_BASE_NEW", str(self.root / "state.fp.base.new")),
            (main, "CHANGES_LOG", str(self.root / "changes.log")),
        ]:
            patcher = patch.object(module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        # QApplication is shared with test_gui. Do not leave English installed for
        # its German assertions, nor discard translators installed by other callers.
        self.previous_translators = getattr(self.app, "_kde_sync_translators", [])
        for translator in self.previous_translators:
            self.app.removeTranslator(translator)
        self.app._kde_sync_translators = []
        self.addCleanup(self.restore_translators)

    def restore_translators(self):
        for translator in self.app._kde_sync_translators:
            self.app.removeTranslator(translator)
            translator.deleteLater()
        self.app._kde_sync_translators = self.previous_translators
        for translator in self.previous_translators:
            self.app.installTranslator(translator)

    def make_gui(self, language="system"):
        config = config_manager.load_config()
        config.update(backup_dir=str(self.backup_dir), language=language,
                      autostart_enabled=False, periodic_check_enabled=False,
                      auto_snapshot_enabled=False, fingerprint_version=main.FINGERPRINT_VERSION)
        config_manager.save_config(config)
        for target, name in [
            (main.QTimer, "singleShot"),
            (main, "update_autostart_file"),
            (main.KdeSyncGUI, "init_tray_icon"),
            (main.KdeSyncGUI, "setup_fs_watcher"),
            (main.KdeSyncGUI, "setup_config_watcher"),
            (main.KdeSyncGUI, "update_fingerprint_baseline_silently"),
        ]:
            patcher = patch.object(target, name)
            patcher.start()
            self.addCleanup(patcher.stop)
        gui = main.KdeSyncGUI(autostart_check=False)
        gui.tray_icon = Mock()
        gui.tray_icon.isVisible.return_value = False

        def close_gui():
            gui.close()
            gui.deleteLater()
            QCoreApplication.sendPostedEvents(gui, QEvent.Type.DeferredDelete)

        self.addCleanup(close_gui)
        return gui

    def assert_progress_rendered(self, language):
        gui = self.make_gui(language)
        gui.init_operation_ui("Test backup", main.BACKUP_STEPS)
        gui.handle_script_output("::PROGRESS_V2::backup.wallpapers\n")
        expected = {
            "de": ("Schritt 2 von 4", "Hintergrundbild", "Fertig", "Datei: ",
                   "Backup erfolgreich erstellt!"),
            "en": ("Step 2 of 4", "Wallpaper", "Done", "File: ",
                   "Backup created successfully!"),
        }[language]
        badge, phase, done, path_prefix, done_title = expected
        self.assertEqual(gui.lbl_op_step_badge.text(), badge)
        self.assertEqual(gui.lbl_phase_title.text(), phase)
        self.assertEqual(gui.lbl_phase_icon.text(), "🖼️")
        self.assertIn(phase, gui.step_pills[1].text())
        self.assertTrue(gui.step_pills[0].text().startswith("✓ "))
        self.assertGreater(gui.op_progress_bar.value(), 10)
        path = str(self.backup_dir / "snapshot :: with {braces}.tar.gz")
        gui.handle_script_output(f"::PROGRESS_V2::backup.done::{path}\n")
        self.assertEqual(gui.lbl_op_step_badge.text(), done)
        self.assertEqual(gui.lbl_phase_title.text(), done_title)
        self.assertEqual(gui.lbl_phase_detail.text(), path_prefix + path)
        self.assertEqual(gui.op_progress_bar.value(), 100)
        self.assertFalse(gui.btn_close_card.isHidden())
        self.assertTrue(all(pill.text().startswith("✓ ") for pill in gui.step_pills))
        log = gui.inline_log_output.toPlainText()
        self.assertIn(path_prefix + path, log)
        self.assertNotIn("::PROGRESS_V2::", log)

    def change_fixture(self):
        wallpaper = self.backup_dir / "wallpaper.jpg"
        plasma = self.root / "plasmarc"
        applets = self.root / "plasma-org.kde.plasma.desktop-appletsrc"
        old = self.root / "old.fp"
        new = self.root / "new.fp"
        old.write_text("S\tWALLPAPER=/old.jpg\nS\tPLASMA_THEME=old\n", encoding="utf-8")
        new.write_text(
            f"S\tWALLPAPER={wallpaper}\nS\tPLASMA_THEME=new\n"
            f"A\tnew\t{wallpaper}\nA\tnew\t{plasma}\nA\tnew\t{applets}\n",
            encoding="utf-8",
        )
        return old, new


class TestGermanAndFallback(QtTestCase):
    def test_missing_english_qm_falls_back_to_german_with_actionable_warning(self):
        with self.assertLogs("i18n", level="WARNING") as logs:
            effective = i18n.install_translations(self.app, "en", self.root)
        self.assertEqual(effective, "de")
        warning = "\n".join(logs.output)
        self.assertIn(str(self.root / "kde_sync_en.qm"), warning)
        self.assertIn("German", warning)
        self.assertIn("lrelease", warning)
        self.assertEqual(QCoreApplication.translate("KdeSyncGUI", "Einstellungen"), "Einstellungen")
        gui = self.make_gui("en")
        self.assertEqual(gui.tabs.tabText(2), "Einstellungen")
        self.assertEqual(gui.btn_backup.text(), "Jetzt sichern")
        self.assertEqual(gui.combo_language.parentWidget().title(), "Darstellung")
        self.assertEqual(gui.txt_backup_dir.parentWidget().title(), "Synchronisation")
        self.assertIsNot(gui.combo_language.parentWidget(), gui.txt_backup_dir.parentWidget())

    def test_german_progress_is_rendered_without_a_catalog(self):
        self.assert_progress_rendered("de")

    def test_all_stable_events_decode_without_localized_wire_text(self):
        for operation, events in [
            ("backup", ("desktop", "wallpapers", "themes", "archive")),
            ("restore", ("prepare", "extract", "wallpapers", "desktop")),
        ]:
            for step, event in enumerate(events, 1):
                with self.subTest(operation=operation, event=event):
                    decoded = progress.decode_progress(f"::PROGRESS_V2::{operation}.{event}")
                    self.assertIsNotNone(decoded)
                    self.assertEqual(decoded[:2], (step, 4))
                    self.assertTrue(all(decoded[2:]))
        self.assertEqual(progress.decode_progress("::PROGRESS_V2::backup.wallpapers"), (
            2, 4, "🖼️", "Hintergrundbild",
            "Suche aktive Desktop- und Sperrbildschirm-Hintergründe...",
        ))
        self.assertEqual(progress.decode_progress("::PROGRESS_V2::restore.done"), (
            None, None, "✅", "Wiederherstellung erfolgreich abgeschlossen!",
            "Plasma Desktop & Layouts geladen.",
        ))

    def test_unknown_and_malformed_progress_is_not_an_event(self):
        lines = [
            "", "ordinary backend output", "::PROGRESS_V2::", "::PROGRESS_V2::unknown",
            "::PROGRESS_V2::backup", "::PROGRESS_V2::BACKUP.desktop",
            "prefix ::PROGRESS_V2::backup.desktop", "::PROGRESS::2::4::legacy::detail",
            "::PROGRESS_V2::backup.done", "::PROGRESS_V2::backup.done::",
            "::PROGRESS_V2::backup.desktop::", "::PROGRESS_V2::restore.prepare::extra",
            "::PROGRESS_V2::restore.done::unexpected payload",
        ]
        for line in lines:
            with self.subTest(line=line):
                self.assertIsNone(progress.decode_progress(line))

    def test_german_change_summary_deduplicates_overlapping_evidence(self):
        old, new = self.change_fixture()
        names, details = main.diff_states(old, new)
        self.assertEqual(names, ["das Hintergrundbild", "das Plasma-Design"])
        self.assertEqual(len(details["files"]), 3)
        self.assertEqual(main.describe_changes(old, new), "das Hintergrundbild und das Plasma-Design")
        self.assertIsNone(main.format_change_list([]))


class TestEnglishTranslation(QtTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        lrelease = shutil.which("lrelease6") or shutil.which("lrelease")
        if lrelease is None:
            raise unittest.SkipTest("Qt lrelease6/lrelease unavailable; see TRANSLATING.md for development dependencies")
        cls.catalog_dir = tempfile.TemporaryDirectory(prefix="kde-sync-test-qm-")
        cls.addClassCleanup(cls.catalog_dir.cleanup)
        result = subprocess.run(
            [lrelease, str(CATALOG), "-qm", str(Path(cls.catalog_dir.name) / "kde_sync_en.qm")],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode:
            raise AssertionError(f"lrelease failed:\n{result.stdout}\n{result.stderr}")

    def setUp(self):
        super().setUp()
        self.assertEqual(i18n.install_translations(self.app, "en", self.catalog_dir.name), "en")

    def test_translator_survives_gc_and_can_be_removed_and_reinstalled(self):
        refs = [weakref.ref(translator) for translator in self.app._kde_sync_translators]
        self.assertTrue(refs)
        gc.collect()
        self.assertTrue(all(ref() is not None for ref in refs))
        self.assertEqual(QCoreApplication.translate("KdeSyncGUI", "Einstellungen"), "Settings")
        self.assertEqual(i18n.install_translations(self.app, "de", self.catalog_dir.name), "de")
        self.assertEqual(QCoreApplication.translate("KdeSyncGUI", "Einstellungen"), "Einstellungen")
        self.assertEqual(i18n.install_translations(self.app, "en", self.catalog_dir.name), "en")
        gc.collect()
        self.assertEqual(QCoreApplication.translate("KdeSyncGUI", "Einstellungen"), "Settings")
        self.assertEqual(len(self.app._kde_sync_translators), 1)

    def test_english_widgets_and_language_choices(self):
        gui = self.make_gui("en")
        self.assertEqual([gui.tabs.tabText(i) for i in range(gui.tabs.count())],
                         ["Dashboard", "Backups", "Settings"])
        self.assertEqual(gui.btn_backup.text(), "Back up now")
        self.assertEqual(gui.btn_save_settings.text(), "Save")
        self.assertEqual(gui.status_bar_label.text(), "Ready")
        self.assertEqual(gui.btn_toggle_protocol.text(), "▼ Show log")
        self.assertEqual([gui.combo_language.itemData(i) for i in range(gui.combo_language.count())],
                         ["system", "de", "en"])
        self.assertEqual(gui.combo_language.currentData(), "en")
        self.assertEqual(gui.combo_language.parentWidget().title(), "Appearance")
        self.assertEqual(gui.txt_backup_dir.parentWidget().title(), "Synchronization")
        self.assertIsNot(gui.combo_language.parentWidget(), gui.txt_backup_dir.parentWidget())
        labels = [label.text() for label in gui.findChildren(QLabel)]
        self.assertIn("The language will change after restarting the application.", labels)

    def test_settings_persist_language_without_live_switching(self):
        gui = self.make_gui("en")
        translators = list(self.app._kde_sync_translators)
        with patch.object(main.QMessageBox, "information") as information:
            for language in ("de", "system", "en"):
                with self.subTest(language=language):
                    gui.combo_language.setCurrentIndex(gui.combo_language.findData(language))
                    gui.btn_save_settings.click()
                    self.assertEqual(config_manager.load_config()["language"], language)
                    self.assertEqual(gui.tabs.tabText(2), "Settings")
                    self.assertEqual(gui.btn_save_settings.text(), "Save")
                    self.assertEqual(self.app._kde_sync_translators, translators)
                    self.assertEqual(QCoreApplication.translate("KdeSyncGUI", "Einstellungen"), "Settings")
            self.assertEqual(information.call_count, 3)

    def test_spinbox_suffixes_follow_the_current_count(self):
        gui = self.make_gui("en")
        for spin, singular, plural in [
            (gui.spin_interval, " hour", " hours"),
            (gui.spin_keep_snapshots, " snapshot", " snapshots"),
        ]:
            for value, suffix in [(1, singular), (2, plural), (1, singular)]:
                with self.subTest(suffix=suffix, value=value):
                    spin.setValue(value)
                    self.assertEqual(spin.suffix(), suffix)
        gui.spin_keep_snapshots.setValue(0)
        self.assertEqual(gui.spin_keep_snapshots.text(), "Unlimited (0)")
        # Seconds/minutes cannot reach one through these controls; test their
        # singular catalog forms directly, without changing production ranges.
        for source, singular, plural in [(" Sekunden", " second", " seconds"),
                                         (" Minuten", " minute", " minutes")]:
            self.assertEqual(gui.tr(source, None, 1), singular)
            self.assertEqual(gui.tr(source, None, 2), plural)
        gui.spin_quiet_seconds.setValue(20)
        gui.spin_min_interval.setValue(10)
        self.assertEqual(gui.spin_quiet_seconds.suffix(), " seconds")
        self.assertEqual(gui.spin_min_interval.suffix(), " minutes")

    def test_change_summaries_translate_after_deduplication_and_select_plurals(self):
        old, new = self.change_fixture()
        names, details = main.diff_states(old, new)
        self.assertEqual(names, ["das Hintergrundbild", "das Plasma-Design"])
        self.assertEqual(details["semantic"], ["WALLPAPER", "PLASMA_THEME"])
        self.assertEqual(main.describe_changes(old, new), "the wallpaper and the Plasma theme")
        names = ["das Hintergrundbild", "das Icon-Design", "das GTK-Design",
                 "den Mauszeiger", "das Farbschema"]
        self.assertIsNone(main.format_change_list([]))
        self.assertEqual(main.format_change_list(names[:1]), "the wallpaper")
        self.assertEqual(main.format_change_list(names[:3]),
                         "the wallpaper, the icon theme and the GTK theme")
        self.assertEqual(main.format_change_list(names[:4]),
                         "the wallpaper, the icon theme, the GTK theme and 1 other setting")
        self.assertEqual(main.format_change_list(names),
                         "the wallpaper, the icon theme, the GTK theme and 2 other settings")

    def test_english_progress_is_rendered_from_stable_events(self):
        self.assert_progress_rendered("en")
        decoded = progress.decode_progress("::PROGRESS_V2::restore.prepare")
        self.assertEqual(decoded[:4], (1, 4, "🔍", "Preparation"))
        self.assertNotIn("Prüfe", decoded[4])
        done = progress.decode_progress("::PROGRESS_V2::restore.done")
        self.assertEqual(done[:3], (None, None, "✅"))
        self.assertEqual(done[3], "Restore completed successfully!")

    def test_history_tab_refresh_uses_identity_not_translated_text(self):
        gui = self.make_gui("en")
        index = gui.tabs.indexOf(gui.history_tab)
        gui.tabs.setTabText(index, "Archives / Sicherungen")
        gui.tabs.setTabText(0, "Backups")
        with patch.object(gui, "refresh_backup_list") as refresh:
            gui.tabs.setCurrentWidget(gui.history_tab)
            refresh.assert_called_once_with()
            refresh.reset_mock()
            gui.tabs.setCurrentIndex(0)
            refresh.assert_not_called()
            gui.is_operation_running = True
            gui.tabs.setCurrentWidget(gui.history_tab)
            refresh.assert_not_called()


if __name__ == "__main__":
    unittest.main()

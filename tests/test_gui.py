#!/usr/bin/env python3
import atexit
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

# Headless Qt
os.environ["QT_QPA_PLATFORM"] = "offscreen"

# Die GUI schreibt beim Start in ~/.config (Autostart-Datei, Vergleichsstand, Änderungs-
# protokoll). Die Tests laufen deshalb in einem eigenen HOME, und zwar bevor main und
# config_manager importiert werden, denn beide leiten ihre Pfade beim Import aus HOME ab.
# PKGBUILD check() führt diese Tests bei jedem Paketbau aus; das Live-System bleibt unberührt.
_TEST_HOME = tempfile.mkdtemp(prefix="kde-sync-test-home-")
atexit.register(shutil.rmtree, _TEST_HOME, True)
os.environ["HOME"] = _TEST_HOME
os.environ["XDG_CONFIG_HOME"] = os.path.join(_TEST_HOME, ".config")
os.environ["XDG_DATA_HOME"] = os.path.join(_TEST_HOME, ".local", "share")

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

from PyQt6.QtWidgets import QApplication, QDialog, QMessageBox
from PyQt6.QtCore import Qt
import config_manager
import main


def write_state(path, fp, sem=None, files=None, dirs=None):
    """Schreibt eine Zustandsdatei im Format von 'kde_sync.sh fingerprint <datei>'."""
    lines = [f"#HASH\t{fp}"]
    for key, value in (sem or {}).items():
        lines.append(f"S\t{key}={value}")
    for p, h in (files or {}).items():
        lines.append(f"A\t{h}\t{p}")
    for p, sig in (dirs or {}).items():
        lines.append(f"B\t{p} {sig}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


class TestKdeSyncGUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["test"])

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.orig_config_dir = config_manager.CONFIG_DIR
        self.orig_config_file = config_manager.CONFIG_FILE
        self.orig_main_paths = (main.STATE_FILE, main.STATE_FILE_NEW, main.STATE_FILE_BASE_NEW, main.CHANGES_LOG)
        config_manager.CONFIG_DIR = self.tmpdir.name
        config_manager.CONFIG_FILE = os.path.join(self.tmpdir.name, "config.json")
        main.STATE_FILE = os.path.join(self.tmpdir.name, "state.fp")
        main.STATE_FILE_NEW = main.STATE_FILE + ".new"
        main.STATE_FILE_BASE_NEW = main.STATE_FILE + ".base.new"
        main.CHANGES_LOG = os.path.join(self.tmpdir.name, "changes.log")

        self.gui = main.KdeSyncGUI(autostart_check=False)
        self.gui.show()

    def tearDown(self):
        if self.gui.snapshot_dialog is not None and hasattr(self.gui.snapshot_dialog, "close"):
            self.gui.snapshot_dialog.close()
        self.gui.close()
        config_manager.CONFIG_DIR = self.orig_config_dir
        config_manager.CONFIG_FILE = self.orig_config_file
        main.STATE_FILE, main.STATE_FILE_NEW, main.STATE_FILE_BASE_NEW, main.CHANGES_LOG = self.orig_main_paths
        self.tmpdir.cleanup()

    def test_protocol_tab_is_completely_removed(self):
        """Prüft, dass der Tab 'Protokoll' vollständig aus der GUI entfernt wurde."""
        tab_count = self.gui.tabs.count()
        self.assertEqual(tab_count, 3, f"Erwarte genau 3 Tabs (Dashboard, Backups, Einstellungen), gefunden: {tab_count}")
        tab_titles = [self.gui.tabs.tabText(i) for i in range(tab_count)]
        self.assertEqual(tab_titles, ["Dashboard", "Backups", "Einstellungen"])
        self.assertNotIn("Protokoll", tab_titles)

    def test_btn_toggle_protocol(self):
        """Prüft, dass der Button 'Protokoll anzeigen' die Konsole ein- und ausklappt."""
        self.gui.operation_card.setVisible(True)
        btn = self.gui.btn_toggle_protocol
        console = self.gui.console_widget

        self.assertEqual(btn.text(), "▼ Protokoll anzeigen")
        self.assertFalse(console.isVisible())

        # Aufklappen
        self.gui.toggle_console()
        self.assertEqual(btn.text(), "▲ Protokoll ausblenden")
        self.assertTrue(console.isVisible())

        # Zuklappen
        self.gui.toggle_console()
        self.assertEqual(btn.text(), "▼ Protokoll anzeigen")
        self.assertFalse(console.isVisible())

    def test_graphical_progress_updates(self):
        """Prüft, dass eingehende ::PROGRESS::-Meldungen die grafische Oberfläche aktualisieren."""
        self.gui.init_operation_ui("Backup wird erstellt...", main.BACKUP_STEPS)
        self.assertTrue(self.gui.operation_card.isVisible())
        self.assertEqual(self.gui.lbl_op_title.text(), "Backup wird erstellt...")
        self.assertEqual(self.gui.lbl_op_step_badge.text(), "Schritt 1 von 4")
        self.assertIn("Plasma Desktop", self.gui.step_pills[0].text())

        # Schritt 2 simulieren
        self.gui.handle_script_output("::PROGRESS::2::4::Hintergrundbild::Suche aktive Desktop- und Sperrbildschirm-Hintergründe...\n")
        self.assertEqual(self.gui.lbl_op_step_badge.text(), "Schritt 2 von 4")
        self.assertEqual(self.gui.lbl_phase_title.text(), "Hintergrundbild")
        self.assertIn("Suche aktive Desktop", self.gui.lbl_phase_detail.text())
        self.assertTrue(self.gui.step_pills[0].text().startswith("✓ "))
        self.assertGreater(self.gui.op_progress_bar.value(), 20)

        # Schritt 4 simulieren
        self.gui.handle_script_output("::PROGRESS::4::4::Archivierung::Komprimiere Backup und speichere im Sync-Ordner...\n")
        self.assertEqual(self.gui.lbl_op_step_badge.text(), "Schritt 4 von 4")
        self.assertEqual(self.gui.lbl_phase_title.text(), "Archivierung")
        self.assertTrue(self.gui.step_pills[1].text().startswith("✓ "))

        # DONE simulieren
        self.gui.handle_script_output("::PROGRESS::DONE::Backup erfolgreich erstellt!::Datei: test.tar.gz\n")
        self.assertEqual(self.gui.lbl_op_step_badge.text(), "Fertig")
        self.assertEqual(self.gui.op_progress_bar.value(), 100)
        for i in range(4):
            self.assertTrue(self.gui.step_pills[i].text().startswith("✓ "))
        self.assertTrue(self.gui.btn_close_card.isVisible())

    def test_cooldown_prevents_reprompt_after_rejection(self):
        """Prüft, dass nach einer Nachfrage (Prompt-Cooldown) innerhalb des Mindestintervalls kein Runner startet."""
        self.gui.config["auto_snapshot_enabled"] = True
        self.gui.config["auto_snapshot_min_interval_minutes"] = 30
        # Letzter Prompt vor 5 Minuten
        recent_prompt = (datetime.now() - timedelta(minutes=5)).isoformat()
        self.gui.config["last_prompt_time"] = recent_prompt

        self.gui.check_auto_snapshot()
        self.assertIsNone(self.gui.fingerprint_runner, "Bei aktivem Prompt-Cooldown darf kein Fingerprint-Runner starten!")

    # ----------------- Isolation -----------------

    def test_tests_never_touch_the_real_home(self):
        real_home = os.path.expanduser("~")
        self.assertTrue(real_home.startswith(tempfile.gettempdir()), real_home)
        self.assertTrue(os.path.realpath(main.STATE_FILE).startswith(os.path.realpath(self.tmpdir.name)))

    # ----------------- Nachfrage-Logik -----------------

    def log_actions(self):
        import json
        if not os.path.exists(main.CHANGES_LOG):
            return []
        with open(main.CHANGES_LOG, encoding="utf-8") as f:
            return [json.loads(line)["action"] for line in f if line.strip()]

    def prepare_widget_change(self):
        """Alter Stand: appletsrc 'aa', neuer Stand: 'bb'."""
        rc = "/h/.config/plasma-org.kde.plasma.desktop-appletsrc"
        write_state(main.STATE_FILE, "old", files={rc: "aa"})
        write_state(main.STATE_FILE_NEW, "new", files={rc: "bb"})
        self.gui.config["last_fingerprint"] = "old"
        self.gui.config["auto_snapshot_ask_first"] = True

    def test_unnamed_hash_difference_is_adopted_silently(self):
        """Der Hash weicht ab, es lässt sich aber nichts Benennbares finden: nicht fragen."""
        write_state(main.STATE_FILE, "old", files={"/x/other.conf": "aa"})
        write_state(main.STATE_FILE_NEW, "new", files={"/x/other.conf": "bb"})
        self.gui.config["last_fingerprint"] = "old"
        self.gui.handle_auto_snapshot_fingerprint("new", 0)
        self.assertIsNone(self.gui.snapshot_dialog)
        self.assertEqual(self.gui.config["last_fingerprint"], "new")
        self.assertTrue(os.path.exists(main.STATE_FILE))
        self.assertFalse(os.path.exists(main.STATE_FILE_NEW))
        self.assertEqual(self.log_actions(), ["ignored_unnamed"])

    def test_named_change_opens_nonmodal_question_without_raising_window(self):
        self.gui.hide()
        self.prepare_widget_change()
        self.gui.handle_auto_snapshot_fingerprint("new", 0)
        dialog = self.gui.snapshot_dialog
        self.assertIsNotNone(dialog)
        self.assertEqual(dialog.windowModality(), Qt.WindowModality.NonModal)
        self.assertFalse(self.gui.isVisible(), "Das Hauptfenster darf nicht nach vorn geholt werden")
        self.assertIn("die Arbeitsflächen-Widgets", dialog.text())
        self.assertTrue(self.gui.config["last_prompt_time"])
        self.assertEqual(self.log_actions(), ["asked"])

    def test_declining_adopts_new_baseline(self):
        self.prepare_widget_change()
        self.gui.handle_auto_snapshot_fingerprint("new", 0)
        dialog = self.gui.snapshot_dialog
        next(b for b in dialog.buttons() if b.text() == "Ignorieren").click()
        self.assertIsNone(self.gui.snapshot_dialog)
        self.assertEqual(self.gui.config["last_fingerprint"], "new")
        self.assertEqual(self.log_actions(), ["asked", "declined"])

    def test_accepting_starts_silent_backup(self):
        self.prepare_widget_change()
        started = []
        self.gui.start_backup = lambda silent=False: started.append(silent)
        self.gui.handle_auto_snapshot_fingerprint("new", 0)
        self.gui.snapshot_ctx["yes_btn"].click()
        self.assertEqual(started, [True])
        self.assertEqual(self.gui.pending_fingerprint, "new")
        self.assertEqual(self.log_actions(), ["asked", "accepted"])

    def test_no_second_check_while_question_is_open(self):
        self.gui.config["auto_snapshot_enabled"] = True
        self.gui.config["last_prompt_time"] = ""
        self.gui.config["last_auto_snapshot"] = ""
        self.gui.snapshot_dialog = object()
        self.gui.check_auto_snapshot()
        self.assertIsNone(self.gui.fingerprint_runner)
        self.gui.snapshot_dialog = None

    def test_silent_mode_snapshots_named_changes_without_question(self):
        self.prepare_widget_change()
        self.gui.config["auto_snapshot_ask_first"] = False
        started = []
        self.gui.start_backup = lambda silent=False: started.append(silent)
        self.gui.handle_auto_snapshot_fingerprint("new", 0)
        self.assertIsNone(self.gui.snapshot_dialog)
        self.assertEqual(started, [True])

    def test_wallpaper_replaced_at_same_path_is_named(self):
        path = "/h/Bilder/w.jpg"
        write_state(main.STATE_FILE, "old", sem={"WALLPAPER": path}, files={path: "aa"})
        write_state(main.STATE_FILE_NEW, "new", sem={"WALLPAPER": path}, files={path: "bb"})
        self.assertEqual(main.describe_changes(main.STATE_FILE, main.STATE_FILE_NEW), "das Hintergrundbild")

    def test_installed_widget_change_is_named(self):
        d = "/h/.local/share/plasma/plasmoids/org.example.dock"
        write_state(main.STATE_FILE, "old", dirs={d: "1111"})
        write_state(main.STATE_FILE_NEW, "new", dirs={d: "2222"})
        self.assertEqual(main.describe_changes(main.STATE_FILE, main.STATE_FILE_NEW), "die installierten Widgets")

    def test_fingerprint_version_change_retakes_baseline_silently(self):
        self.gui.close()
        cfg = config_manager.load_config()
        cfg["auto_snapshot_enabled"] = True
        cfg["last_fingerprint"] = "alter-hash-anderes-verfahren"
        cfg["fingerprint_version"] = 1
        config_manager.save_config(cfg)
        started = []
        orig = main.KdeSyncGUI.update_fingerprint_baseline_silently
        main.KdeSyncGUI.update_fingerprint_baseline_silently = lambda self_: started.append(True)
        try:
            gui = main.KdeSyncGUI(autostart_check=False)
            self.assertEqual(gui.config["last_fingerprint"], "")
            self.assertEqual(gui.config["fingerprint_version"], main.FINGERPRINT_VERSION)
            self.assertTrue(started)
            gui.close()
        finally:
            main.KdeSyncGUI.update_fingerprint_baseline_silently = orig
        self.gui = main.KdeSyncGUI(autostart_check=False)

    # ----------------- Abbrechen und sudo -----------------

    def test_cancelling_the_sudo_dialog_stops_the_script(self):
        class Rejecting:
            def __init__(self, *_a, **_k):
                self.password_input = None

            def exec(self):
                return QDialog.DialogCode.Rejected

        class FakeRunner:
            stopped = False

            def stop(self):
                self.stopped = True

        orig = main.PasswordDialog
        main.PasswordDialog = Rejecting
        try:
            self.gui.runner = FakeRunner()
            self.gui.prompt_sudo_password()
            self.assertTrue(self.gui.runner.stopped)
        finally:
            main.PasswordDialog = orig

    def test_script_runner_passes_extra_environment(self):
        runner = main.ScriptRunner(["bash", "-c", "echo NOSUDO=$KDE_SYNC_NO_SUDO"], self.tmpdir.name,
                                   env={"KDE_SYNC_NO_SUDO": "1"})
        out = []
        runner.output_received.connect(out.append)
        runner.run()
        self.assertIn("NOSUDO=1", "".join(out))

    def test_script_runner_stop_ends_the_process(self):
        runner = main.ScriptRunner(["bash", "-c", "sleep 30"], self.tmpdir.name)
        codes = []
        runner.finished_status.connect(codes.append)
        runner.stop_requested = True
        runner.run()
        self.assertEqual(len(codes), 1)
        self.assertNotEqual(codes[0], 0)

    # ----------------- Autostart und Dashboard -----------------

    def test_autostart_file_is_idempotent_and_not_executable(self):
        path = os.path.join(os.path.expanduser("~"), ".config", "autostart", "kde-sync-gui.desktop")
        main.update_autostart_file(True)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o644)
        with open(path) as f:
            self.assertIn("--minimized", f.read())
        first = os.stat(path).st_mtime_ns
        main.update_autostart_file(True)
        self.assertEqual(os.stat(path).st_mtime_ns, first, "unveränderte Datei darf nicht neu geschrieben werden")
        os.chmod(path, 0o755)
        main.update_autostart_file(True)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o644)
        main.update_autostart_file(False)
        self.assertFalse(os.path.exists(path))

    def test_dashboard_does_not_create_missing_backup_dir(self):
        missing = os.path.join(self.tmpdir.name, "nicht-eingehaengt", "kde-backup")
        self.gui.config["backup_dir"] = missing
        self.gui.update_dashboard_ui()
        self.assertFalse(os.path.exists(missing))
        self.assertIn("existiert nicht", self.gui.lbl_status_val.text())


if __name__ == "__main__":
    unittest.main()

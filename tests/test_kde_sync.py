#!/usr/bin/env python3
import os
import stat
import subprocess
import tarfile
import tempfile
import unittest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT_PATH = os.path.join(PROJECT_DIR, "kde_sync.sh")

# Programme, die das Skript nicht wirklich ausführen darf: Sie landen als Attrappen vorn
# im PATH und schreiben nur ihren Aufruf in calls.log. Dazu gehören sudo (Paketinstallation)
# und alles, was die laufende Sitzung berührt (systemctl, qdbus, kstart, killall).
STUBBED = ["systemctl", "qdbus", "qdbus6", "kstart", "killall", "kquitapp6", "fc-cache", "sudo"]


class ScriptTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.home = os.path.join(self.tmp, "home")
        self.out = os.path.join(self.tmp, "out")
        self.bin = os.path.join(self.tmp, "bin")
        self.calls_log = os.path.join(self.tmp, "calls.log")
        for d in (os.path.join(self.home, ".config"), self.out, self.bin):
            os.makedirs(d)
        for name in STUBBED:
            path = os.path.join(self.bin, name)
            with open(path, "w") as f:
                f.write(f'#!/bin/sh\necho "{name} $*" >> "{self.calls_log}"\n')
            os.chmod(path, 0o755)

    def tearDown(self):
        # Schreibgeschützte Verzeichnisse aus den Tests wieder freigeben
        for root, dirs, _files in os.walk(self.tmp):
            for d in dirs:
                os.chmod(os.path.join(root, d), 0o755)
        self._tmp.cleanup()

    def write(self, rel, content="x\n"):
        path = os.path.join(self.home, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
        return path

    def run_script(self, *args, cwd=None, home=None, extra_env=None):
        env = dict(os.environ)
        env["HOME"] = home or self.home
        env["PATH"] = self.bin + os.pathsep + env["PATH"]
        env["KDE_SYNC_NO_SUDO"] = "1"
        env.pop("KDE_SYNC_IGNORE_DOCK", None)
        env.update(extra_env or {})
        return subprocess.run(
            [SCRIPT_PATH, *args], cwd=cwd or self.out, env=env,
            capture_output=True, text=True
        )

    def calls(self):
        if not os.path.exists(self.calls_log):
            return []
        with open(self.calls_log) as f:
            return f.read().splitlines()

    def archives(self, directory=None):
        directory = directory or self.out
        return sorted(f for f in os.listdir(directory) if f.startswith("kde_plasma_backup_"))

    def bash(self, snippet):
        cmd = f'source "{SCRIPT_PATH}"; {snippet}'
        return subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, check=True).stdout


class TestFingerprintNormalization(ScriptTestCase):
    def hash_of(self, content):
        path = os.path.join(self.tmp, "plasma-org.kde.plasma.desktop-appletsrc")
        with open(path, "w") as f:
            f.write(content)
        return self.bash(f'hash_file_normalized "{path}"').strip()

    def test_volatile_keys_do_not_change_hash(self):
        """Flüchtige UI-Änderungen (Größen, Tray-Listen, Geometrien) ändern den Hash nicht."""
        base = """[Containments][33][Applets][40]
plugin=org.kde.plasma.systemtray
formfactor=0

[Containments][33][Applets][57]
plugin=org.kde.plasma.digitalclock
customDateFormat=ddd dd.MM.yy
"""
        volatile = """[Containments][33][Applets][40]
plugin=org.kde.plasma.systemtray
formfactor=0
popupHeight=436
popupWidth=436
DialogHeight=630
DialogWidth=810
knownItems=app1,app2,app3,app4
hiddenItems=app1
shownItems=app2

[Containments][33][Applets][40][ConfigDialog]
DialogHeight=630
DialogWidth=810

[Containments][33][Applets][57]
plugin=org.kde.plasma.digitalclock
customDateFormat=ddd dd.MM.yy
currentDetailsTab=details
lastScreen=0

[ScreenMapping]
itemsOnDisabledScreens=0,desktop:/test.desktop
screenMapping=desktop:/test.desktop,0
"""
        self.assertEqual(self.hash_of(base), self.hash_of(volatile))

    def test_real_changes_are_detected(self):
        base = "[Containments][33][Applets][40]\nplugin=org.kde.plasma.systemtray\n"
        changed = base + "\n[Containments][33][Applets][99]\nplugin=org.kde.plasma.newapplet\n"
        self.assertNotEqual(self.hash_of(base), self.hash_of(changed))

    def test_dock_keys_only_ignored_when_requested(self):
        base = "[Containments][80][Applets][90][Configuration][General]\niconSize=46\nlaunchers=a,b\n"
        moved = "[Containments][80][Applets][90][Configuration][General]\niconSize=46\nlaunchers=a,b,c\nappGroups={}\nfolderUrls=file:///x\n"
        self.assertNotEqual(self.hash_of(base), self.hash_of(moved))
        path_a = os.path.join(self.tmp, "a", "plasma-org.kde.plasma.desktop-appletsrc")
        path_b = os.path.join(self.tmp, "b", "plasma-org.kde.plasma.desktop-appletsrc")
        for path, content in ((path_a, base), (path_b, moved)):
            os.makedirs(os.path.dirname(path))
            with open(path, "w") as f:
                f.write(content)
        env_cmd = 'export KDE_SYNC_IGNORE_DOCK=1; '
        ha = self.bash(env_cmd + f'hash_file_normalized "{path_a}"').strip()
        hb = self.bash(env_cmd + f'hash_file_normalized "{path_b}"').strip()
        self.assertEqual(ha, hb)
        # Eine Änderung außerhalb der Dock-Schlüssel bleibt auch dann sichtbar
        path_c = os.path.join(self.tmp, "c", "plasma-org.kde.plasma.desktop-appletsrc")
        os.makedirs(os.path.dirname(path_c))
        with open(path_c, "w") as f:
            f.write(moved.replace("iconSize=46", "iconSize=60"))
        hc = self.bash(env_cmd + f'hash_file_normalized "{path_c}"').strip()
        self.assertNotEqual(hb, hc)


class TestFingerprintInstalledItems(ScriptTestCase):
    def fingerprint(self):
        res = self.run_script("fingerprint")
        self.assertEqual(res.returncode, 0, res.stderr)
        return res.stdout.strip()

    def make_plasmoid(self, name="org.example.dock", qml="Item {}\n"):
        base = f".local/share/plasma/plasmoids/{name}"
        self.write(f"{base}/metadata.json", '{"KPlugin": {"Id": "%s"}}\n' % name)
        self.write(f"{base}/contents/ui/main.qml", qml)
        return os.path.join(self.home, base)

    def test_fingerprint_is_stable_across_runs(self):
        self.write(".config/plasmarc", "[Theme]\nname=x\n")
        self.make_plasmoid()
        self.assertEqual(self.fingerprint(), self.fingerprint())

    def test_reinstall_without_content_change_keeps_fingerprint(self):
        """Eine Neuinstallation (neuer Ordner, neue Zeitstempel) ist keine Änderung."""
        self.write(".config/plasmarc", "[Theme]\nname=x\n")
        path = self.make_plasmoid()
        before = self.fingerprint()
        import shutil
        shutil.rmtree(path)
        self.make_plasmoid()
        os.utime(os.path.join(self.home, ".local/share/plasma/plasmoids"), (1, 1))
        self.assertEqual(before, self.fingerprint())

    def test_new_or_changed_plasmoid_changes_fingerprint(self):
        self.write(".config/plasmarc", "[Theme]\nname=x\n")
        self.make_plasmoid()
        before = self.fingerprint()
        self.make_plasmoid("org.example.other")
        after_new = self.fingerprint()
        self.assertNotEqual(before, after_new)
        self.make_plasmoid("org.example.other", qml="Item { width: 100 }\n")
        self.assertNotEqual(after_new, self.fingerprint())

    def test_state_file_uses_signatures_not_times(self):
        self.make_plasmoid()
        state = os.path.join(self.tmp, "state.fp")
        res = self.run_script("fingerprint", state)
        self.assertEqual(res.returncode, 0, res.stderr)
        with open(state) as f:
            b_lines = [line for line in f if line.startswith("B\t")]
        self.assertEqual(len(b_lines), 1)
        path, _, signature = b_lines[0].rstrip("\n")[2:].rpartition(" ")
        self.assertTrue(path.endswith("org.example.dock"))
        self.assertRegex(signature, r"^[0-9a-f]{16}$")


class TestBackup(ScriptTestCase):
    def test_backup_creates_archive_and_excludes_machine_bound_files(self):
        self.write(".config/plasmarc", "[a]\n")
        self.write(".config/kdeglobals", "[General]\n")
        self.write(".config/kdeconnect/privateKey.pem", "SECRET\n")
        self.write(".config/kdeconnect/certificate.pem", "CERT\n")
        self.write(".config/kde-sync-gui/config.json", "{}\n")
        self.write(".config/powerdevilrc", "[a]\n")
        self.write(".config/kwinoutputconfig.json", "{}\n")
        self.write(".config/akonadi_google_resource_0rc", "[a]\n")
        res = self.run_script("backup")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        archives = [a for a in self.archives() if a.endswith(".tar.gz")]
        self.assertEqual(len(archives), 1)
        self.assertEqual([f for f in os.listdir(self.out) if f.endswith(".part")], [])
        with tarfile.open(os.path.join(self.out, archives[0])) as tar:
            names = tar.getnames()
        self.assertIn(".config/plasmarc", names)
        self.assertIn(".config/kdeglobals", names)
        for forbidden in ("kdeconnect", "kde-sync-gui", "powerdevilrc", "kwinoutputconfig", "akonadi"):
            self.assertFalse([n for n in names if forbidden in n], f"{forbidden} darf nicht im Archiv liegen: {names}")

    def test_user_exclude_list_is_honored(self):
        self.write(".config/plasmarc", "[a]\n")
        self.write(".config/myspecialrc", "[a]\n")
        self.write(".config/kde-sync-gui/exclude.list", "# eigene Ausnahmen\n.config/myspecialrc\n")
        res = self.run_script("backup")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        with tarfile.open(os.path.join(self.out, self.archives()[0])) as tar:
            names = tar.getnames()
        self.assertIn(".config/plasmarc", names)
        self.assertNotIn(".config/myspecialrc", names)

    def test_backup_failure_is_reported_and_leaves_no_archive(self):
        """Früher: 'Backup erfolgreich erstellt' trotz tar-Fehler, Exit-Code 0."""
        self.write(".config/plasmarc", "[a]\n")
        read_only = os.path.join(self.tmp, "readonly")
        os.makedirs(read_only)
        os.chmod(read_only, 0o555)
        if os.access(read_only, os.W_OK):
            self.skipTest("Schreibschutz greift nicht (läuft als root?)")
        res = self.run_script("backup", cwd=read_only)
        self.assertNotEqual(res.returncode, 0, res.stdout)
        self.assertNotIn("Backup erfolgreich erstellt", res.stdout)
        self.assertNotIn("::PROGRESS::DONE", res.stdout)
        self.assertEqual(os.listdir(read_only), [])

    def test_backup_progress_markers_emitted(self):
        res = self.run_script("backup")
        out = res.stdout
        self.assertIn("::PROGRESS::1::4::Plasma Desktop::", out)
        self.assertIn("::PROGRESS::2::4::Hintergrundbild::", out)
        self.assertIn("::PROGRESS::3::4::Themes & Design::", out)
        self.assertIn("::PROGRESS::4::4::Archivierung::", out)
        self.assertIn("::PROGRESS::DONE::", out)


class TestRestore(ScriptTestCase):
    def make_archive(self, files):
        archive = os.path.join(self.tmp, "kde_plasma_backup_20260101_000000.tar.gz")
        with tarfile.open(archive, "w:gz") as tar:
            for rel, content in files.items():
                src = os.path.join(self.tmp, "src", rel)
                os.makedirs(os.path.dirname(src), exist_ok=True)
                with open(src, "w") as f:
                    f.write(content)
                tar.add(src, arcname=rel)
        return archive

    def test_corrupt_archive_changes_nothing_and_fails(self):
        """Früher: 'erfolgreich entpackt' und plasmashell-Neustart bei kaputtem Archiv."""
        bad = os.path.join(self.tmp, "bad.tar.gz")
        with open(bad, "w") as f:
            f.write("kein archiv")
        target = os.path.join(self.tmp, "home2")
        os.makedirs(target)
        res = self.run_script("restore", bad, home=target)
        self.assertNotEqual(res.returncode, 0)
        self.assertNotIn("erfolgreich", res.stdout)
        self.assertEqual(os.listdir(target), [])
        self.assertEqual(self.calls(), [], "plasmashell darf nicht angefasst werden")

    def test_restore_stops_shell_before_extracting_and_starts_it_after(self):
        archive = self.make_archive({".config/plasmarc": "[restored]\n"})
        target = os.path.join(self.tmp, "home2")
        os.makedirs(target)
        res = self.run_script("restore", archive, home=target)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        with open(os.path.join(target, ".config/plasmarc")) as f:
            self.assertEqual(f.read(), "[restored]\n")
        calls = self.calls()
        stop = next(i for i, c in enumerate(calls) if c.startswith("systemctl --user stop"))
        start = next(i for i, c in enumerate(calls) if c.startswith("systemctl --user start"))
        self.assertLess(stop, start)
        self.assertFalse([c for c in calls if c.startswith("sudo")], "kein sudo im Test")

    def test_restore_skips_machine_bound_files_from_old_archives(self):
        archive = self.make_archive({
            ".config/plasmarc": "[restored]\n",
            ".config/kdeconnect/privateKey.pem": "FREMDER SCHLUESSEL\n",
            ".config/powerdevilrc": "[fremd]\n",
        })
        target = os.path.join(self.tmp, "home2")
        os.makedirs(os.path.join(target, ".config/kdeconnect"))
        with open(os.path.join(target, ".config/kdeconnect/privateKey.pem"), "w") as f:
            f.write("EIGENER SCHLUESSEL\n")
        res = self.run_script("restore", archive, home=target)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        with open(os.path.join(target, ".config/kdeconnect/privateKey.pem")) as f:
            self.assertEqual(f.read(), "EIGENER SCHLUESSEL\n")
        self.assertFalse(os.path.exists(os.path.join(target, ".config/powerdevilrc")))


class TestHelpers(ScriptTestCase):
    def test_decode_uri(self):
        self.assertEqual(self.bash('decode_uri "/home/x/Bilder/a%20b.jpg"'), "/home/x/Bilder/a b.jpg")
        self.assertEqual(self.bash('decode_uri "/home/x/50%.jpg"'), "/home/x/50%.jpg")
        self.assertEqual(self.bash('decode_uri "/home/x/a\\\\b.jpg"'), "/home/x/a\\b.jpg")
        self.assertEqual(self.bash('decode_uri "/home/x/%C3%A4.jpg"'), "/home/x/ä.jpg")

    def test_prune_keeps_newest_and_ignores_foreign_files(self):
        names = [f"kde_plasma_backup_2026010{i}_000000.tar.gz" for i in range(1, 6)]
        foreign = "kde_plasma_backup_20260101_000000 (conflicted copy).tar.gz"
        for n in names + [foreign]:
            with open(os.path.join(self.out, n), "w") as f:
                f.write("x")
        res = self.run_script("prune", self.out, "2")
        self.assertEqual(res.returncode, 0)
        left = sorted(os.listdir(self.out))
        self.assertEqual(left, sorted([foreign] + names[-2:]))

    def test_stubs_cover_everything_that_touches_the_session(self):
        for name in STUBBED:
            self.assertTrue(os.access(os.path.join(self.bin, name), os.X_OK))
        self.assertTrue(stat.S_ISREG(os.stat(os.path.join(self.bin, "sudo")).st_mode))


if __name__ == "__main__":
    unittest.main()

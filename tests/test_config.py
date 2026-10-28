#!/usr/bin/env python3
import json
import os
import sys
import tempfile
import unittest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

import config_manager


class TestConfigManager(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orig = (config_manager.CONFIG_DIR, config_manager.CONFIG_FILE)
        config_manager.CONFIG_DIR = self.tmp.name
        config_manager.CONFIG_FILE = os.path.join(self.tmp.name, "config.json")

    def tearDown(self):
        config_manager.CONFIG_DIR, config_manager.CONFIG_FILE = self.orig
        self.tmp.cleanup()

    def test_defaults_are_never_handed_out_by_reference(self):
        cfg = config_manager.load_config()
        cfg["keep_snapshots"] = 99
        cfg["last_fingerprint"] = "abc"
        self.assertEqual(config_manager.DEFAULT_CONFIG["keep_snapshots"], 3)
        self.assertEqual(config_manager.DEFAULT_CONFIG["last_fingerprint"], "")

    def test_corrupt_file_falls_back_to_independent_defaults(self):
        with open(config_manager.CONFIG_FILE, "w") as f:
            f.write("{ das ist kein json")
        cfg = config_manager.load_config()
        cfg["keep_snapshots"] = 42
        self.assertEqual(config_manager.DEFAULT_CONFIG["keep_snapshots"], 3)
        self.assertEqual(config_manager.load_config()["keep_snapshots"], 3)

    def test_missing_keys_are_filled_in_and_saved(self):
        with open(config_manager.CONFIG_FILE, "w") as f:
            json.dump({"backup_dir": "/x"}, f)
        cfg = config_manager.load_config()
        self.assertEqual(cfg["backup_dir"], "/x")
        self.assertIn("auto_snapshot_ignore_dock", cfg)
        with open(config_manager.CONFIG_FILE) as f:
            self.assertIn("fingerprint_version", json.load(f))

    def test_save_is_atomic_and_leaves_no_temp_files(self):
        config_manager.save_config({"a": 1})
        config_manager.save_config({"a": 2})
        with open(config_manager.CONFIG_FILE) as f:
            self.assertEqual(json.load(f), {"a": 2})
        self.assertEqual(os.listdir(self.tmp.name), ["config.json"])

    def test_failed_save_keeps_old_file(self):
        config_manager.save_config({"a": 1})
        # Nicht serialisierbar: json.dump bricht mitten im Schreiben ab
        config_manager.save_config({"a": object()})
        with open(config_manager.CONFIG_FILE) as f:
            self.assertEqual(json.load(f), {"a": 1})
        self.assertEqual(os.listdir(self.tmp.name), ["config.json"])


if __name__ == "__main__":
    unittest.main()

import os
import copy
import json
import tempfile

CONFIG_DIR = os.path.expanduser("~/.config/kde-sync-gui")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

def _default_backup_dir():
    """Cloud-agnostischer Default: nimm den ersten vorhandenen Sync-Ordner
    eines gängigen Clients (Nextcloud, OneDrive, Google Drive, Dropbox,
    Syncthing, ...). Welcher Dienst dahinter steht, ist egal — das Programm
    beobachtet nur den lokalen Ordner, den Sync übernimmt der jeweilige Client."""
    candidates = [
        "~/Nextcloud", "~/ownCloud", "~/OneDrive", "~/GoogleDrive",
        "~/Google Drive", "~/Dropbox", "~/Sync", "~/pCloudDrive",
    ]
    for c in candidates:
        p = os.path.expanduser(c)
        if os.path.isdir(p):
            return os.path.join(p, "kde-backup")
    return os.path.expanduser("~/kde-backup")


DEFAULT_CONFIG = {
    "backup_dir": _default_backup_dir(),
    "last_applied_backup": "",
    "check_interval_hours": 2,
    "periodic_check_enabled": True,
    "autostart_enabled": True,
    "auto_snapshot_enabled": False,
    "auto_snapshot_ask_first": True,
    "auto_snapshot_quiet_seconds": 90,
    "auto_snapshot_min_interval_minutes": 30,
    "keep_snapshots": 3,
    "last_fingerprint": "",
    "last_auto_snapshot": "",
    "last_prompt_time": "",
    "auto_restore_enabled": False,
    "auto_snapshot_ignore_dock": False,
    "fingerprint_version": 0
}

def load_config():
    """Liest die Konfiguration. Gibt immer ein eigenes Objekt zurück, nie DEFAULT_CONFIG
    selbst: Die GUI schreibt in das Ergebnis, die Standardwerte dürfen sich nicht ändern."""
    if not os.path.exists(CONFIG_DIR):
        os.makedirs(CONFIG_DIR, exist_ok=True)
    if not os.path.exists(CONFIG_FILE):
        config = copy.deepcopy(DEFAULT_CONFIG)
        save_config(config)
        return config
    try:
        with open(CONFIG_FILE, "r") as f:
            config = json.load(f)
        if not isinstance(config, dict):
            raise ValueError("config.json enthält kein Objekt")
        # Ensure all default keys exist
        updated = False
        for k, v in DEFAULT_CONFIG.items():
            if k not in config:
                config[k] = copy.deepcopy(v)
                updated = True
        if updated:
            save_config(config)
        return config
    except Exception:
        return copy.deepcopy(DEFAULT_CONFIG)

def save_config(config):
    """Schreibt atomar: erst in eine Temp-Datei im selben Ordner, dann umbenennen.
    Ein Absturz mitten im Schreiben hinterlässt so nie eine halbe config.json."""
    if not os.path.exists(CONFIG_DIR):
        os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(dir=CONFIG_DIR, prefix=".config.", suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(config, f, indent=4)
        os.replace(tmp_path, CONFIG_FILE)
        tmp_path = None
    except Exception as e:
        print(f"Error saving config: {e}")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass

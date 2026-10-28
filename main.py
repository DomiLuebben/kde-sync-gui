#!/usr/bin/env python3
import sys
import os
import re
import json
import subprocess
import pty
import select
import shutil
import threading
from datetime import datetime

from PyQt6.QtCore import QThread, pyqtSignal, QTimer, Qt, QSize, QFileSystemWatcher
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTabWidget, QPushButton, QLabel, QPlainTextEdit, QLineEdit,
    QListWidget, QListWidgetItem, QFileDialog, QMessageBox, QDialog,
    QCheckBox, QGroupBox, QFormLayout, QSystemTrayIcon, QMenu,
    QProgressBar, QStyle, QSpinBox
)
from PyQt6.QtGui import QIcon, QFont, QAction, QPixmap

import config_manager

# Ensure paths are correct (resolve relative to this script's directory)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPT_PATH = os.path.join(SCRIPT_DIR, "kde_sync.sh")
if not os.path.exists(SCRIPT_PATH):
    SCRIPT_PATH = os.path.join(SCRIPT_DIR, "kde-sync.sh")

# Vergleichsstand für die Änderungserkennung. Liegt bewusst in
# ~/.config/kde-sync-gui — dieser Ordner ist aus dem Backup-Set ausgenommen,
# sonst löste jedes Schreiben hier den nächsten Snapshot aus.
STATE_FILE = os.path.expanduser("~/.config/kde-sync-gui/state.fp")
STATE_FILE_NEW = STATE_FILE + ".new"
# Eigene Zwischendatei für die stille Neu-Aufnahme der Vergleichsbasis, damit sie
# sich nicht mit einem gleichzeitig laufenden Auto-Snapshot-Lauf überschreibt.
STATE_FILE_BASE_NEW = STATE_FILE + ".base.new"
# Protokoll jeder erkannten Änderung und der Antwort darauf. Ohne dieses Protokoll ist
# hinterher nicht mehr nachvollziehbar, warum gefragt wurde (state.fp wird überschrieben).
CHANGES_LOG = os.path.expanduser("~/.config/kde-sync-gui/changes.log")
CHANGES_LOG_MAX_BYTES = 256 * 1024

# Version des Fingerabdruck-Verfahrens. Ändert sich das Verfahren, wird die Vergleichsbasis
# einmal still neu aufgenommen, statt wegen der neuen Berechnung nachzufragen.
FINGERPRINT_VERSION = 2

# Gültiger Snapshot-Name. Muss zur Regel in kde_sync.sh prune passen.
BACKUP_NAME_RE = re.compile(r"^kde_plasma_backup_\d{8}_\d{6}\.tar\.gz$")


def format_size(bytes_size):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if bytes_size < 1024:
            return f"{bytes_size:.1f} {unit}"
        bytes_size /= 1024
    return f"{bytes_size:.1f} TB"


def clean_ansi(text):
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    text = ansi_escape.sub('', text)
    text = text.replace('\r', '')
    return text


# Klartext-Namen für die Werte aus dem Manifest (S-Zeilen der Zustandsdatei).
SEMANTIC_LABELS = {
    "WALLPAPER": "das Hintergrundbild",
    "LOCK_WALLPAPER": "das Bild des Sperrbildschirms",
    "ICON_THEME": "das Icon-Design",
    "GTK_ICON_THEME": "das GTK-Icon-Design",
    "GTK_THEME": "das GTK-Design",
    "CURSOR_THEME": "den Mauszeiger",
    "KVANTUM_THEME": "das Kvantum-Design",
    "COLOR_SCHEME": "das Farbschema",
    "WIDGET_STYLE": "den Bedienelement-Stil",
    "PLASMA_THEME": "das Plasma-Design",
    "DECORATION": "die Fensterdekoration",
    "USES_LATTE": "die Latte-Dock-Nutzung",
}

BACKUP_STEPS = [
    ("🖥️", "Plasma Desktop", "Panels, Widgets & Desktop-Konfigurationen erfassen"),
    ("🖼️", "Hintergrundbild", "Aktive Desktop- & Sperrbildschirm-Hintergründe sammeln"),
    ("🎨", "Themes & Design", "Farbschemata, Kvantum-, GTK- und Icon-Themes sichern"),
    ("📦", "Archivierung", "Backup-Archiv komprimieren & im Sync-Ordner ablegen"),
]

RESTORE_STEPS = [
    ("🔍", "Vorbereitung", "Theme-Manifest & System-Abhängigkeiten prüfen"),
    ("📦", "Dateien einspielen", "KDE-Konfigurationen, Widgets und Themes entpacken"),
    ("🖼️", "Hintergrund & Schriften", "Hintergrundbild einrichten & Font-Cache aktualisieren"),
    ("🖥️", "Plasma Desktop", "Plasma Desktop & KWin neu laden, Layouts anwenden"),
]


def parse_state_file(path):
    """Liest eine Zustandsdatei von 'kde_sync.sh fingerprint <datei>'.
    Ergibt (semantische Werte, Datei-Hashes, Theme-Ordner-Zeitstempel)."""
    semantic, files, dirs = {}, {}, {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.rstrip("\n")
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                kind = parts[0]
                if kind == "S" and len(parts) >= 2 and "=" in parts[1]:
                    key, _, value = parts[1].partition("=")
                    semantic[key] = value
                elif kind == "A" and len(parts) >= 3:
                    files[parts[2]] = parts[1]
                elif kind == "B" and len(parts) >= 2:
                    # Rohzeile "<pfad> <mtime>" — am letzten Leerzeichen trennen
                    raw = parts[1]
                    p, _, t = raw.rpartition(" ")
                    if p:
                        dirs[p] = t
    except OSError:
        return None
    return semantic, files, dirs


def _plain_path(value):
    """Wallpaper-Werte aus den Konfigurationen als Dateipfad (ohne file://)."""
    if value.startswith("file://"):
        from urllib.parse import unquote
        return unquote(value[7:])
    return value


def diff_states(old_path, new_path):
    """Vergleicht zwei Zustandsdateien. Gibt (benannte Änderungen, Einzelheiten) zurück
    oder None, wenn eine der Dateien fehlt. Die Einzelheiten (geänderte Schlüssel,
    Dateien, Ordner) gehen ins Änderungsprotokoll."""
    old = parse_state_file(old_path)
    new = parse_state_file(new_path)
    if not old or not new:
        return None

    old_sem, old_files, old_dirs = old
    new_sem, new_files, new_dirs = new
    details = {"semantic": [], "files": [], "dirs": []}

    # 1. Semantische Änderungen zuerst (Wallpaper, Look&Feel, Farben)
    named = []
    for key, label in SEMANTIC_LABELS.items():
        if old_sem.get(key, "") != new_sem.get(key, ""):
            named.append(label)
            details["semantic"].append(key)

    # 2. Konfigurationsdateien: Arbeitsflächen-Widgets, Panels, Lockscreen
    changed_paths = set()
    for p, h in new_files.items():
        if old_files.get(p) != h:
            changed_paths.add(p)
    for p in old_files:
        if p not in new_files:
            changed_paths.add(p)
    changed_files = {os.path.basename(p) for p in changed_paths}
    details["files"] = sorted(changed_paths)

    # Ein Hintergrundbild, das am selben Pfad ausgetauscht wurde: die Datei selbst steht
    # im Zustand, der Pfad in den Einstellungen bleibt gleich.
    wallpaper = _plain_path(new_sem.get("WALLPAPER", ""))
    lock_wallpaper = _plain_path(new_sem.get("LOCK_WALLPAPER", ""))
    for p in changed_paths:
        if lock_wallpaper and p == lock_wallpaper and "das Bild des Sperrbildschirms" not in named:
            named.append("das Bild des Sperrbildschirms")
        if wallpaper and p == wallpaper and "das Hintergrundbild" not in named:
            named.append("das Hintergrundbild")

    if "plasma-org.kde.plasma.desktop-appletsrc" in changed_files:
        # Da plasma-org.kde.plasma.desktop-appletsrc auch das Hintergrundbild speichert,
        # Widgets nur benennen, wenn nicht bereits das Hintergrundbild als Ursache erkannt wurde.
        if "das Hintergrundbild" not in named and "die Arbeitsflächen-Widgets" not in named:
            named.append("die Arbeitsflächen-Widgets")

    if "plasmashellrc" in changed_files and "die Plasma-Panels" not in named:
        named.append("die Plasma-Panels")
    if "plasmarc" in changed_files and "das Plasma-Design" not in named:
        named.append("das Plasma-Design")
    if "kscreenlockerrc" in changed_files and "das Bild des Sperrbildschirms" not in named:
        named.append("den Sperrbildschirm")

    # 3. Theme- und Widget-Ordner (Ebene B)
    new_dir_keys = set(new_dirs)
    old_dir_keys = set(old_dirs)
    changed_dirs = [p for p, t in new_dirs.items() if old_dirs.get(p) != t] + list(new_dir_keys ^ old_dir_keys)
    details["dirs"] = sorted(set(changed_dirs))
    if changed_dirs:
        has_plasmoid = any("plasmoids" in p for p in changed_dirs)
        has_wallpaper = any("wallpapers" in p for p in changed_dirs)
        has_theme = any("desktoptheme" in p for p in changed_dirs)
        if has_plasmoid and "die installierten Widgets" not in named:
            named.append("die installierten Widgets")
        if has_wallpaper and "das Hintergrundbild" not in named:
            named.append("die Hintergrundbilder")
        if has_theme and "das Plasma-Design" not in named:
            named.append("die installierten Plasma-Designs")
        if not (has_plasmoid or has_wallpaper or has_theme) and "die installierten Designs" not in named:
            named.append("die installierten Designs")

    return list(dict.fromkeys(named)), details


def format_change_list(named, max_items=3):
    """Baut aus der Liste benannter Änderungen den Satzteil für 'Du hast … geändert'."""
    if not named:
        return None
    if len(named) > max_items:
        shown = named[:max_items]
        return ", ".join(shown) + f" und {len(named) - max_items} weitere Einstellungen"
    if len(named) == 1:
        return named[0]
    return ", ".join(named[:-1]) + " und " + named[-1]


def describe_changes(old_path, new_path, max_items=3):
    """Baut den Satzteil für 'Du hast … geändert'.
    Gibt None zurück, wenn sich nichts benennen lässt."""
    result = diff_states(old_path, new_path)
    if result is None:
        return None
    return format_change_list(result[0], max_items)


def write_change_log(action, what=None, details=None, old_fp="", new_fp=""):
    """Hängt eine Zeile (JSON) ans Änderungsprotokoll an. Fehler hier dürfen nie
    die Erkennung stören."""
    entry = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "action": action,
        "what": what or "",
        "old": old_fp[:12],
        "new": new_fp[:12],
        "details": details or {},
    }
    try:
        os.makedirs(os.path.dirname(CHANGES_LOG), exist_ok=True)
        if os.path.exists(CHANGES_LOG) and os.path.getsize(CHANGES_LOG) > CHANGES_LOG_MAX_BYTES:
            with open(CHANGES_LOG, "r", encoding="utf-8", errors="replace") as f:
                tail = f.readlines()[-500:]
            with open(CHANGES_LOG, "w", encoding="utf-8") as f:
                f.writelines(tail)
        with open(CHANGES_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


AUTOSTART_TEMPLATE = (
    "[Desktop Entry]\n"
    "Type=Application\n"
    "Exec={exec_cmd}\n"
    "Hidden=false\n"
    "NoDisplay=false\n"
    "X-GNOME-Autostart-enabled=true\n"
    "Name=KDE Plasma Sync GUI\n"
    "Comment=KDE Plasma Backup & Sync GUI Tool\n"
    "Icon=preferences-desktop-wallpaper\n"
    "Categories=System;Utility;\n"
    "StartupNotify=false\n"
    "Terminal=false\n"
)


def update_autostart_file(enabled):
    autostart_dir = os.path.expanduser("~/.config/autostart")
    autostart_file = os.path.join(autostart_dir, "kde-sync-gui.desktop")

    if enabled:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        if script_dir.startswith("/opt/"):
            exec_cmd = "kde-sync-gui --minimized"
        else:
            exec_cmd = f"python3 {os.path.join(script_dir, 'main.py')} --minimized"
        content = AUTOSTART_TEMPLATE.format(exec_cmd=exec_cmd)

        try:
            # Nur schreiben, wenn sich etwas ändert; die Datei darf nicht ausführbar sein
            # (systemd-xdg-autostart-generator warnt sonst bei jedem Login).
            current = None
            if os.path.exists(autostart_file):
                with open(autostart_file, "r") as f:
                    current = f.read()
            if current != content:
                os.makedirs(autostart_dir, exist_ok=True)
                with open(autostart_file, "w") as f:
                    f.write(content)
            if (os.stat(autostart_file).st_mode & 0o777) != 0o644:
                os.chmod(autostart_file, 0o644)
        except Exception as e:
            print(f"Error writing autostart file: {e}")
    else:
        if os.path.exists(autostart_file):
            try:
                os.remove(autostart_file)
            except Exception as e:
                print(f"Error removing autostart file: {e}")


class ScriptRunner(QThread):
    output_received = pyqtSignal(str)
    password_requested = pyqtSignal()
    finished_status = pyqtSignal(int)
    
    def __init__(self, cmd, cwd, env=None):
        super().__init__()
        self.cmd = cmd
        self.cwd = cwd
        # Zusätzliche Umgebungsvariablen für das Skript (z. B. KDE_SYNC_NO_SUDO)
        self.extra_env = env or {}
        self.password = None
        self.password_ready = threading.Event()
        self.stop_requested = False
        
    def run(self):
        master_fd, slave_fd = pty.openpty()
        try:
            p = subprocess.Popen(
                self.cmd,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
                cwd=self.cwd,
                env={**os.environ, **self.extra_env},
                text=False
            )
        except Exception as e:
            self.output_received.emit(f"Fehler beim Starten des Prozesses: {e}\n")
            self.finished_status.emit(-1)
            return

        os.close(slave_fd)
        
        while p.poll() is None:
            if self.stop_requested:
                self._terminate(p)
                break
                
            r, w, x = select.select([master_fd], [], [], 0.1)
            if master_fd in r:
                try:
                    data = os.read(master_fd, 4096)
                    if not data:
                        break
                    text = data.decode('utf-8', errors='replace')
                    self.output_received.emit(text)
                    
                    # Detect sudo password prompt
                    if "password for" in text.lower() or "passwort für" in text.lower():
                        self.password_ready.clear()
                        self.password_requested.emit()
                        # Wait for GUI to set the password
                        self.password_ready.wait()
                        if self.password is not None:
                            os.write(master_fd, self.password.encode('utf-8') + b'\n')
                            self.password = None
                except OSError:
                    break
                    
        # Read trailing buffer
        try:
            while True:
                r, w, x = select.select([master_fd], [], [], 0.05)
                if master_fd in r:
                    data = os.read(master_fd, 4096)
                    if not data:
                        break
                    self.output_received.emit(data.decode('utf-8', errors='replace'))
                else:
                    break
        except OSError:
            pass
            
        os.close(master_fd)
        p.wait()
        self.finished_status.emit(p.returncode)

    @staticmethod
    def _terminate(p):
        """Beendet das Skript samt seiner Kindprozesse (tar, gzip, sudo). Nur direkte Kinder
        unseres eigenen Prozesses, keine Suche nach Namen."""
        try:
            subprocess.run(["pkill", "-TERM", "-P", str(p.pid)], check=False)
        except OSError:
            pass
        p.terminate()

    def set_password(self, password):
        self.password = password
        self.password_ready.set()

    def stop(self):
        self.stop_requested = True
        self.password_ready.set()


class FingerprintRunner(QThread):
    finished_fingerprint = pyqtSignal(str, int)

    def __init__(self, script_path, cwd=None, state_out=None, ignore_dock=False):
        super().__init__()
        self.script_path = script_path
        self.cwd = cwd
        self.ignore_dock = ignore_dock
        # Optionaler Pfad für den Einzelzustand; daraus entsteht der Satz
        # "Du hast das Hintergrundbild geändert".
        self.state_out = state_out

    def run(self):
        try:
            cmd = [self.script_path, "fingerprint"]
            if self.state_out:
                cmd.append(self.state_out)
            res = subprocess.run(
                cmd,
                cwd=self.cwd,
                env={**os.environ, "KDE_SYNC_IGNORE_DOCK": "1" if self.ignore_dock else "0"},
                capture_output=True,
                text=True,
                check=False
            )
            fp = res.stdout.strip()
            self.finished_fingerprint.emit(fp, res.returncode)
        except Exception as e:
            self.finished_fingerprint.emit(str(e), -1)


class PasswordDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Administratorrechte erforderlich")
        self.setModal(True)
        self.setFixedWidth(380)
        
        layout = QVBoxLayout(self)
        
        icon_label = QLabel()
        icon_label.setPixmap(QIcon.fromTheme("dialog-password").pixmap(48, 48))
        icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(icon_label)
        
        text_label = QLabel(
            "Das Backup-Skript erfordert Administratorrechte (sudo), "
            "um eventuell fehlende KDE-Abhängigkeiten oder Themes zu installieren.\n\n"
            "Bitte gib das Passwort für 'sudo' ein:"
        )
        text_label.setWordWrap(True)
        layout.addWidget(text_label)
        
        self.password_input = QLineEdit()
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addWidget(self.password_input)
        
        btn_layout = QHBoxLayout()
        self.btn_ok = QPushButton("Bestätigen")
        self.btn_ok.setObjectName("primaryButton")
        self.btn_ok.clicked.connect(self.accept)
        
        self.btn_cancel = QPushButton("Abbrechen")
        self.btn_cancel.clicked.connect(self.reject)
        
        btn_layout.addWidget(self.btn_cancel)
        btn_layout.addWidget(self.btn_ok)
        layout.addLayout(btn_layout)


class BackupItemWidget(QWidget):
    restore_requested = pyqtSignal(str)
    delete_requested = pyqtSignal(str)
    
    def __init__(self, filename, backup_dir):
        super().__init__()
        self.filename = filename
        self.filepath = os.path.join(backup_dir, filename)
        
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        
        icon_label = QLabel()
        icon_label.setPixmap(QIcon.fromTheme("package-x-generic").pixmap(32, 32))
        layout.addWidget(icon_label)
        
        info_layout = QVBoxLayout()
        name_label = QLabel(filename)
        name_label.setStyleSheet("font-weight: bold; font-size: 13px;")
        
        date_str = "Unbekanntes Datum"
        try:
            match = re.search(r'(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})', filename)
            if match:
                y, m, d, hh, mm, ss = match.groups()
                date_str = f"{d}.{m}.{y} {hh}:{mm}:{ss}"
        except Exception:
            pass
            
        date_label = QLabel(f"Erstellt am: {date_str}")
        date_label.setStyleSheet("color: #a0a0b0; font-size: 11px;")
        
        info_layout.addWidget(name_label)
        info_layout.addWidget(date_label)
        layout.addLayout(info_layout)
        
        layout.addStretch()
        
        size_str = "0 B"
        if os.path.exists(self.filepath):
            size_str = format_size(os.path.getsize(self.filepath))
        size_label = QLabel(size_str)
        size_label.setStyleSheet("color: #a0a0b0; font-size: 12px; margin-right: 15px;")
        layout.addWidget(size_label)
        
        btn_restore = QPushButton("Einspielen")
        btn_restore.setToolTip("Dieses Backup wiederherstellen")
        btn_restore.clicked.connect(lambda: self.restore_requested.emit(self.filename))
        
        btn_delete = QPushButton("Löschen")
        btn_delete.setObjectName("dangerButton")
        btn_delete.setToolTip("Backup löschen")
        btn_delete.clicked.connect(lambda: self.delete_requested.emit(self.filename))
        
        layout.addWidget(btn_restore)
        layout.addWidget(btn_delete)


class KdeSyncGUI(QMainWindow):
    def __init__(self, autostart_check=False):
        super().__init__()
        self.config = config_manager.load_config()
        self.runner = None
        self.check_timer = QTimer(self)
        self.is_operation_running = False

        self.setWindowTitle("KDE Plasma Backup & Sync")
        self.setMinimumSize(750, 500)
        self.setWindowIcon(QIcon.fromTheme("preferences-desktop-wallpaper"))

        # Setup main container
        self.central_widget = QWidget()
        self.central_widget.setObjectName("centralWidget")
        self.central_widget.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCentralWidget(self.central_widget)
        self.main_layout = QVBoxLayout(self.central_widget)
        self.main_layout.setContentsMargins(15, 15, 15, 15)

        # Tab Widget (nur Dashboard, Backups, Einstellungen - kein Protokoll-Tab)
        self.tabs = QTabWidget()
        self.main_layout.addWidget(self.tabs)

        # Initialize pages
        self.init_dashboard_tab()
        self.init_history_tab()
        self.init_settings_tab()

        # Grafische Fortschrittskarte (wird während Backup / Restore eingeblendet)
        self.operation_card = QGroupBox("Laufende Aktion")
        self.operation_card.setObjectName("operationCard")
        self.operation_card_layout = QVBoxLayout(self.operation_card)
        self.operation_card_layout.setContentsMargins(15, 12, 15, 12)
        self.operation_card_layout.setSpacing(10)

        # Kopfzeile: Titel, Schritt-Badge, Toggle-Button, Schließen-Button
        op_header_layout = QHBoxLayout()
        self.lbl_op_title = QLabel("Aktion läuft...")
        self.lbl_op_title.setObjectName("operationTitle")
        
        self.lbl_op_step_badge = QLabel("Schritt 1 von 4")
        self.lbl_op_step_badge.setObjectName("operationStepBadge")
        
        self.btn_toggle_protocol = QPushButton("▼ Protokoll anzeigen")
        self.btn_toggle_protocol.setObjectName("btnToggleProtocol")
        self.btn_toggle_protocol.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_toggle_protocol.clicked.connect(self.toggle_console)

        self.btn_cancel_op = QPushButton("Abbrechen")
        self.btn_cancel_op.setObjectName("btnCancelOp")
        self.btn_cancel_op.setToolTip("Laufende Aktion abbrechen")
        self.btn_cancel_op.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_cancel_op.setVisible(False)
        self.btn_cancel_op.clicked.connect(self.cancel_operation)

        self.btn_close_card = QPushButton("✕")
        self.btn_close_card.setObjectName("btnCloseCard")
        self.btn_close_card.setToolTip("Aktionsanzeige schließen")
        self.btn_close_card.setFixedSize(24, 24)
        self.btn_close_card.setVisible(False)
        self.btn_close_card.clicked.connect(lambda: self.operation_card.setVisible(False))

        op_header_layout.addWidget(self.lbl_op_title)
        op_header_layout.addWidget(self.lbl_op_step_badge)
        op_header_layout.addStretch()
        op_header_layout.addWidget(self.btn_toggle_protocol)
        op_header_layout.addWidget(self.btn_cancel_op)
        op_header_layout.addWidget(self.btn_close_card)
        self.operation_card_layout.addLayout(op_header_layout)

        # Grafische Phasenanzeige (Großes Icon + Titel + Detailbeschreibung)
        activity_layout = QHBoxLayout()
        activity_layout.setSpacing(12)
        
        self.lbl_phase_icon = QLabel("🖥️")
        self.lbl_phase_icon.setObjectName("phaseIcon")
        
        phase_text_layout = QVBoxLayout()
        phase_text_layout.setSpacing(2)
        self.lbl_phase_title = QLabel("Plasma Desktop")
        self.lbl_phase_title.setObjectName("phaseTitle")
        self.lbl_phase_detail = QLabel("Sammle KDE-Konfigurationen, Widgets und Panels...")
        self.lbl_phase_detail.setObjectName("phaseDetail")
        self.lbl_phase_detail.setWordWrap(True)
        phase_text_layout.addWidget(self.lbl_phase_title)
        phase_text_layout.addWidget(self.lbl_phase_detail)

        activity_layout.addWidget(self.lbl_phase_icon)
        activity_layout.addLayout(phase_text_layout, 1)
        self.operation_card_layout.addLayout(activity_layout)

        # Visuelle Phasen-Pills
        self.steps_pills_layout = QHBoxLayout()
        self.steps_pills_layout.setSpacing(6)
        self.step_pills = []
        for i in range(4):
            pill = QLabel(f"Schritt {i+1}")
            pill.setObjectName(f"stepPill_{i}")
            pill.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.step_pills.append(pill)
            self.steps_pills_layout.addWidget(pill)
            if i < 3:
                arrow = QLabel("›")
                arrow.setStyleSheet("color: #606070; font-size: 14px; font-weight: bold;")
                self.steps_pills_layout.addWidget(arrow)
        self.operation_card_layout.addLayout(self.steps_pills_layout)

        # Fortschrittsbalken
        self.op_progress_bar = QProgressBar()
        self.op_progress_bar.setObjectName("operationProgressBar")
        self.op_progress_bar.setRange(0, 100)
        self.op_progress_bar.setValue(0)
        self.op_progress_bar.setTextVisible(True)
        self.operation_card_layout.addWidget(self.op_progress_bar)

        # Einklappbarer Protokollbereich
        self.console_widget = QWidget()
        self.console_layout = QVBoxLayout(self.console_widget)
        self.console_layout.setContentsMargins(0, 6, 0, 0)
        self.console_layout.setSpacing(4)
        
        self.inline_log_output = QPlainTextEdit()
        self.inline_log_output.setObjectName("inlineLogOutput")
        self.inline_log_output.setReadOnly(True)
        self.inline_log_output.setFont(QFont("Monospace", 9))
        self.inline_log_output.setMinimumHeight(110)
        self.inline_log_output.setMaximumHeight(180)
        self.console_layout.addWidget(self.inline_log_output)
        
        log_btn_layout = QHBoxLayout()
        self.btn_copy_log = QPushButton("Protokoll kopieren")
        self.btn_copy_log.setObjectName("btnCopyLog")
        self.btn_copy_log.clicked.connect(self.copy_log_to_clipboard)
        self.btn_clear_log = QPushButton("Protokoll leeren")
        self.btn_clear_log.setObjectName("btnClearLog")
        self.btn_clear_log.clicked.connect(self.inline_log_output.clear)
        log_btn_layout.addStretch()
        log_btn_layout.addWidget(self.btn_clear_log)
        log_btn_layout.addWidget(self.btn_copy_log)
        self.console_layout.addLayout(log_btn_layout)

        self.console_widget.setVisible(False)
        self.operation_card_layout.addWidget(self.console_widget)

        self.operation_card.setVisible(False)
        self.main_layout.addWidget(self.operation_card)

        # Referenz für Kompatibilität
        self.log_output = self.inline_log_output
        self.btn_toggle_console = self.btn_toggle_protocol

        # Status Bar (für Ruhezustand / einfache Meldungen)
        self.progress_layout = QHBoxLayout()
        self.status_bar_label = QLabel("Bereit")
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setMaximumWidth(120)

        self.progress_layout.addWidget(self.status_bar_label)
        self.progress_layout.addStretch()
        self.progress_layout.addWidget(self.progress_bar)
        self.main_layout.addLayout(self.progress_layout)

        # Apply QSS Styling
        self.load_stylesheet()

        # Setup System Tray
        self.init_tray_icon()

        # Auto-check timers
        self.check_timer.timeout.connect(self.on_periodic_timer_timeout)
        self.update_timer_interval()

        # Sofort-Erkennung: reagiert, sobald der Cloud-Client eine Datei in den
        # Sync-Ordner legt (statt nur alle X Stunden zu pollen). Der Debounce
        # sammelt die vielen Events während eines laufenden Syncs ein.
        self.fs_watcher = QFileSystemWatcher(self)
        self.fs_watcher.directoryChanged.connect(self.on_backup_dir_changed)
        self.fs_debounce = QTimer(self)
        self.fs_debounce.setSingleShot(True)
        self.fs_debounce.setInterval(5000)
        self.fs_debounce.timeout.connect(self.background_update_check)
        self.setup_fs_watcher()

        # Automatische Snapshots: Wächter für Konfigurationsverzeichnisse
        self.config_watcher = QFileSystemWatcher(self)
        self.config_watcher.directoryChanged.connect(self.on_config_dir_changed)
        self.auto_snapshot_debounce = QTimer(self)
        self.auto_snapshot_debounce.setSingleShot(True)
        self.auto_snapshot_debounce.timeout.connect(self.check_auto_snapshot)
        self.restore_lockout_until = 0.0
        # Erste Änderung der laufenden Ruhephase (siehe on_config_dir_changed)
        self.first_change_ts = 0.0
        # Offene, nicht-modale Nachfrage "Snapshot anlegen?"
        self.snapshot_dialog = None
        self.snapshot_ctx = None
        self.fingerprint_runner = None
        self._bg_fp_runner = None
        self.pending_fingerprint = ""
        self.pending_change_text = ""
        self.is_silent_backup = False
        # Nur für diese Sitzung abgelehnter Snapshot (siehe background_update_check)
        self.declined_this_session = ""
        self.setup_config_watcher()
        # Neues Fingerabdruck-Verfahren: Vergleichsbasis einmal still neu aufnehmen,
        # sonst fragt die GUI wegen der geänderten Berechnung nach.
        if self.config.get("fingerprint_version") != FINGERPRINT_VERSION:
            self.config["last_fingerprint"] = ""
            self.config["fingerprint_version"] = FINGERPRINT_VERSION
            config_manager.save_config(self.config)
        if self.config.get("auto_snapshot_enabled", False) and not self.config.get("last_fingerprint"):
            self.update_fingerprint_baseline_silently()

        # Check script existence
        self.verify_script_exists()

        # Backup-Liste initial befüllen (vorher war sie bis zum ersten Klick
        # auf "Liste aktualisieren" leer) und beim Wechsel auf den Tab auffrischen.
        self.refresh_backup_list()
        self.tabs.currentChanged.connect(self.on_tab_changed)

        # Update autostart if enabled in config
        if self.config.get("autostart_enabled", True):
            update_autostart_file(True)

        # Run startup check
        if autostart_check:
            # We delay the check slightly to let desktop components load completely
            QTimer.singleShot(3000, self.run_startup_update_check)
        else:
            QTimer.singleShot(1000, self.background_update_check)

        # Fehlt die Vergleichsbasis, jetzt eine anlegen. Sonst würde die erste
        # echte Änderung nach dem Start nur als Basis verbucht statt gesichert.
        if self.config.get("auto_snapshot_enabled", False) and not self.config.get("last_fingerprint"):
            QTimer.singleShot(5000, self.update_fingerprint_baseline_silently)

    def load_stylesheet(self):
        style_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "style.qss")
        if os.path.exists(style_path):
            with open(style_path, "r") as f:
                self.setStyleSheet(f.read())

    def toggle_console(self):
        is_visible = self.console_widget.isVisible()
        self.console_widget.setVisible(not is_visible)
        if not is_visible:
            self.btn_toggle_protocol.setText("▲ Protokoll ausblenden")
            if self.height() < 640:
                self.resize(self.width(), 640)
            self.inline_log_output.ensureCursorVisible()
        else:
            self.btn_toggle_protocol.setText("▼ Protokoll anzeigen")

    def set_pill_state(self, index, state):
        if index < 0 or index >= len(self.step_pills):
            return
        pill = self.step_pills[index]
        if state == "active":
            pill.setStyleSheet("background-color: #2b4a6f; color: #3daee9; border: 1px solid #3daee9; border-radius: 4px; padding: 4px 8px; font-weight: bold;")
        elif state == "done":
            pill.setStyleSheet("background-color: #1b2f24; color: #2ecc71; border: 1px solid #2ecc71; border-radius: 4px; padding: 4px 8px; font-weight: 500;")
        elif state == "error":
            pill.setStyleSheet("background-color: #381b1b; color: #da4453; border: 1px solid #da4453; border-radius: 4px; padding: 4px 8px;")
        else:  # pending
            pill.setStyleSheet("background-color: #1b1b22; color: #707080; border: 1px solid #2a2a35; border-radius: 4px; padding: 4px 8px;")

    def init_operation_ui(self, title, step_defs):
        self.current_step_defs = step_defs
        self.lbl_op_title.setText(title)
        self.lbl_op_step_badge.setText(f"Schritt 1 von {len(step_defs)}")
        self.lbl_phase_icon.setText(step_defs[0][0])
        self.lbl_phase_title.setText(step_defs[0][1])
        self.lbl_phase_detail.setText(step_defs[0][2])
        self.op_progress_bar.setValue(10)
        self.btn_close_card.setVisible(False)
        self.btn_toggle_protocol.setText("▼ Protokoll anzeigen")
        self.console_widget.setVisible(False)

        for i, (icon, name, _) in enumerate(step_defs):
            if i < len(self.step_pills):
                self.step_pills[i].setText(f"{icon} {name}")
                if i == 0:
                    self.set_pill_state(i, "active")
                else:
                    self.set_pill_state(i, "pending")
        self.operation_card.setVisible(True)

    def on_progress_step(self, step, total, phase, detail):
        self.lbl_op_step_badge.setText(f"Schritt {step} von {total}")
        self.lbl_phase_title.setText(phase)
        self.lbl_phase_detail.setText(detail)

        # Icon mapping basierend auf Phasen-Namen
        icon = "⚙️"
        if "Plasma" in phase or "Desktop" in phase:
            icon = "🖥️"
        elif "Hintergrund" in phase or "Wallpaper" in phase:
            icon = "🖼️"
        elif "Theme" in phase or "Design" in phase:
            icon = "🎨"
        elif "Archiv" in phase or "Datei" in phase or "entpack" in phase:
            icon = "📦"
        elif "Vorbereit" in phase or "Prüf" in phase:
            icon = "🔍"
        self.lbl_phase_icon.setText(icon)

        pct = int((step / max(total, 1)) * 95)
        self.op_progress_bar.setValue(max(pct, 10))

        for i in range(len(self.step_pills)):
            if i < step - 1:
                text = self.step_pills[i].text()
                if not text.startswith("✓ "):
                    self.step_pills[i].setText(f"✓ {text}")
                self.set_pill_state(i, "done")
            elif i == step - 1:
                self.set_pill_state(i, "active")
            else:
                self.set_pill_state(i, "pending")

    def on_progress_done(self, title, detail):
        self.lbl_phase_icon.setText("✅")
        self.lbl_phase_title.setText(title)
        self.lbl_phase_detail.setText(detail)
        self.lbl_op_step_badge.setText("Fertig")
        self.op_progress_bar.setValue(100)
        for i in range(len(self.step_pills)):
            text = self.step_pills[i].text()
            if not text.startswith("✓ "):
                self.step_pills[i].setText(f"✓ {text}")
            self.set_pill_state(i, "done")
        self.btn_close_card.setVisible(True)

    def verify_script_exists(self):
        if not os.path.exists(SCRIPT_PATH):
            self.status_bar_label.setText(f"Skript nicht gefunden unter: {SCRIPT_PATH}")
            self.status_bar_label.setStyleSheet("color: #da4453; font-weight: bold;")
            self.btn_backup.setEnabled(False)
            self.btn_restore_latest.setEnabled(False)
            return False
        return True

    def init_tray_icon(self):
        self.tray_icon = QSystemTrayIcon(self)
        self.tray_icon.setIcon(QIcon.fromTheme("preferences-desktop-wallpaper"))
        
        tray_menu = QMenu()
        show_action = QAction("GUI Öffnen", self)
        show_action.triggered.connect(self.show_normal)
        
        check_action = QAction("Jetzt auf Updates prüfen", self)
        check_action.triggered.connect(lambda: self.background_update_check(manual=True))
        
        backup_action = QAction("Jetzt sichern", self)
        backup_action.triggered.connect(self.start_backup)
        
        exit_action = QAction("Beenden", self)
        exit_action.triggered.connect(QApplication.instance().quit)
        
        tray_menu.addAction(show_action)
        tray_menu.addAction(check_action)
        tray_menu.addAction(backup_action)
        tray_menu.addSeparator()
        tray_menu.addAction(exit_action)
        
        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.activated.connect(self.tray_icon_activated)
        self.tray_icon.show()

    def tray_icon_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show_normal()

    def show_normal(self):
        self.show()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event):
        # Instead of closing, hide to tray if enabled
        if self.tray_icon.isVisible():
            self.hide()
            self.tray_icon.showMessage(
                "KDE Plasma Sync",
                "Das Tool läuft im Hintergrund im System-Tray weiter.",
                QSystemTrayIcon.MessageIcon.Information,
                2000
            )
            event.ignore()
        else:
            event.accept()

    def on_tab_changed(self, index):
        if self.tabs.tabText(index) == "Backups" and not self.is_operation_running:
            self.refresh_backup_list()

    def update_timer_interval(self):
        if not self.config.get("periodic_check_enabled", True):
            self.check_timer.stop()
            return
        interval_ms = max(1, int(self.config.get("check_interval_hours", 2))) * 60 * 60 * 1000
        self.check_timer.start(interval_ms)

    def setup_fs_watcher(self):
        paths = self.fs_watcher.directories()
        if paths:
            self.fs_watcher.removePaths(paths)
        backup_dir = self.config.get("backup_dir", "")
        if backup_dir and os.path.isdir(backup_dir):
            self.fs_watcher.addPath(backup_dir)

    def on_backup_dir_changed(self, _path):
        # Debounce: während der Cloud-Client schreibt, feuern viele Events —
        # erst 5s nach dem letzten Event prüfen.
        if not self.is_operation_running:
            self.fs_debounce.start()

    def get_active_wallpaper_dirs(self):
        dirs = set()
        configs = [
            os.path.expanduser("~/.config/plasma-org.kde.plasma.desktop-appletsrc"),
            os.path.expanduser("~/.config/kscreenlockerrc")
        ]
        pattern = re.compile(r'^(?:Image|PreviewImage)=(.*)$')
        for cfg in configs:
            if not os.path.exists(cfg):
                continue
            try:
                with open(cfg, 'r', errors='replace') as f:
                    for line in f:
                        m = pattern.match(line.strip())
                        if m:
                            val = m.group(1)
                            if val.startswith("file://"):
                                from urllib.parse import unquote
                                val = unquote(val[7:])
                            if os.path.isabs(val) and os.path.exists(val):
                                dirs.add(os.path.dirname(val))
            except Exception:
                pass
        return list(dirs)

    def setup_config_watcher(self):
        paths = self.config_watcher.directories()
        if paths:
            self.config_watcher.removePaths(paths)

        if not self.config.get("auto_snapshot_enabled", False):
            return

        to_watch = [
            os.path.expanduser("~/.config"),
            os.path.expanduser("~/.local/share/plasma"),
            os.path.expanduser("~/.local/share/plasma/plasmoids"),
            os.path.expanduser("~/.local/share/plasma/desktoptheme"),
            os.path.expanduser("~/.local/share/wallpapers"),
        ]

        for wp_dir in self.get_active_wallpaper_dirs():
            if wp_dir not in to_watch:
                to_watch.append(wp_dir)

        valid_dirs = [d for d in to_watch if os.path.isdir(d)]
        if valid_dirs:
            self.config_watcher.addPaths(valid_dirs)

    def on_config_dir_changed(self, _path):
        import time
        if not self.config.get("auto_snapshot_enabled", False):
            return
        if self.is_operation_running:
            return
        if time.time() < self.restore_lockout_until:
            return

        quiet_seconds = max(10, int(self.config.get("auto_snapshot_quiet_seconds", 90)))
        now = time.time()
        # Die Ruhephase beginnt bei jedem Ereignis neu. Schreibt irgendein Programm
        # öfter als alle quiet_seconds in ~/.config, käme der Timer nie zum Ablauf.
        # Darum wird er nach spätestens max_wait Sekunden nicht mehr verlängert.
        max_wait = max(600, quiet_seconds * 6)
        if self.auto_snapshot_debounce.isActive():
            if self.first_change_ts and now - self.first_change_ts >= max_wait:
                return
        else:
            self.first_change_ts = now
        self.auto_snapshot_debounce.setInterval(quiet_seconds * 1000)
        self.auto_snapshot_debounce.start()

    def on_periodic_timer_timeout(self):
        self.background_update_check()
        if self.config.get("auto_snapshot_enabled", False):
            self.check_auto_snapshot()

    def check_auto_snapshot(self):
        import time
        if not self.config.get("auto_snapshot_enabled", False):
            return
        if self.is_operation_running:
            return
        if self.snapshot_dialog is not None:
            return
        if time.time() < self.restore_lockout_until:
            return

        min_interval_min = max(1, int(self.config.get("auto_snapshot_min_interval_minutes", 30)))
        last_auto_str = self.config.get("last_auto_snapshot", "")
        if last_auto_str:
            try:
                last_dt = datetime.fromisoformat(last_auto_str)
                elapsed_min = (datetime.now() - last_dt).total_seconds() / 60.0
                if elapsed_min < min_interval_min:
                    remaining_ms = int((min_interval_min - elapsed_min) * 60 * 1000)
                    if remaining_ms > 0 and not self.auto_snapshot_debounce.isActive():
                        self.auto_snapshot_debounce.start(min(remaining_ms, 60000))
                    return
            except Exception:
                pass

        # Mindestabstand seit der letzten Nachfrage (Prompt-Cooldown, damit der Nutzer nicht genervt wird)
        last_prompt_str = self.config.get("last_prompt_time", "")
        if last_prompt_str:
            try:
                last_pdt = datetime.fromisoformat(last_prompt_str)
                elapsed_pmin = (datetime.now() - last_pdt).total_seconds() / 60.0
                if elapsed_pmin < min_interval_min:
                    remaining_ms = int((min_interval_min - elapsed_pmin) * 60 * 1000)
                    if remaining_ms > 0 and not self.auto_snapshot_debounce.isActive():
                        self.auto_snapshot_debounce.start(min(remaining_ms, 60000))
                    return
            except Exception:
                pass

        if self.fingerprint_runner and self.fingerprint_runner.isRunning():
            return

        self.first_change_ts = 0.0
        self.fingerprint_runner = FingerprintRunner(
            SCRIPT_PATH, cwd=self.config.get("backup_dir"), state_out=STATE_FILE_NEW,
            ignore_dock=bool(self.config.get("auto_snapshot_ignore_dock", False))
        )
        self.fingerprint_runner.finished_fingerprint.connect(self.handle_auto_snapshot_fingerprint)
        self.fingerprint_runner.start()

    def promote_state_file(self, source=None):
        """Den frisch erhobenen Stand zum neuen Vergleichsstand machen."""
        source = source or STATE_FILE_NEW
        try:
            if os.path.exists(source):
                os.replace(source, STATE_FILE)
        except OSError:
            pass

    def handle_auto_snapshot_fingerprint(self, new_fp, returncode):
        if returncode != 0 or not new_fp:
            return

        last_fp = self.config.get("last_fingerprint", "")

        # Erstaktivierung: wenn last_fingerprint noch leer ist, nur speichern, kein Snapshot
        if not last_fp:
            self.config["last_fingerprint"] = new_fp
            config_manager.save_config(self.config)
            self.promote_state_file()
            write_change_log("baseline", new_fp=new_fp)
            return

        # Keine Änderung
        if new_fp == last_fp:
            self.promote_state_file()
            return

        # Änderung erkannt. Vor dem Sichern benennen, was sich geändert hat.
        result = diff_states(STATE_FILE, STATE_FILE_NEW)
        named, details = result if result else ([], {})
        what = format_change_list(named)

        if not what:
            # Der Hash weicht ab, aber es lässt sich nichts Benennbares finden (kein Widget,
            # kein Hintergrundbild, kein Design). Dann fragen wir nicht, sondern übernehmen
            # den Stand als neue Basis. Der Eintrag im Änderungsprotokoll hält fest, was war.
            self.config["last_fingerprint"] = new_fp
            config_manager.save_config(self.config)
            self.promote_state_file()
            write_change_log("ignored_unnamed", None, details, last_fp, new_fp)
            return

        if self.config.get("auto_snapshot_ask_first", True):
            self.show_snapshot_question(what, new_fp, details, last_fp)
            return

        write_change_log("snapshot_silent", what, details, last_fp, new_fp)
        self.pending_fingerprint = new_fp
        self.pending_change_text = what
        self.start_backup(silent=True)

    def show_snapshot_question(self, what, new_fp, details, last_fp):
        """Nicht-modale Nachfrage. Sie holt das Hauptfenster nicht nach vorn und blockiert
        die Ereignisschleife nicht, solange niemand antwortet."""
        if self.snapshot_dialog is not None:
            return

        frage = (
            f"Du hast {what} geändert.\n\nSoll jetzt ein neuer Snapshot angelegt werden?"
        )
        # Zeitstempel der Nachfrage sofort vermerken, um wiederholte Prompts zu blockieren
        self.config["last_prompt_time"] = datetime.now().isoformat()
        config_manager.save_config(self.config)
        write_change_log("asked", what, details, last_fp, new_fp)

        box = QMessageBox()
        box.setWindowTitle("Änderung erkannt")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(frage)
        yes_btn = box.addButton("Snapshot anlegen", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Ignorieren", QMessageBox.ButtonRole.RejectRole)
        box.setWindowModality(Qt.WindowModality.NonModal)
        box.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.snapshot_dialog = box
        self.snapshot_ctx = {
            "what": what, "new_fp": new_fp, "details": details,
            "last_fp": last_fp, "yes_btn": yes_btn,
        }
        box.finished.connect(self.on_snapshot_answer)
        box.show()

        if self.tray_icon.isVisible():
            self.tray_icon.showMessage(
                "KDE Plasma Sync", f"Du hast {what} geändert. Snapshot anlegen?",
                QSystemTrayIcon.MessageIcon.Information, 6000
            )

    def on_snapshot_answer(self, _result):
        box = self.snapshot_dialog
        ctx = getattr(self, "snapshot_ctx", None) or {}
        self.snapshot_dialog = None
        self.snapshot_ctx = None
        if box is None or not ctx:
            return
        accepted = box.clickedButton() is ctx["yes_btn"]
        box.deleteLater()

        if accepted and not self.is_operation_running:
            write_change_log("accepted", ctx["what"], ctx["details"], ctx["last_fp"], ctx["new_fp"])
            self.pending_fingerprint = ctx["new_fp"]
            self.pending_change_text = ctx["what"]
            self.start_backup(silent=True)
            return

        # Abgelehnt (oder gerade läuft eine andere Aktion): diesen Stand als Basis
        # übernehmen, sonst wird bei jeder Prüfung erneut wegen derselben Änderung gefragt.
        write_change_log("declined" if not accepted else "skipped_busy",
                         ctx["what"], ctx["details"], ctx["last_fp"], ctx["new_fp"])
        self.config["last_fingerprint"] = ctx["new_fp"]
        self.config["last_prompt_time"] = datetime.now().isoformat()
        config_manager.save_config(self.config)
        self.promote_state_file()
        self.status_bar_label.setText("Änderung erkannt — kein Snapshot angelegt.")

    def update_fingerprint_baseline_silently(self):
        # Läuft noch eine Aufnahme, nicht dieselbe Zwischendatei und denselben
        # QThread-Verweis überschreiben.
        if self._bg_fp_runner is not None and self._bg_fp_runner.isRunning():
            return

        def on_fp(fp, code):
            if code == 0 and fp:
                self.config["last_fingerprint"] = fp
                self.config["fingerprint_version"] = FINGERPRINT_VERSION
                config_manager.save_config(self.config)
                self.promote_state_file(STATE_FILE_BASE_NEW)

        self._bg_fp_runner = FingerprintRunner(
            SCRIPT_PATH, cwd=self.config.get("backup_dir"), state_out=STATE_FILE_BASE_NEW,
            ignore_dock=bool(self.config.get("auto_snapshot_ignore_dock", False))
        )
        self._bg_fp_runner.finished_fingerprint.connect(on_fp)
        self._bg_fp_runner.start()

    # ----------------- TABS SETUP -----------------

    def init_dashboard_tab(self):
        tab = QWidget()
        tab.setObjectName("dashboardTab")
        tab.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QVBoxLayout(tab)
        layout.setSpacing(15)

        # Header Info
        header_layout = QHBoxLayout()
        header_label = QLabel("KDE Plasma Synchronisation")
        header_label.setObjectName("headerLabel")
        sub_label = QLabel("Backup & Restore deiner Plasma 6 Einstellungen")
        sub_label.setObjectName("subHeaderLabel")
        
        info_vbox = QVBoxLayout()
        info_vbox.addWidget(header_label)
        info_vbox.addWidget(sub_label)
        header_layout.addLayout(info_vbox)
        
        # Logo or Theme Icon on the right
        icon_label = QLabel()
        icon_label.setPixmap(QIcon.fromTheme("preferences-desktop-wallpaper").pixmap(48, 48))
        header_layout.addWidget(icon_label)
        layout.addLayout(header_layout)

        # Status Card (Groupbox)
        status_group = QGroupBox("Status Übersicht")
        status_layout = QVBoxLayout(status_group)
        status_layout.setSpacing(8)

        self.lbl_status_desc = QLabel("Sync-Ordner:")
        self.lbl_status_val = QLabel("Bereit")
        self.lbl_status_val.setObjectName("statusValueLabel")

        self.lbl_last_backup = QLabel("Letztes angewendetes Backup:")
        self.lbl_last_backup_val = QLabel("Unbekannt")
        self.lbl_last_backup_val.setStyleSheet("font-weight: bold;")
        
        self.lbl_newest_backup = QLabel("Neuestes Backup im Sync-Ordner:")
        self.lbl_newest_backup_val = QLabel("Suche...")
        self.lbl_newest_backup_val.setStyleSheet("font-weight: bold;")

        self.lbl_last_auto_desc = QLabel("Letzter automatischer Snapshot:")
        self.lbl_last_auto_val = QLabel("Nie")
        self.lbl_last_auto_val.setStyleSheet("font-weight: bold;")

        status_layout.addWidget(self.lbl_status_desc)
        status_layout.addWidget(self.lbl_status_val)
        status_layout.addWidget(self.lbl_last_backup)
        status_layout.addWidget(self.lbl_last_backup_val)
        status_layout.addWidget(self.lbl_newest_backup)
        status_layout.addWidget(self.lbl_newest_backup_val)
        status_layout.addWidget(self.lbl_last_auto_desc)
        status_layout.addWidget(self.lbl_last_auto_val)
        layout.addWidget(status_group)

        # Actions Card
        actions_group = QGroupBox("Schnellaktionen")
        actions_layout = QHBoxLayout(actions_group)
        
        self.btn_backup = QPushButton("Jetzt sichern")
        self.btn_backup.setObjectName("primaryButton")
        self.btn_backup.setToolTip("Erstellt ein aktuelles Backup und legt es im Sync-Ordner ab — dein Cloud-Client lädt es automatisch hoch")
        self.btn_backup.clicked.connect(self.start_backup)
        
        self.btn_restore_latest = QPushButton("Neuestes Backup einspielen")
        self.btn_restore_latest.setToolTip("Spielt das aktuellste Backup aus dem Sync-Ordner ein")
        self.btn_restore_latest.clicked.connect(self.restore_latest_backup)
        
        self.btn_check_now = QPushButton("Auf Updates prüfen")
        self.btn_check_now.setToolTip("Sucht nach neueren Backups im Sync-Ordner")
        self.btn_check_now.clicked.connect(lambda: self.background_update_check(manual=True))

        actions_layout.addWidget(self.btn_backup)
        actions_layout.addWidget(self.btn_restore_latest)
        actions_layout.addWidget(self.btn_check_now)
        layout.addWidget(actions_group)
        
        layout.addStretch()
        self.tabs.addTab(tab, "Dashboard")
        
        self.update_dashboard_ui()

    def init_history_tab(self):
        tab = QWidget()
        tab.setObjectName("historyTab")
        tab.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QVBoxLayout(tab)
        
        header = QLabel("Verfügbare Backups")
        header.setObjectName("headerLabel")
        sub = QLabel("Liste aller Backup-Archive im Sync-Ordner")
        sub.setObjectName("subHeaderLabel")
        layout.addWidget(header)
        layout.addWidget(sub)

        self.list_backups = QListWidget()
        layout.addWidget(self.list_backups)

        btn_layout = QHBoxLayout()
        self.btn_refresh_history = QPushButton("Liste aktualisieren")
        self.btn_refresh_history.clicked.connect(self.refresh_backup_list)
        btn_layout.addWidget(self.btn_refresh_history)
        btn_layout.addStretch()
        
        layout.addLayout(btn_layout)
        self.tabs.addTab(tab, "Backups")


    def init_settings_tab(self):
        tab = QWidget()
        tab.setObjectName("settingsTab")
        tab.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QVBoxLayout(tab)
        
        header = QLabel("Einstellungen")
        header.setObjectName("headerLabel")
        sub = QLabel("Pfade und Synchronisations-Optionen konfigurieren")
        sub.setObjectName("subHeaderLabel")
        layout.addWidget(header)
        layout.addWidget(sub)

        form_group = QGroupBox("Synchronisation")
        form_layout = QFormLayout(form_group)
        
        # Path Field
        self.txt_backup_dir = QLineEdit()
        self.txt_backup_dir.setText(self.config.get("backup_dir", ""))
        btn_browse = QPushButton("Durchsuchen...")
        btn_browse.clicked.connect(self.browse_backup_dir)
        
        path_layout = QHBoxLayout()
        path_layout.addWidget(self.txt_backup_dir)
        path_layout.addWidget(btn_browse)
        form_layout.addRow("Backup-Ordner:", path_layout)

        hint = QLabel(
            "Egal welcher Cloud-Dienst: Wähle einfach einen Ordner, den dein "
            "Sync-Client (Nextcloud, Google Drive, OneDrive, Dropbox, Syncthing, rsync, …) "
            "automatisch synchronisiert. Dieses Programm beobachtet nur den lokalen Ordner — "
            "den Up-/Download übernimmt dein Client."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #a0a0b0; font-size: 11px;")
        form_layout.addRow("", hint)

        # Autostart Checkbox
        self.chk_autostart = QCheckBox("Mit System starten")
        self.chk_autostart.setToolTip("Startet diese GUI automatisch im Hintergrund beim Login.")
        self.chk_autostart.setChecked(self.config.get("autostart_enabled", True))
        form_layout.addRow("", self.chk_autostart)

        # Periodic check timer
        self.chk_periodic = QCheckBox("Zusätzlich regelmäßig im Hintergrund prüfen")
        self.chk_periodic.setToolTip(
            "Neue Backups werden ohnehin sofort erkannt, sobald sie im Ordner auftauchen. "
            "Diese Prüfung ist ein zusätzliches Sicherheitsnetz."
        )
        self.chk_periodic.setChecked(self.config.get("periodic_check_enabled", True))
        form_layout.addRow("", self.chk_periodic)

        self.spin_interval = QSpinBox()
        self.spin_interval.setRange(1, 24)
        self.spin_interval.setSuffix(" Stunden")
        self.spin_interval.setValue(max(1, int(self.config.get("check_interval_hours", 2))))
        form_layout.addRow("Prüfintervall:", self.spin_interval)

        layout.addWidget(form_group)

        # Gruppe: Automatische Snapshots
        auto_group = QGroupBox("Automatische Snapshots")
        auto_layout = QFormLayout(auto_group)

        self.chk_auto_snapshot = QCheckBox("Automatische Snapshots bei erkannter Änderung")
        self.chk_auto_snapshot.setToolTip("Erstellt automatisch einen Snapshot, wenn sich Einstellungen oder Designs ändern.")
        self.chk_auto_snapshot.setChecked(self.config.get("auto_snapshot_enabled", False))
        auto_layout.addRow("", self.chk_auto_snapshot)

        self.chk_ask_first = QCheckBox("Vorher fragen („Du hast … geändert, Snapshot anlegen?“)")
        self.chk_ask_first.setToolTip(
            "Fragt nach und benennt dabei, was sich geändert hat. "
            "Ohne Haken wird der Snapshot still im Hintergrund angelegt."
        )
        self.chk_ask_first.setChecked(self.config.get("auto_snapshot_ask_first", True))
        auto_layout.addRow("", self.chk_ask_first)

        self.chk_ignore_dock = QCheckBox("Dock-Anordnung ignorieren (Starter, Gruppen, Ordner)")
        self.chk_ignore_dock.setToolTip(
            "Änderungen an den angehefteten Starter-Listen, Gruppen und Ordnern des Docks lösen "
            "keine Nachfrage aus. Sie werden weiterhin mit jedem Snapshot gesichert."
        )
        self.chk_ignore_dock.setChecked(self.config.get("auto_snapshot_ignore_dock", False))
        auto_layout.addRow("", self.chk_ignore_dock)

        auto_hint = QLabel(
            "Erkennt Änderungen an KDE-Konfigurationen, Themes, Farbschemata und Wallpapers "
            "und legt nach einer Ruhephase einen Snapshot an.\n"
            "Hinweis: Ein vollständiger Snapshot ist mehrere hundert MB groß (enthält Themes & Configs) "
            "und wird von deinem konfigurierten Cloud-Client synchronisiert."
        )
        auto_hint.setWordWrap(True)
        auto_hint.setStyleSheet("color: #a0a0b0; font-size: 11px;")
        auto_layout.addRow("", auto_hint)

        self.spin_quiet_seconds = QSpinBox()
        self.spin_quiet_seconds.setRange(10, 600)
        self.spin_quiet_seconds.setSuffix(" Sekunden")
        self.spin_quiet_seconds.setValue(int(self.config.get("auto_snapshot_quiet_seconds", 90)))
        self.spin_quiet_seconds.setToolTip("Ruhephase nach der letzten Dateiänderung, bevor der Fingerabdruck berechnet wird.")
        auto_layout.addRow("Ruhephase:", self.spin_quiet_seconds)

        self.spin_min_interval = QSpinBox()
        self.spin_min_interval.setRange(5, 1440)
        self.spin_min_interval.setSuffix(" Minuten")
        self.spin_min_interval.setValue(int(self.config.get("auto_snapshot_min_interval_minutes", 30)))
        self.spin_min_interval.setToolTip("Mindestabstand zwischen zwei automatischen Snapshots, um Bandbreite und Speicher zu schonen.")
        auto_layout.addRow("Mindestabstand:", self.spin_min_interval)

        self.spin_keep_snapshots = QSpinBox()
        self.spin_keep_snapshots.setRange(0, 100)
        self.spin_keep_snapshots.setSuffix(" Snapshots")
        self.spin_keep_snapshots.setSpecialValueText("Unbegrenzt (0)")
        self.spin_keep_snapshots.setValue(int(self.config.get("keep_snapshots", 3)))
        self.spin_keep_snapshots.setToolTip("Anzahl der neuesten Snapshots, die im Sync-Ordner behalten werden (ältere werden rotiert).")
        auto_layout.addRow("Behalte Snapshots:", self.spin_keep_snapshots)

        self.chk_auto_restore = QCheckBox("Neue Snapshots automatisch ohne Nachfrage einspielen")
        self.chk_auto_restore.setToolTip("Spielt neu im Sync-Ordner eintreffende Snapshots von anderen Rechnern sofort automatisch ein. Ist dies deaktiviert, erscheint die Nachfrage: 'Neuer Snapshot gefunden, einspielen?'")
        self.chk_auto_restore.setChecked(self.config.get("auto_restore_enabled", False))
        auto_layout.addRow("", self.chk_auto_restore)

        layout.addWidget(auto_group)

        # Buttons
        btn_layout = QHBoxLayout()
        self.btn_save_settings = QPushButton("Speichern")
        self.btn_save_settings.setObjectName("primaryButton")
        self.btn_save_settings.clicked.connect(self.save_settings)
        btn_layout.addWidget(self.btn_save_settings)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)
        
        layout.addStretch()
        self.tabs.addTab(tab, "Einstellungen")

    # ----------------- ACTIONS & SLOTS -----------------

    def update_dashboard_ui(self):
        # Update last applied
        last_applied = self.config.get("last_applied_backup", "")
        if last_applied:
            self.lbl_last_backup_val.setText(last_applied)
        else:
            self.lbl_last_backup_val.setText("Keines (Noch nie synchronisiert)")

        # Update last auto snapshot
        last_auto = self.config.get("last_auto_snapshot", "")
        if hasattr(self, "lbl_last_auto_val"):
            if last_auto:
                try:
                    dt = datetime.fromisoformat(last_auto)
                    self.lbl_last_auto_val.setText(dt.strftime("%d.%m.%Y %H:%M:%S"))
                except Exception:
                    self.lbl_last_auto_val.setText(last_auto)
            else:
                self.lbl_last_auto_val.setText("Nie")

        # Verify the sync directory exists
        backup_dir = self.config.get("backup_dir", "")
        if not os.path.exists(backup_dir):
            # Nicht anlegen: Ist der Cloud-Ordner gerade nicht eingehängt, entstünde ein
            # lokaler Leerordner, der später mit dem Sync kollidiert. Das erste Backup
            # legt den Ordner selbst an (start_backup).
            self.lbl_status_val.setText("Backup-Ordner existiert nicht (wird beim ersten Backup angelegt)")
            self.lbl_status_val.setStyleSheet("color: #e0a030;")
            return


        self.lbl_status_val.setText("Bereit (Ordner wird überwacht)")
        self.lbl_status_val.setStyleSheet("color: #2ecc71;")
        if hasattr(self, "fs_watcher") and backup_dir not in self.fs_watcher.directories():
            self.setup_fs_watcher()

    def get_backup_files(self):
        backup_dir = self.config.get("backup_dir", "")
        if not os.path.exists(backup_dir):
            return []
        
        # Streng auf das Zeitstempel-Format prüfen, damit Konfliktkopien des
        # Cloud-Clients nicht als Snapshot gelten (gleiche Regel wie in prune).
        files = [f for f in os.listdir(backup_dir) if BACKUP_NAME_RE.match(f)]
        files.sort()  # Sorts chronologically YYYYMMDD_HHMMSS
        return files

    def run_startup_update_check(self):
        # Silent check on startup, does not open window unless updates exist
        self.background_update_check(manual=False)

    def background_update_check(self, manual=False):
        if self.is_operation_running:
            return

        files = self.get_backup_files()
        self.refresh_backup_list()
        if not files:
            self.lbl_newest_backup_val.setText("Keine Backups im Sync-Ordner gefunden")
            if manual:
                QMessageBox.information(
                    self, "Update-Check", 
                    "Es wurden keine Backups im konfigurierten Ordner gefunden."
                )
            return

        latest_backup = files[-1]

        # Sync-Schutz: Wenn die neueste Datei in den letzten 30s geschrieben wurde,
        # lädt der Cloud-Client sie evtl. noch herunter. Dann nicht sofort fragen,
        # sondern in 45s erneut prüfen.
        latest_path = os.path.join(self.config.get("backup_dir", ""), latest_backup)
        try:
            import time
            if time.time() - os.path.getmtime(latest_path) < 30:
                self.lbl_newest_backup_val.setText(f"{latest_backup} (wird synchronisiert …)")
                QTimer.singleShot(45000, self.background_update_check)
                return
        except OSError:
            pass

        self.lbl_newest_backup_val.setText(latest_backup)

        last_applied = self.config.get("last_applied_backup", "")
        last_notified = self.config.get("last_notified_backup", "")

        # If newest backup is newer than what was applied
        if latest_backup != last_applied and latest_backup > last_applied:
            # We have a new backup!
            # Ein abgelehnter Snapshot wird nur für die laufende Sitzung
            # zurückgestellt, nicht dauerhaft — beim nächsten Start wird wieder
            # gefragt, ob er eingespielt werden soll.
            if latest_backup != self.declined_this_session or manual:
                # Prüfen, ob automatische Wiederherstellung aktiviert ist
                if self.config.get("auto_restore_enabled", False):
                    if self.tray_icon.isVisible():
                        self.tray_icon.showMessage(
                            "Neuer Snapshot gefunden",
                            f"Neuer Snapshot gefunden. Spiele '{latest_backup}' automatisch ein …",
                            QSystemTrayIcon.MessageIcon.Information,
                            5000
                        )
                    self.start_restore(latest_backup, confirm=False)
                else:
                    # Show GUI if minimized
                    if not self.isVisible():
                        self.show_normal()

                    if self.tray_icon.isVisible():
                        self.tray_icon.showMessage(
                            "Neuer Snapshot gefunden",
                            f"Neuer Snapshot gefunden: '{latest_backup}'. Jetzt einspielen?",
                            QSystemTrayIcon.MessageIcon.Information,
                            7000
                        )

                    # Show Prompt dialog
                    btn = QMessageBox.question(
                        self, "Neuer Snapshot gefunden",
                        f"Es liegt ein neuer KDE-Snapshot in deinem Sync-Ordner vor:\n\n"
                        f"Datei: {latest_backup}\n\n"
                        f"Möchtest du diesen Snapshot jetzt einspielen?",
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                    )

                    if btn == QMessageBox.StandardButton.Yes:
                        self.start_restore(latest_backup, confirm=False)
                    else:
                        # Nur für diese Sitzung zurückstellen
                        self.declined_this_session = latest_backup
                        self.status_bar_label.setText(
                            f"Snapshot {latest_backup} nicht eingespielt."
                        )
        else:
            if manual:
                QMessageBox.information(
                    self, "Update-Check", 
                    "Deine KDE Plasma Einstellungen sind auf dem neuesten Stand!"
                )

    def refresh_backup_list(self):
        self.list_backups.clear()
        backup_dir = self.config.get("backup_dir", "")
        files = self.get_backup_files()
        
        # Sort in reverse order (newest first)
        for filename in reversed(files):
            item = QListWidgetItem(self.list_backups)
            widget = BackupItemWidget(filename, backup_dir)
            widget.restore_requested.connect(self.start_restore)
            widget.delete_requested.connect(self.delete_backup_prompt)
            item.setSizeHint(QSize(0, 65))
            self.list_backups.addItem(item)
            self.list_backups.setItemWidget(item, widget)

    def browse_backup_dir(self):
        dir_path = QFileDialog.getExistingDirectory(
            self, "Backup-Verzeichnis auswählen", 
            self.txt_backup_dir.text()
        )
        if dir_path:
            self.txt_backup_dir.setText(dir_path)

    def save_settings(self):
        new_dir = self.txt_backup_dir.text().strip()
        if not new_dir:
            QMessageBox.warning(self, "Fehler", "Bitte gib einen gültigen Pfad an.")
            return

        is_auto_enabled = self.chk_auto_snapshot.isChecked()

        self.config["backup_dir"] = new_dir
        self.config["autostart_enabled"] = self.chk_autostart.isChecked()
        self.config["periodic_check_enabled"] = self.chk_periodic.isChecked()
        self.config["check_interval_hours"] = int(self.spin_interval.value())
        self.config["auto_snapshot_enabled"] = is_auto_enabled
        self.config["auto_snapshot_ask_first"] = self.chk_ask_first.isChecked()
        self.config["auto_snapshot_quiet_seconds"] = int(self.spin_quiet_seconds.value())
        self.config["auto_snapshot_min_interval_minutes"] = int(self.spin_min_interval.value())
        self.config["keep_snapshots"] = int(self.spin_keep_snapshots.value())
        self.config["auto_restore_enabled"] = self.chk_auto_restore.isChecked()
        dock_flag_changed = bool(self.config.get("auto_snapshot_ignore_dock", False)) != self.chk_ignore_dock.isChecked()
        self.config["auto_snapshot_ignore_dock"] = self.chk_ignore_dock.isChecked()

        # Update autostart
        update_autostart_file(self.chk_autostart.isChecked())

        # Rotation nur nach Rückfrage: Sie löscht Dateien im Sync-Ordner, auch Snapshots,
        # die ein anderer Rechner dort abgelegt hat.
        keep = self.config["keep_snapshots"]
        if keep > 0 and os.path.isdir(new_dir):
            existing = [f for f in os.listdir(new_dir) if BACKUP_NAME_RE.match(f)]
            surplus = len(existing) - keep
            if surplus > 0:
                answer = QMessageBox.question(
                    self, "Alte Snapshots löschen?",
                    f"Es liegen {len(existing)} Snapshots im Ordner, behalten werden {keep}.\n\n"
                    f"Die {surplus} ältesten werden unwiderruflich gelöscht (auch im Cloud-Speicher, "
                    f"auch wenn sie von einem anderen Rechner stammen). Fortfahren?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                )
                if answer == QMessageBox.StandardButton.Yes:
                    try:
                        subprocess.run([SCRIPT_PATH, "prune", new_dir, str(keep)], check=False)
                    except Exception as e:
                        print(f"Error during prune: {e}")
                else:
                    # Nicht löschen, und die Einstellung nicht stillschweigend übernehmen
                    self.config["keep_snapshots"] = max(keep, len(existing))
                    self.spin_keep_snapshots.setValue(int(self.config["keep_snapshots"]))

        config_manager.save_config(self.config)
        self.update_dashboard_ui()
        self.refresh_backup_list()
        self.update_timer_interval()
        self.setup_fs_watcher()
        self.setup_config_watcher()

        # Der Dock-Schalter verändert den Fingerabdruck: Basis neu aufnehmen, sonst
        # fragt die GUI beim nächsten Lauf wegen des Schalters selbst.
        if is_auto_enabled and (dock_flag_changed or not self.config.get("last_fingerprint")):
            self.update_fingerprint_baseline_silently()

        QMessageBox.information(self, "Erfolg", "Einstellungen wurden erfolgreich gespeichert.")

    def copy_log_to_clipboard(self):
        clipboard = QApplication.clipboard()
        clipboard.setText(self.log_output.toPlainText())
        self.status_bar_label.setText("Protokoll in die Zwischenablage kopiert.")

    # ----------------- EXECUTION RUNNERS -----------------

    def set_operation_state(self, running, status_text=""):
        self.is_operation_running = running
        self.btn_backup.setEnabled(not running)
        self.btn_restore_latest.setEnabled(not running)
        self.btn_check_now.setEnabled(not running)
        self.btn_refresh_history.setEnabled(not running)
        self.progress_bar.setVisible(running)
        self.btn_cancel_op.setVisible(running and not self.operation_card.isHidden())
        self.btn_cancel_op.setEnabled(True)

        if status_text:
            self.status_bar_label.setText(status_text)
            
        if not running:
            # Refresh lists
            self.refresh_backup_list()
            self.update_dashboard_ui()

    def start_backup(self, silent=False):
        if not self.verify_script_exists():
            return

        self.is_silent_backup = silent

        self.inline_log_output.clear()
        mode_text = "Automatischer Hintergrund-Snapshot" if silent else "Backup"
        msg = f"--- {mode_text}-Start am {datetime.now().strftime('%d.%m.%Y %H:%M:%S')} ---"
        self.inline_log_output.appendPlainText(msg)

        if not silent:
            self.init_operation_ui("Backup wird erstellt...", BACKUP_STEPS)
        
        backup_dir = self.config.get("backup_dir")
        os.makedirs(backup_dir, exist_ok=True)

        cmd = [SCRIPT_PATH, "backup"]
        status_msg = "Erstelle automatischen Snapshot..." if silent else "Erstelle Backup..."
        self.set_operation_state(True, status_msg)
        
        self.runner = ScriptRunner(cmd, backup_dir)
        self.runner.output_received.connect(self.handle_script_output)
        self.runner.finished_status.connect(self.handle_backup_finished)
        self.runner.start()

    def handle_backup_finished(self, returncode):
        self.set_operation_state(False)
        is_silent = getattr(self, "is_silent_backup", False)
        self.is_silent_backup = False

        if returncode == 0:
            self.inline_log_output.appendPlainText("\n--- Backup erfolgreich erstellt! ---")
            self.status_bar_label.setText("Backup erfolgreich erstellt.")
            if not is_silent:
                self.on_progress_done("Backup erfolgreich erstellt!", "Das Backup wurde im Sync-Ordner abgelegt.")
            
            # Find the new file
            files = self.get_backup_files()
            if files:
                new_backup = files[-1]
                self.config["last_applied_backup"] = new_backup
                self.config["last_notified_backup"] = new_backup

            now_iso = datetime.now().isoformat()
            self.config["last_prompt_time"] = now_iso
            if is_silent:
                self.config["last_auto_snapshot"] = now_iso
                if hasattr(self, "pending_fingerprint") and self.pending_fingerprint:
                    self.config["last_fingerprint"] = self.pending_fingerprint
                    self.pending_fingerprint = ""
                    self.promote_state_file()
                else:
                    self.update_fingerprint_baseline_silently()
            else:
                self.update_fingerprint_baseline_silently()

            # Rotation (prune) ausführen
            keep = int(self.config.get("keep_snapshots", 10))
            if keep > 0:
                backup_dir = self.config.get("backup_dir", "")
                try:
                    subprocess.run([SCRIPT_PATH, "prune", backup_dir, str(keep)], check=False)
                except Exception as e:
                    print(f"Error during prune: {e}")

            config_manager.save_config(self.config)
            self.update_dashboard_ui()
            self.refresh_backup_list()

            if is_silent:
                if self.tray_icon.isVisible():
                    was = getattr(self, "pending_change_text", "")
                    self.pending_change_text = ""
                    text = (
                        f"Snapshot angelegt — geändert wurde {was}."
                        if was else "Automatischer Snapshot erfolgreich erstellt."
                    )
                    self.tray_icon.showMessage(
                        "KDE Plasma Sync",
                        text,
                        QSystemTrayIcon.MessageIcon.Information,
                        4000
                    )
            else:
                QMessageBox.information(
                    self, "Backup erfolgreich",
                    "Dein KDE Plasma Backup wurde erstellt und im Sync-Ordner abgelegt.\n"
                    "Dein Cloud-Client (Nextcloud, Google Drive, OneDrive, Syncthing, …) synchronisiert es automatisch."
                )
        else:
            self.inline_log_output.appendPlainText(f"\n--- Backup fehlgeschlagen (Exit Code: {returncode}) ---")
            self.status_bar_label.setText("Backup fehlgeschlagen.")
            if not is_silent:
                self.lbl_phase_icon.setText("❌")
                self.lbl_phase_title.setText("Backup fehlgeschlagen")
                self.lbl_phase_detail.setText("Ein Fehler ist aufgetreten. Klicke auf 'Protokoll anzeigen' für Details.")
                self.btn_close_card.setVisible(True)
                self.console_widget.setVisible(True)
                self.btn_toggle_protocol.setText("▲ Protokoll ausblenden")
            if is_silent:
                if self.tray_icon.isVisible():
                    self.tray_icon.showMessage(
                        "KDE Plasma Sync",
                        "Automatischer Snapshot fehlgeschlagen. Bitte prüfe das Protokoll.",
                        QSystemTrayIcon.MessageIcon.Warning,
                        5000
                    )
            else:
                QMessageBox.critical(
                    self, "Backup fehlgeschlagen",
                    "Das Erstellen des Backups ist fehlgeschlagen. Klicke auf 'Protokoll anzeigen' für Details."
                )

    def restore_latest_backup(self):
        files = self.get_backup_files()
        if not files:
            QMessageBox.warning(self, "Keine Backups", "Es wurden keine Backups zum Wiederherstellen gefunden.")
            return
        
        latest = files[-1]
        self.start_restore(latest)

    def start_restore(self, filename, confirm=True):
        if not self.verify_script_exists():
            return
            
        if confirm:
            btn = QMessageBox.question(
                self, "Backup wiederherstellen",
                f"Möchtest du das Backup '{filename}' wirklich einspielen?\n\n"
                f"WICHTIG: Dabei werden deine aktuellen Plasma-Einstellungen überschrieben "
                f"und die Benutzeroberfläche (Plasmashell) wird neu gestartet.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if btn != QMessageBox.StandardButton.Yes:
                return

        self.inline_log_output.clear()
        msg = f"--- Restore-Start für {filename} am {datetime.now().strftime('%d.%m.%Y %H:%M:%S')} ---"
        self.inline_log_output.appendPlainText(msg)

        self.init_operation_ui(f"Backup wird eingespielt: {filename}", RESTORE_STEPS)
        
        backup_dir = self.config.get("backup_dir")
        filepath = os.path.join(backup_dir, filename)

        cmd = [SCRIPT_PATH, "restore", filepath]
        self.set_operation_state(True, "Stelle Backup wieder her...")
        
        # Ohne Rückfrage eingespielte Snapshots (Auto-Restore) installieren keine Pakete:
        # Ein Passwortdialog aus dem Nichts wäre das Gegenteil von "ohne Nachfrage".
        env = {} if confirm else {"KDE_SYNC_NO_SUDO": "1"}
        self.runner = ScriptRunner(cmd, backup_dir, env=env)
        self.runner.output_received.connect(self.handle_script_output)
        self.runner.password_requested.connect(self.prompt_sudo_password)
        self.runner.finished_status.connect(lambda code: self.handle_restore_finished(code, filename))
        self.runner.start()

    def cancel_operation(self):
        """Bricht ein laufendes Backup oder Einspielen ab."""
        if self.runner and self.runner.isRunning():
            self.btn_cancel_op.setEnabled(False)
            self.status_bar_label.setText("Breche ab …")
            self.runner.stop()

    def prompt_sudo_password(self):
        dialog = PasswordDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            password = dialog.password_input.text()
            if self.runner:
                self.runner.set_password(password)
        else:
            # Ohne Passwort würde sudo ewig weiterwarten und die GUI bliebe gesperrt.
            if self.runner:
                self.runner.stop()

    def handle_script_output(self, text):
        cleaned = clean_ansi(text)
        for line in cleaned.splitlines():
            line_str = line.strip()
            if "::PROGRESS::" in line_str:
                idx = line_str.find("::PROGRESS::")
                marker = line_str[idx + len("::PROGRESS::"):]
                parts = marker.split("::")
                if len(parts) >= 2 and parts[0] == "DONE":
                    self.on_progress_done(parts[1], parts[2] if len(parts) > 2 else "")
                    self.inline_log_output.appendPlainText(f"\n✅ {parts[1]}")
                elif len(parts) >= 4:
                    try:
                        step = int(parts[0])
                        total = int(parts[1])
                        phase = parts[2]
                        detail = parts[3]
                        self.on_progress_step(step, total, phase, detail)
                        self.inline_log_output.appendPlainText(f"[{step}/{total}] {phase}: {detail}")
                    except ValueError:
                        self.inline_log_output.appendPlainText(line_str)
            else:
                if line_str:
                    self.inline_log_output.appendPlainText(line)
        self.inline_log_output.ensureCursorVisible()

    def handle_restore_finished(self, returncode, filename):
        self.set_operation_state(False)
        if returncode == 0:
            self.inline_log_output.appendPlainText("\n--- Backup erfolgreich wiederhergestellt! ---")
            self.status_bar_label.setText("Restore erfolgreich durchgeführt.")
            self.on_progress_done("Wiederherstellung erfolgreich!", "Alle Plasma-Einstellungen und Themes wurden geladen.")
            
            # Save state
            self.config["last_applied_backup"] = filename
            self.config["last_notified_backup"] = filename
            self.config["last_prompt_time"] = datetime.now().isoformat()
            config_manager.save_config(self.config)
            self.update_dashboard_ui()

            # Schleife 1 verhindern: Nach Restore für quiet_seconds * 2 keine Auto-Snapshots,
            # danach den neuen Fingerabdruck stumm als last_fingerprint speichern.
            import time
            quiet_seconds = max(10, int(self.config.get("auto_snapshot_quiet_seconds", 90)))
            self.restore_lockout_until = time.time() + (quiet_seconds * 2)
            QTimer.singleShot(quiet_seconds * 2 * 1000, self.update_fingerprint_baseline_silently)

            QMessageBox.information(
                self, "Restore erfolgreich",
                "KDE Plasma Backup wurde erfolgreich wiederhergestellt.\n\n"
                "Bitte melde dich einmal vom System ab und wieder an, damit alle "
                "Themes (Kvantum, GTK) vollständig angewendet werden!"
            )
        else:
            self.inline_log_output.appendPlainText(f"\n--- Restore fehlgeschlagen (Exit Code: {returncode}) ---")
            self.status_bar_label.setText("Restore fehlgeschlagen.")
            self.lbl_phase_icon.setText("❌")
            self.lbl_phase_title.setText("Wiederherstellung fehlgeschlagen")
            self.lbl_phase_detail.setText("Ein Fehler ist aufgetreten. Klicke auf 'Protokoll anzeigen' für Details.")
            self.btn_close_card.setVisible(True)
            self.console_widget.setVisible(True)
            self.btn_toggle_protocol.setText("▲ Protokoll ausblenden")
            QMessageBox.critical(
                self, "Restore fehlgeschlagen",
                "Die Wiederherstellung ist fehlgeschlagen. Klicke auf 'Protokoll anzeigen' für Details."
            )

    def delete_backup_prompt(self, filename):
        btn = QMessageBox.question(
            self, "Backup löschen",
            f"Möchtest du das Backup '{filename}' wirklich unwiderruflich aus dem Sync-Ordner löschen?\n\n"
            f"Dein Cloud-Client entfernt es dann auch aus der Cloud.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if btn == QMessageBox.StandardButton.Yes:
            backup_dir = self.config.get("backup_dir")
            filepath = os.path.join(backup_dir, filename)
            try:
                if os.path.exists(filepath):
                    os.remove(filepath)
                self.refresh_backup_list()
                self.background_update_check() # Recalculate status
                self.status_bar_label.setText(f"Backup {filename} gelöscht.")
            except Exception as e:
                QMessageBox.critical(self, "Fehler", f"Konnte Datei nicht löschen: {e}")


def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    
    # Parse args
    autostart_check = "--minimized" in sys.argv or "--autostart" in sys.argv

    # Check if config says autostart is disabled, if we started minimized.
    # This prevents running in tray if user disabled it but desktop file was not deleted.
    config = config_manager.load_config()
    if autostart_check and not config.get("autostart_enabled", True):
        sys.exit(0)

    gui = KdeSyncGUI(autostart_check=autostart_check)

    if not autostart_check:
        gui.show()
    else:
        # Just notify if running in background
        print("KDE Plasma Backup GUI im Hintergrund gestartet.")
        
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

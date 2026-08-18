import ctypes
import json
import logging
import os
import sys
import tempfile
import threading
import time
from typing import Optional

import keyboard
import psutil
import pydirectinput
import win32con
import win32gui
import win32process
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QScrollArea,
    QSlider,
    QSplitter,
    QVBoxLayout,
    QWidget,
    QPushButton,
    QTextEdit,
    QComboBox,
    QCheckBox,
    QSizePolicy,
)

import paths
import updater

CURRENT_VERSION = "1.3.0"
LOG_PATH = paths.LOG_PATH
SHEET_LIBRARY_FILE_NAME = paths.SHEET_LIBRARY_FILE_NAME
SHEET_FILE_EXTENSION = ".piano-sheet.json"
MAX_SHEET_FILE_BYTES = 1_000_000

logging.basicConfig(
    filename=str(LOG_PATH),
    filemode="w",
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

log = logging.getLogger("piano")

APP_BG = "#070b12"
APP_PANEL = "#111a24"
APP_PANEL_ALT = "#151f2b"
APP_BORDER = "#2b3746"
APP_GLOW = "#70e0a4"
APP_ACCENT = "#6ea8fe"
APP_TEXT = "#edf3ff"
APP_MUTED = "#8d99ab"
APP_SUCCESS = "#59d98a"
APP_DANGER = "#ff6b76"

DEFAULT_TEMPO = 120
MIN_TEMPO = 30
MAX_TEMPO = 300
START_HOTKEY = "f6"
STOP_HOTKEY = "f7"
PAUSE_HOTKEY = "f9"
EXIT_HOTKEY = "f8"
FOCUS_SETTLE_DELAY = 0.03

SHIFT_SYMBOLS = {
    "!": ("1", True),
    "@": ("2", True),
    "#": ("3", True),
    "$": ("4", True),
    "%": ("5", True),
    "^": ("6", True),
    "&": ("7", True),
    "*": ("8", True),
    "(": ("9", True),
    ")": ("0", True),
    "_": ("-", True),
    "+": ("=", True),
    "{": ("[", True),
    "}": ("]", True),
    "|": ("\\", True),
    ":": (";", True),
    '"': ("'", True),
    "<": (",", True),
    ">": (".", True),
    "?": ("/", True),
    "~": ("`", True),
}

DIRECT_KEYS = set(
    "abcdefghijklmnopqrstuvwxyz"
    "0123456789"
    "`-=[]\\;',./"
)


def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        log.exception("admin check failed")
        return False


def relaunch_as_admin():
    try:
        params = " ".join(f'"{arg}"' for arg in sys.argv)
        result = ctypes.windll.shell32.ShellExecuteW(
            None,
            "runas",
            sys.executable,
            params,
            None,
            1,
        )
        log.info("relaunch_as_admin ShellExecuteW result=%s", result)
        return result > 32
    except Exception:
        log.exception("relaunch_as_admin failed")
        return False


pydirectinput.PAUSE = 0
pydirectinput.FAILSAFE = True


class WindowTarget:
    def __init__(self, hwnd, pid, title, exe_name):
        self.hwnd = hwnd
        self.pid = pid
        self.title = title
        self.exe_name = exe_name

    def __str__(self):
        return f"{self.exe_name} - {self.title}"

    def is_alive(self):
        if not win32gui.IsWindow(self.hwnd):
            return False
        try:
            return psutil.pid_exists(self.pid)
        except Exception:
            return False

    def focus(self):
        if not self.is_alive():
            return False, "window no longer exists"
        try:
            if win32gui.IsIconic(self.hwnd):
                win32gui.ShowWindow(self.hwnd, win32con.SW_RESTORE)
            keyboard.press("alt")
            keyboard.release("alt")
            win32gui.SetForegroundWindow(self.hwnd)
            time.sleep(FOCUS_SETTLE_DELAY)
            if win32gui.GetForegroundWindow() == self.hwnd:
                return True, ""
            return False, "windows blocked the focus switch (foreground lock)"
        except Exception as error:
            return False, f"focus() raised: {error}"


def list_windows():
    results = []
    seen_pids_titles = set()

    def callback(hwnd, _extra):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd)
        if not title.strip():
            return
        rect = win32gui.GetWindowRect(hwnd)
        if rect[2] - rect[0] <= 0 or rect[3] - rect[1] <= 0:
            return
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            process = psutil.Process(pid)
            exe_name = process.name()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return
        except Exception:
            return

        key = (pid, title)
        if key in seen_pids_titles:
            return
        seen_pids_titles.add(key)
        results.append(WindowTarget(hwnd, pid, title, exe_name))

    win32gui.EnumWindows(callback, None)
    results.sort(key=lambda w: w.exe_name.lower())
    return results


class SheetLibrary:
    DEFAULT_SHEET_NAME = "Untitled sheet"

    def __init__(self):
        self.storage_directory = str(paths.APP_DATA_DIR)
        self.storage_path = str(paths.SHEET_LIBRARY_PATH)
        self.sheets = {self.DEFAULT_SHEET_NAME: ""}
        self.active_sheet_name = self.DEFAULT_SHEET_NAME
        self.settings = {}
        self.load()

    def load(self):
        try:
            with open(self.storage_path, "r", encoding="utf-8") as handle:
                content = json.load(handle)
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError):
            log.exception("could not load sheet library")
            return
        if not isinstance(content, dict):
            return
        sheets = content.get("sheets")
        if not isinstance(sheets, dict) or not sheets:
            return
        valid_sheets = {
            name: sheet_content
            for name, sheet_content in sheets.items()
            if isinstance(name, str) and isinstance(sheet_content, str)
        }
        if not valid_sheets:
            return
        self.sheets = valid_sheets
        active_sheet_name = content.get("active_sheet_name")
        if active_sheet_name in self.sheets:
            self.active_sheet_name = active_sheet_name
        else:
            self.active_sheet_name = next(iter(self.sheets))
        settings = content.get("settings", {})
        if isinstance(settings, dict):
            self.settings = settings

    def save(self):
        try:
            os.makedirs(self.storage_directory, exist_ok=True)
            temporary_path = f"{self.storage_path}.tmp"
            with open(temporary_path, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "active_sheet_name": self.active_sheet_name,
                        "settings": self.settings,
                        "sheets": self.sheets,
                    },
                    handle,
                    ensure_ascii=False,
                    indent=2,
                )
            os.replace(temporary_path, self.storage_path)
            return True
        except OSError:
            log.exception("could not save sheet library")
            return False

    def get_sheet_names(self):
        return list(self.sheets)

    def get_sheet_content(self, sheet_name):
        return self.sheets.get(sheet_name, "")

    def get_active_sheet_name(self):
        return self.active_sheet_name

    def set_active_sheet_name(self, sheet_name):
        if sheet_name not in self.sheets:
            return False
        self.active_sheet_name = sheet_name
        return True

    def set_sheet_content(self, sheet_name, content):
        if sheet_name not in self.sheets:
            return False
        self.sheets[sheet_name] = content
        return True

    def add_sheet(self, sheet_name):
        normalized_name = sheet_name.strip()
        if not normalized_name or normalized_name in self.sheets:
            return False
        self.sheets[normalized_name] = ""
        self.active_sheet_name = normalized_name
        return True

    def rename_sheet(self, sheet_name, new_name):
        normalized_name = new_name.strip()
        if (
            sheet_name not in self.sheets
            or not normalized_name
            or normalized_name in self.sheets
        ):
            return False
        sheet_content = self.sheets.pop(sheet_name)
        self.sheets[normalized_name] = sheet_content
        self.active_sheet_name = normalized_name
        return True

    def delete_sheet(self, sheet_name):
        if sheet_name not in self.sheets or len(self.sheets) == 1:
            return False
        del self.sheets[sheet_name]
        self.active_sheet_name = next(iter(self.sheets))
        return True

    def get_setting(self, name, default):
        return self.settings.get(name, default)

    def set_settings(self, settings):
        self.settings = settings


class SheetFileCodec:
    FORMAT_NAME = "virtual-piano-sheet"
    FORMAT_VERSION = 1

    @classmethod
    def create_export(cls, sheet_name, sheet_content):
        return {
            "format": cls.FORMAT_NAME,
            "name": sheet_name,
            "sheet": sheet_content,
            "version": cls.FORMAT_VERSION,
        }

    @classmethod
    def load(cls, file_path):
        try:
            if os.path.getsize(file_path) > MAX_SHEET_FILE_BYTES:
                return None, "sheet files must be smaller than 1 MB"
            with open(file_path, "r", encoding="utf-8-sig") as handle:
                content = handle.read()
        except OSError as error:
            return None, f"could not read the file: {error}"
        if file_path.lower().endswith(".txt"):
            return {"name": os.path.splitext(os.path.basename(file_path))[0], "sheet": content}, None
        try:
            document = json.loads(content)
        except json.JSONDecodeError as error:
            return None, f"invalid sheet file: {error.msg}"
        if not isinstance(document, dict):
            return None, "invalid sheet file: expected an object"
        if document.get("format") != cls.FORMAT_NAME:
            return None, "this file is not a virtual piano sheet"
        if document.get("version") != cls.FORMAT_VERSION:
            return None, "this sheet file uses an unsupported version"
        sheet_name = document.get("name")
        sheet_content = document.get("sheet")
        if not isinstance(sheet_name, str) or not isinstance(sheet_content, str):
            return None, "invalid sheet file: missing sheet name or content"
        return {"name": sheet_name, "sheet": sheet_content}, None


class SheetParser:
    @staticmethod
    def key_to_action(char):
        if char.isalpha():
            return char.lower(), char.isupper()
        if char in DIRECT_KEYS:
            return char, False
        if char in SHIFT_SYMBOLS:
            return SHIFT_SYMBOLS[char]
        return None

    @classmethod
    def parse(cls, text):
        events = []
        lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        for line_number, line in enumerate(lines):
            i = 0
            while i < len(line):
                char = line[i]
                if char == " ":
                    events.append({"type": "rest", "length": 0.35, "source": " ", "line": line_number})
                    i += 1
                    continue
                if char == "-":
                    count = 0
                    while i < len(line) and line[i] == "-":
                        count += 1
                        i += 1
                    events.append({"type": "rest", "length": float(count), "source": "-" * count, "line": line_number})
                    continue
                if char == "[":
                    end = line.find("]", i + 1)
                    if end == -1:
                        raise ValueError(f"missing ] on line {line_number + 1}")
                    contents = line[i + 1:end]
                    actions = []
                    for item in contents:
                        if item.isspace():
                            continue
                        action = cls.key_to_action(item)
                        if action is None:
                            raise ValueError(f"unsupported character {item!r} on line {line_number + 1}")
                        actions.append(action)
                    if not actions:
                        raise ValueError(f"empty chord on line {line_number + 1}")
                    events.append({"type": "chord", "actions": actions, "source": line[i:end + 1], "line": line_number})
                    i = end + 1
                    continue
                if char == "{":
                    end = line.find("}", i + 1)
                    if end == -1:
                        raise ValueError(f"missing }} on line {line_number + 1}")
                    contents = line[i + 1:end]
                    actions = []
                    for item in contents:
                        if item.isspace():
                            continue
                        action = cls.key_to_action(item)
                        if action is None:
                            raise ValueError(f"unsupported character {item!r} on line {line_number + 1}")
                        actions.append(action)
                    if not actions:
                        raise ValueError(f"empty run on line {line_number + 1}")
                    events.append({"type": "run", "actions": actions, "source": line[i:end + 1], "line": line_number})
                    i = end + 1
                    continue
                action = cls.key_to_action(char)
                if action is None:
                    raise ValueError(f"unsupported character {char!r} on line {line_number + 1}")
                events.append({"type": "note", "actions": [action], "source": char, "line": line_number})
                i += 1
        return events


class Player:
    HOLD_RATIO = 0.9
    SLEEP_SLACK = 0.0015

    def __init__(self, on_note, on_error, on_finished):
        self.events = []
        self.playing = False
        self.stop_requested = False
        self.paused = False
        self.thread = None
        self.on_note = on_note
        self.on_error = on_error
        self.on_finished = on_finished
        self.target = None
        self.state_changed = threading.Condition()

    @staticmethod
    def hold_action(action):
        key, shift = action
        if shift:
            pydirectinput.keyDown("shift")
            pydirectinput.keyDown(key)
        else:
            pydirectinput.keyDown(key)

    @staticmethod
    def release_action(action):
        key, shift = action
        if shift:
            pydirectinput.keyUp(key)
            pydirectinput.keyUp("shift")
        else:
            pydirectinput.keyUp(key)

    @classmethod
    def press_chord_down(cls, actions):
        normal = [a for a in actions if not a[1]]
        shifted = [a for a in actions if a[1]]
        if shifted:
            pydirectinput.keyDown("shift")
            for key, _ in shifted:
                pydirectinput.keyDown(key)
        for key, _ in normal:
            pydirectinput.keyDown(key)

    @classmethod
    def press_chord_up(cls, actions):
        normal = [a for a in actions if not a[1]]
        shifted = [a for a in actions if a[1]]
        for key, _ in reversed(normal):
            pydirectinput.keyUp(key)
        for key, _ in reversed(shifted):
            pydirectinput.keyUp(key)
        if shifted:
            pydirectinput.keyUp("shift")

    @staticmethod
    def release_everything():
        for key in "abcdefghijklmnopqrstuvwxyz0123456789`-=[]\\;',./shift":
            try:
                pydirectinput.keyUp(key)
            except Exception:
                pass

    def stop(self):
        with self.state_changed:
            self.stop_requested = True
            self.paused = False
            self.state_changed.notify_all()
        self.release_everything()

    def pause(self):
        with self.state_changed:
            if not self.playing or self.paused:
                return False
            self.paused = True
            self.state_changed.notify_all()
        self.release_everything()
        return True

    def resume(self):
        with self.state_changed:
            if not self.playing or not self.paused:
                return False
            self.paused = False
            self.state_changed.notify_all()
        return True

    def start(self, events, start_index, end_index, repeat_count, tempo, target):
        if self.playing:
            return
        self.events = events
        self.target = target
        self.playing = True
        self.stop_requested = False
        self.paused = False
        self.thread = threading.Thread(
            target=self._run,
            args=(start_index, end_index, repeat_count, tempo),
            daemon=True,
        )
        self.thread.start()

    def _ensure_focus(self):
        if self.target is None:
            return True
        if not self.target.is_alive():
            self.on_error("target window closed")
            return False
        current_fg = win32gui.GetForegroundWindow()
        if current_fg != self.target.hwnd:
            success, reason = self.target.focus()
            if not success:
                self.on_error(f"stopped: {reason}")
                return False
        return True

    def _sleep_until(self, clock_start, target_beats, beat_seconds):
        while True:
            with self.state_changed:
                while self.paused and not self.stop_requested:
                    pause_started = time.perf_counter()
                    self.state_changed.wait()
                    clock_start += time.perf_counter() - pause_started
                if self.stop_requested:
                    return None
                target_time = clock_start + (target_beats * beat_seconds)
                remaining = target_time - time.perf_counter() - self.SLEEP_SLACK
                if remaining <= 0:
                    return clock_start
                self.state_changed.wait(timeout=remaining)

    @staticmethod
    def _event_beats(event):
        if event["type"] == "rest":
            return event["length"] if event["source"] != " " else 0.5
        return 1.0

    def _run(self, start_index, end_index, repeat_count, tempo):
        beat_seconds = 60.0 / tempo
        beat_offsets = [0.0] * len(self.events)
        cursor = 0.0
        for index, event in enumerate(self.events):
            beat_offsets[index] = cursor
            cursor += self._event_beats(event)
        try:
            completed_plays = 0
            while repeat_count == 0 or completed_plays < repeat_count:
                clock_start = time.perf_counter() - beat_offsets[start_index] * beat_seconds
                for index in range(start_index, end_index + 1):
                    if self.stop_requested:
                        break
                    event_start_beats = beat_offsets[index]
                    clock_start = self._sleep_until(
                        clock_start,
                        event_start_beats,
                        beat_seconds,
                    )
                    if clock_start is None:
                        break
                    event = self.events[index]
                    event_beats = self._event_beats(event)
                    self.on_note(index)
                    if not self._ensure_focus():
                        self.stop()
                        break
                    if event["type"] == "rest":
                        clock_start = self._sleep_until(clock_start, event_start_beats + event_beats, beat_seconds)
                    elif event["type"] == "note":
                        action = event["actions"][0]
                        self.hold_action(action)
                        clock_start = self._sleep_until(clock_start, event_start_beats + event_beats * self.HOLD_RATIO, beat_seconds)
                        if clock_start is None:
                            break
                        self.release_action(action)
                        clock_start = self._sleep_until(clock_start, event_start_beats + event_beats, beat_seconds)
                    elif event["type"] == "chord":
                        self.press_chord_down(event["actions"])
                        clock_start = self._sleep_until(clock_start, event_start_beats + event_beats * self.HOLD_RATIO, beat_seconds)
                        if clock_start is None:
                            break
                        self.press_chord_up(event["actions"])
                        clock_start = self._sleep_until(clock_start, event_start_beats + event_beats, beat_seconds)
                    elif event["type"] == "run":
                        actions = event["actions"]
                        per_key_beats = event_beats / max(len(actions), 1)
                        for key_index, action in enumerate(actions):
                            if self.stop_requested:
                                break
                            self.hold_action(action)
                            key_start = event_start_beats + key_index * per_key_beats
                            clock_start = self._sleep_until(clock_start, key_start + per_key_beats * self.HOLD_RATIO, beat_seconds)
                            if clock_start is None:
                                break
                            self.release_action(action)
                            clock_start = self._sleep_until(clock_start, key_start + per_key_beats, beat_seconds)
                    if self.stop_requested or clock_start is None:
                        break
                if self.stop_requested or clock_start is None:
                    break
                completed_plays += 1
            if not self.stop_requested:
                log.info("playback reached end of selection")
        except Exception:
            log.exception("playback crashed")
            self.on_error("playback crashed - check the app log")
        finally:
            was_stopped = self.stop_requested
            self.release_everything()
            self.playing = False
            self.stop_requested = False
            self.paused = False
            self.on_finished(was_stopped)


class App(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("virtual piano player")
        self.resize(1100, 780)
        self.setMinimumSize(860, 620)
        self.setStyleSheet(f"background: {APP_BG}; color: {APP_TEXT}; font-family: 'Segoe UI';")

        self.events = []
        self.note_buttons = {}
        self.selected_event = 0
        self.playback_start_index = 0
        self.editing = True
        self.closing = False
        self.sheet_library = SheetLibrary()
        self.editor_save_timer = None
        self.is_loading_sheet = False
        self.sidebar_visible = self.sheet_library.get_setting("sidebar_visible", True)
        self.overlay_active = False
        self.tempo = self.sheet_library.get_setting("tempo", DEFAULT_TEMPO)
        self.loop_enabled = self.sheet_library.get_setting("loop_enabled", False)
        self.loop_scope = self.sheet_library.get_setting("loop_scope", "whole sheet")
        self.loop_start = str(self.sheet_library.get_setting("loop_start", 1))
        self.loop_end = str(self.sheet_library.get_setting("loop_end", 1))
        self.loop_repeats = str(self.sheet_library.get_setting("loop_repeats", 1))
        self.status = "paste a sheet"
        self.position = "0 / 0"
        self.target_mode = self.sheet_library.get_setting("target_mode", "foreground")
        self.selected_window_label = "(none selected)"
        self.window_targets = []
        self.active_target = None
        self.player = Player(self.note_changed, self.player_error, self.playback_finished)
        self._build_ui()
        self.load_active_sheet()
        self.refresh_sheet_list()
        self._register_hotkeys()
        if not is_admin():
            self.show_admin_overlay()

    def _build_ui(self):
        container = QWidget(self)
        container.setStyleSheet(f"background: {APP_BG};")
        self.setCentralWidget(container)

        root_layout = QVBoxLayout(container)
        root_layout.setContentsMargins(18, 16, 18, 16)
        root_layout.setSpacing(10)

        header = QWidget()
        header.setStyleSheet(f"background: {APP_PANEL}; border:1px solid {APP_BORDER};")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(16, 12, 16, 12)

        badge = QLabel("Piano")
        badge.setStyleSheet(f"background: {APP_GLOW}; color: {APP_BG}; font-weight: 700; padding: 6px 10px; border-radius: 5px;")
        header_layout.addWidget(badge)

        title = QLabel("virtual piano player")
        title.setStyleSheet("font-size: 20px; font-weight: 700;")
        header_layout.addWidget(title)

        sub = QLabel("click notes to jump")
        sub.setStyleSheet(f"color: {APP_MUTED}; font-size: 11px;")
        header_layout.addWidget(sub)
        header_layout.addStretch()

        self.sidebar_toggle_button = QPushButton("hide sheets")
        self.sidebar_toggle_button.clicked.connect(self.toggle_sidebar)
        self.sidebar_toggle_button.setStyleSheet(self._button_style())
        header_layout.addWidget(self.sidebar_toggle_button)

        root_layout.addWidget(header)

        self.target_bar = QWidget()
        self.target_bar.setStyleSheet(f"background: {APP_PANEL}; border:1px solid {APP_BORDER};")
        bar_layout = QHBoxLayout(self.target_bar)
        bar_layout.setContentsMargins(16, 10, 16, 10)

        label = QLabel("send input to")
        label.setStyleSheet("font-weight: 700;")
        bar_layout.addWidget(label)

        self.mode_box = QComboBox()
        self.mode_box.addItems(["whatever's focused", "specific window/exe"])
        self.mode_box.setCurrentIndex(1 if self.target_mode == "specific" else 0)
        self.mode_box.currentIndexChanged.connect(self.on_mode_change)
        bar_layout.addWidget(self.mode_box)

        self.window_box = QComboBox()
        self.window_box.setMinimumWidth(300)
        self.window_box.currentIndexChanged.connect(self.on_window_selected)
        bar_layout.addWidget(self.window_box)

        self.refresh_button = QPushButton("refresh")
        self.refresh_button.clicked.connect(self.refresh_windows)
        self.refresh_button.setStyleSheet(self._button_style())
        bar_layout.addWidget(self.refresh_button)

        self.target_label = QLabel("(none selected)")
        self.target_label.setStyleSheet(f"color: {APP_MUTED};")
        bar_layout.addWidget(self.target_label)
        bar_layout.addStretch()

        root_layout.addWidget(self.target_bar)

        self.workspace = QWidget()
        self.workspace.setStyleSheet(f"background:{APP_BG};")
        split_layout = QHBoxLayout(self.workspace)
        split_layout.setContentsMargins(0, 0, 0, 0)
        split_layout.setSpacing(10)

        self.sidebar = QWidget()
        self.sidebar.setStyleSheet(f"background:{APP_PANEL}; border:1px solid {APP_BORDER};")
        sidebar_layout = QVBoxLayout(self.sidebar)
        sidebar_layout.setContentsMargins(10, 10, 10, 10)

        sidebar_title = QLabel("Sheets")
        sidebar_title.setStyleSheet("font-size: 18px; font-weight: 700;")
        sidebar_layout.addWidget(sidebar_title)

        sidebar_caption = QLabel("Saved locally on this device")
        sidebar_caption.setStyleSheet(f"color:{APP_MUTED}; font-size: 11px;")
        sidebar_layout.addWidget(sidebar_caption)

        self.sheet_list = QListWidget()
        self.sheet_list.setStyleSheet(
            f"background: #0d1219; border:1px solid {APP_BORDER}; color: {APP_TEXT}; selection-background-color: #2b7d5d;"
        )
        self.sheet_list.itemClicked.connect(self.on_sheet_selected)
        sidebar_layout.addWidget(self.sheet_list, 1)

        action_box = QWidget()
        action_box.setStyleSheet(f"background:{APP_PANEL};")
        action_layout = QVBoxLayout(action_box)
        action_layout.setContentsMargins(0, 0, 0, 0)
        for label, callback in [
            ("New sheet", self.create_sheet),
            ("Rename", self.rename_sheet),
            ("Delete", self.delete_sheet),
            ("Validate", self.validate_active_sheet),
            ("Import sheet", self.import_sheet),
            ("Export sheet", self.export_sheet),
        ]:
            btn = QPushButton(label)
            btn.clicked.connect(callback)
            btn.setStyleSheet(self._button_style())
            action_layout.addWidget(btn)
        sidebar_layout.addWidget(action_box)

        self.content_panel = QWidget()
        self.content_panel.setStyleSheet(f"background:{APP_BG};")
        content_layout = QVBoxLayout(self.content_panel)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(8)

        self.editor = QTextEdit()
        self.editor.setStyleSheet("background: #0d1219; color: #edf6ff; border:1px solid #2b3746; font-family: 'Cascadia Mono'; font-size: 12px;")
        self.editor.textChanged.connect(self.on_editor_changed)
        self.editor.setVisible(True)
        content_layout.addWidget(self.editor)

        self.sheet_notes = QWidget()
        self.sheet_notes.setStyleSheet("background: #0d1219; border:1px solid #2b3746;")
        self.sheet_notes_layout = QVBoxLayout(self.sheet_notes)
        self.sheet_notes_layout.setContentsMargins(12, 12, 12, 12)
        self.sheet_notes_layout.setSpacing(8)

        self.notes_scroll = QScrollArea()
        self.notes_scroll.setWidget(self.sheet_notes)
        self.notes_scroll.setWidgetResizable(True)
        self.notes_scroll.setStyleSheet("background: #0d1219; border:1px solid #2b3746;")
        self.notes_scroll.setVisible(False)
        content_layout.addWidget(self.notes_scroll)

        self.controls = QWidget()
        self.controls.setStyleSheet(f"background:{APP_PANEL}; border:1px solid {APP_BORDER};")
        controls_layout = QHBoxLayout(self.controls)
        controls_layout.setContentsMargins(12, 12, 12, 12)

        for label, callback, state in [
            ("start  [f6]", self.start, True),
            ("stop  [f7]", self.stop, True),
            ("pause  [f9]", self.toggle_pause, False),
            ("submit", self.submit, True),
            ("edit", self.edit, True),
            ("exit  [f8]", self.exit_program, True),
            ("view log", self.show_log, True),
        ]:
            btn = QPushButton(label)
            btn.clicked.connect(callback)
            btn.setStyleSheet(self._button_style())
            btn.setEnabled(state)
            controls_layout.addWidget(btn)
        controls_layout.addStretch()

        tempo_label = QLabel("tempo")
        tempo_label.setStyleSheet(f"color:{APP_MUTED};")
        controls_layout.addWidget(tempo_label)
        self.tempo_slider = QSlider(Qt.Horizontal)
        self.tempo_slider.setRange(MIN_TEMPO, MAX_TEMPO)
        self.tempo_slider.setValue(self.tempo)
        self.tempo_slider.valueChanged.connect(self.set_tempo)
        self.tempo_slider.setMinimumWidth(200)
        controls_layout.addWidget(self.tempo_slider)

        root_layout.addWidget(self.workspace)
        root_layout.addWidget(self.controls)

        self.status_widget = QWidget()
        self.status_widget.setStyleSheet(f"background:{APP_PANEL}; border:1px solid {APP_BORDER};")
        status_layout = QHBoxLayout(self.status_widget)
        status_layout.setContentsMargins(12, 8, 12, 8)
        self.status_label = QLabel(self.status)
        self.status_label.setStyleSheet(f"color:{APP_SUCCESS}; font-weight: 700;")
        status_layout.addWidget(self.status_label)
        status_layout.addStretch()
        self.position_label = QLabel(self.position)
        self.position_label.setStyleSheet(f"color:{APP_MUTED};")
        status_layout.addWidget(self.position_label)
        root_layout.addWidget(self.status_widget)

        self.loop_panel = QWidget()
        self.loop_panel.setStyleSheet(f"background:{APP_BG}; color:{APP_TEXT};")
        loop_layout = QHBoxLayout(self.loop_panel)
        self.loop_checkbox = QCheckBox("repeat playback")
        self.loop_checkbox.setChecked(self.loop_enabled)
        self.loop_checkbox.toggled.connect(self.set_loop_enabled)
        loop_layout.addWidget(self.loop_checkbox)
        self.loop_scope_box = QComboBox()
        self.loop_scope_box.addItems(["whole sheet", "selected range"])
        self.loop_scope_box.setCurrentText(self.loop_scope)
        self.loop_scope_box.currentTextChanged.connect(self.set_loop_scope)
        loop_layout.addWidget(self.loop_scope_box)
        loop_layout.addWidget(QLabel("from"))
        self.loop_start_box = QLineEdit(self.loop_start)
        loop_layout.addWidget(self.loop_start_box)
        loop_layout.addWidget(QLabel("to"))
        self.loop_end_box = QLineEdit(self.loop_end)
        loop_layout.addWidget(self.loop_end_box)
        loop_layout.addWidget(QLabel("plays (0 = infinite)"))
        self.loop_repeats_box = QLineEdit(self.loop_repeats)
        loop_layout.addWidget(self.loop_repeats_box)
        root_layout.addWidget(self.loop_panel)

        split_layout.addWidget(self.sidebar)
        split_layout.addWidget(self.content_panel)
        self.workspace.setLayout(split_layout)

        self.toggle_sidebar_visibility()

    def _button_style(self):
        return (
            "QPushButton { background:#1b2430; color:#edf3ff; border:1px solid #34404d; border-radius:6px; padding:7px 12px; font-weight:700; } "
            "QPushButton:hover { background:#223040; border-color:#4d6d8b; } "
            "QPushButton:disabled { background:#1d1f24; color:#66707a; }"
        )

    def toggle_sidebar_visibility(self):
        if self.sidebar_visible:
            self.sidebar.show()
        else:
            self.sidebar.hide()

    def set_tempo(self, value):
        self.tempo = value
        self.save_library()

    def set_loop_enabled(self, enabled):
        self.loop_enabled = enabled
        self.save_library()

    def set_loop_scope(self, value):
        self.loop_scope = value
        self.save_library()

    def refresh_sheet_list(self):
        self.sheet_list.clear()
        active_sheet_name = self.sheet_library.get_active_sheet_name()
        for name in self.sheet_library.get_sheet_names():
            self.sheet_list.addItem(name)
            if name == active_sheet_name:
                self.sheet_list.setCurrentRow(self.sheet_list.count() - 1)

    def on_sheet_selected(self, item):
        if item is None:
            return
        sheet_name = item.text()
        if sheet_name == self.sheet_library.get_active_sheet_name():
            return
        self.save_current_sheet()
        self.sheet_library.set_active_sheet_name(sheet_name)
        self.show_active_sheet_in_editor()
        self.save_library()

    def on_editor_changed(self):
        if self.is_loading_sheet:
            return
        if self.editor_save_timer is not None:
            self.editor_save_timer.stop()
        self.editor_save_timer = QTimer(self)
        self.editor_save_timer.setSingleShot(True)
        self.editor_save_timer.timeout.connect(self.save_current_sheet)
        self.editor_save_timer.start(500)

    def save_current_sheet(self):
        sheet_content = self.editor.toPlainText()
        self.sheet_library.set_sheet_content(self.sheet_library.get_active_sheet_name(), sheet_content)
        self.save_library()

    def save_library(self):
        self.sheet_library.set_settings({
            "loop_enabled": self.loop_enabled,
            "loop_end": self.loop_end,
            "loop_repeats": self.loop_repeats,
            "loop_scope": self.loop_scope,
            "loop_start": self.loop_start,
            "sidebar_visible": self.sidebar_visible,
            "target_mode": self.target_mode,
            "tempo": self.tempo,
        })
        self.sheet_library.save()

    def load_active_sheet(self):
        sheet_name = self.sheet_library.get_active_sheet_name()
        self.is_loading_sheet = True
        self.editor.setPlainText(self.sheet_library.get_sheet_content(sheet_name))
        self.is_loading_sheet = False

    def show_active_sheet_in_editor(self):
        self.events = []
        self.selected_event = 0
        self.load_active_sheet()
        self.status = "editing saved sheet"
        self.set_status(self.status)

    def create_sheet(self):
        name, ok = QInputDialog.getText(self, "New sheet", "Sheet name:")
        if not ok or not str(name).strip():
            return
        self.save_current_sheet()
        if not self.sheet_library.add_sheet(str(name).strip()):
            QMessageBox.critical(self, "Cannot create sheet", "Use a unique, non-empty sheet name.")
            return
        self.show_active_sheet_in_editor()
        self.save_library()
        self.refresh_sheet_list()

    def rename_sheet(self):
        current_name = self.sheet_library.get_active_sheet_name()
        name, ok = QInputDialog.getText(self, "Rename sheet", "Sheet name:", text=current_name)
        if not ok or not str(name).strip():
            return
        if not self.sheet_library.rename_sheet(current_name, str(name).strip()):
            QMessageBox.critical(self, "Cannot rename sheet", "Use a unique, non-empty sheet name.")
            return
        self.refresh_sheet_list()
        self.save_library()

    def delete_sheet(self):
        current_name = self.sheet_library.get_active_sheet_name()
        if len(self.sheet_library.get_sheet_names()) == 1:
            QMessageBox.information(self, "Keep one sheet", "Create another sheet before deleting this one.")
            return
        if QMessageBox.question(self, "Delete sheet", f"Delete '{current_name}'? This cannot be undone.") != QMessageBox.StandardButton.Yes:
            return
        if self.sheet_library.delete_sheet(current_name):
            self.show_active_sheet_in_editor()
            self.save_library()
            self.refresh_sheet_list()

    def validate_sheet_content(self, sheet_content):
        if not sheet_content.strip():
            return None, "add at least one note before validating"
        try:
            return SheetParser.parse(sheet_content), None
        except ValueError as error:
            return None, str(error)

    def build_validation_summary(self, events, sheet_content):
        event_counts = {"chord": 0, "note": 0, "rest": 0, "run": 0}
        for event in events:
            event_counts[event["type"]] += 1
        line_count = len(sheet_content.splitlines())
        return (
            f"{len(events)} events across {line_count} lines\n\n"
            f"{event_counts['note']} notes\n"
            f"{event_counts['chord']} chords\n"
            f"{event_counts['run']} fast runs\n"
            f"{event_counts['rest']} rests"
        )

    def show_validation_preview(self, sheet_content, title, confirm_text=None, on_confirm=None):
        events, error = self.validate_sheet_content(sheet_content)
        if error is not None:
            QMessageBox.warning(self, title, error)
            return
        summary = self.build_validation_summary(events, sheet_content)
        msg = QMessageBox(self)
        msg.setWindowTitle(title)
        msg.setText(summary)
        if confirm_text is not None and on_confirm is not None:
            msg.setStandardButtons(QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel)
            msg.button(QMessageBox.StandardButton.Ok).setText(confirm_text)
            result = msg.exec()
            if result == QMessageBox.StandardButton.Ok:
                on_confirm(events)
        else:
            msg.setStandardButtons(QMessageBox.StandardButton.Ok)
            msg.exec()

    def validate_active_sheet(self):
        self.save_current_sheet()
        sheet_content = self.editor.toPlainText()
        self.show_validation_preview(sheet_content, "Validation preview")

    def import_sheet(self):
        file_path, _ = QFileDialog.getOpenFileName(self, "Import sheet", "", "Piano sheet (*.piano-sheet.json *.txt)")
        if not file_path:
            return
        imported_sheet, error = SheetFileCodec.load(file_path)
        if error is not None:
            QMessageBox.critical(self, "Cannot import sheet", error)
            return
        self.show_validation_preview(imported_sheet["sheet"], "Import preview", "Import sheet", lambda _events: self.finish_import(imported_sheet))

    def finish_import(self, imported_sheet):
        name, ok = QInputDialog.getText(self, "Import sheet", "Sheet name:", text=imported_sheet["name"])
        if not ok:
            return
        self.save_current_sheet()
        if not self.sheet_library.add_sheet(str(name).strip()):
            QMessageBox.critical(self, "Cannot import sheet", "Use a unique, non-empty sheet name.")
            return
        self.sheet_library.set_sheet_content(self.sheet_library.get_active_sheet_name(), imported_sheet["sheet"])
        self.show_active_sheet_in_editor()
        self.save_library()
        self.refresh_sheet_list()
        self.set_status("sheet imported")

    def export_sheet(self):
        self.save_current_sheet()
        sheet_name = self.sheet_library.get_active_sheet_name()
        safe_name = "".join(ch if ch.isalnum() or ch in " -_" else "_" for ch in sheet_name).strip() or "sheet"
        file_path, _ = QFileDialog.getSaveFileName(self, "Export sheet", f"{safe_name}{SHEET_FILE_EXTENSION}", f"Piano sheet (*{SHEET_FILE_EXTENSION})")
        if not file_path:
            return
        try:
            with open(file_path, "w", encoding="utf-8") as handle:
                json.dump(SheetFileCodec.create_export(sheet_name, self.editor.toPlainText()), handle, ensure_ascii=False, indent=2)
        except OSError as error:
            QMessageBox.critical(self, "Cannot export sheet", f"could not write the file: {error}")
            return
        self.set_status("sheet exported")

    def toggle_sidebar(self):
        self.sidebar_visible = not self.sidebar_visible
        self.toggle_sidebar_visibility()
        self.save_library()

    def on_mode_change(self, _index):
        self.target_mode = "specific" if self.mode_box.currentIndex() == 1 else "foreground"
        self.refresh_windows()
        if self.target_mode == "specific":
            self.window_box.show()
            self.refresh_button.show()
            self.target_label.show()
        else:
            self.window_box.hide()
            self.refresh_button.hide()
            self.target_label.hide()
        self.save_library()

    def refresh_windows(self):
        self.window_targets = list_windows()
        self.window_box.clear()
        for target in self.window_targets:
            self.window_box.addItem(str(target))
        if self.active_target is not None:
            for idx, target in enumerate(self.window_targets):
                if str(target) == str(self.active_target):
                    self.window_box.setCurrentIndex(idx)
                    return
        self.active_target = None
        self.selected_window_label = "(none selected)"
        self.target_label.setText(self.selected_window_label)

    def on_window_selected(self, index):
        if index < 0 or index >= len(self.window_targets):
            return
        self.active_target = self.window_targets[index]
        self.selected_window_label = f"-> {self.active_target}"
        self.target_label.setText(self.selected_window_label)

    def player_error(self, message):
        self.set_status(message)

    def submit(self):
        if self.player.playing:
            return
        self.save_current_sheet()
        sheet_content = self.editor.toPlainText()
        events, error = self.validate_sheet_content(sheet_content)
        if error is not None:
            QMessageBox.warning(self, "Playback preview", error)
            return
        self.events = events
        self.editing = False
        self.selected_event = 0
        self.playback_start_index = 0
        self.loop_start = "1"
        self.loop_end = str(len(self.events))
        self.editor.setVisible(False)
        self.notes_scroll.setVisible(True)
        self.render_notes()
        self.set_status(f"ready • {len(self.events)} notes")

    def edit(self):
        if self.player.playing:
            return
        self.editing = True
        self.notes_scroll.setVisible(False)
        self.editor.setVisible(True)
        self.set_status("editing")

    def render_notes(self):
        for child in self.sheet_notes.children():
            if child is not None:
                child.deleteLater()
        self.sheet_notes_layout = QVBoxLayout(self.sheet_notes)
        self.sheet_notes_layout.setContentsMargins(12, 12, 12, 12)
        self.sheet_notes_layout.setSpacing(8)
        self.note_buttons = {}

        lines = {}
        for index, event in enumerate(self.events):
            lines.setdefault(event["line"], []).append((index, event))

        max_line = max(lines.keys(), default=0)
        for line_number in range(max_line + 1):
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            line_label = QLabel(f"{line_number + 1:03}")
            line_label.setStyleSheet(f"color:#536071; min-width: 32px;")
            row_layout.addWidget(line_label)
            for index, event in lines.get(line_number, []):
                btn = QPushButton(event["source"])
                btn.setProperty("note", True)
                btn.clicked.connect(lambda _checked, idx=index: self.select_note(idx))
                btn.setMinimumHeight(32)
                btn.setStyleSheet(self._note_style(index == self.selected_event, event["type"] == "rest"))
                row_layout.addWidget(btn)
                self.note_buttons[index] = btn
            self.sheet_notes_layout.addWidget(row)

        self.update_note_styles()

    def _note_style(self, selected, is_rest):
        if selected:
            return "QPushButton { background:#247f4c; color:#ffffff; border:1px solid #55d988; border-radius:6px; padding:5px 7px; font-family:'Cascadia Mono'; font-weight:700; }"
        if is_rest:
            return "QPushButton { background:#181c22; color:#454b53; border:1px solid #292f37; border-radius:6px; padding:5px 7px; font-family:'Cascadia Mono'; font-weight:700; }"
        return "QPushButton { background:#181c22; color:#dfe7ef; border:1px solid #292f37; border-radius:6px; padding:5px 7px; font-family:'Cascadia Mono'; font-weight:700; }"

    def update_note_styles(self):
        for idx, btn in self.note_buttons.items():
            if idx < self.selected_event:
                btn.setStyleSheet("QPushButton { background:#15181d; color:#464c54; border:1px solid #20252b; border-radius:6px; padding:5px 7px; font-family:'Cascadia Mono'; font-weight:700; }")
            elif idx == self.selected_event:
                btn.setStyleSheet("QPushButton { background:#247f4c; color:#ffffff; border:1px solid #55d988; border-radius:6px; padding:5px 7px; font-family:'Cascadia Mono'; font-weight:700; }")
            else:
                btn.setStyleSheet("QPushButton { background:#181c22; color:#ffffff; border:1px solid #292f37; border-radius:6px; padding:5px 7px; font-family:'Cascadia Mono'; font-weight:700; }")
        self.position = f"{self.selected_event + 1} / {len(self.events)}"
        self.position_label.setText(self.position)

    def select_note(self, index):
        if self.editing:
            return
        self.player.stop()
        self.selected_event = index
        self.update_note_styles()
        self.scroll_to_note(index)
        self.set_status(f"selected note {index + 1}")

    def scroll_to_note(self, index):
        if index in self.note_buttons:
            self.notes_scroll.ensureWidgetVisible(self.note_buttons[index])

    def note_changed(self, index):
        self.selected_event = index
        self.update_note_styles()
        self.scroll_to_note(index)

    def playback_finished(self, was_stopped):
        self.set_status("finished" if not was_stopped else "stopped")

    def set_status(self, message):
        self.status = message
        self.status_label.setText(message)

    def player_error(self, message):
        self.set_status(message)

    def start(self):
        if self.editing:
            self.set_status("submit the sheet first")
            return
        if not self.events:
            self.set_status("no sheet submitted")
            return
        if self.player.playing:
            return
        playback_options = self.get_playback_options()
        if playback_options is None:
            return
        start_index, end_index, repeat_count = playback_options
        if self.target_mode == "specific":
            if self.active_target is None:
                self.set_status("pick a window first")
                return
            if not self.active_target.is_alive():
                self.set_status("target window closed - pick again")
                self.refresh_windows()
                return
            success, reason = self.active_target.focus()
            if not success:
                QMessageBox.critical(self, "can't focus target", f"couldn't switch to the target window: {reason}\n\ntry clicking the target window manually once, then hit start again.")
                return
            target = self.active_target
        else:
            target = None
        self.player.start(self.events, start_index, end_index, repeat_count, self.tempo, target)
        self.playback_start_index = start_index
        self.set_status(f"playing notes {start_index + 1}–{end_index + 1}")

    def stop(self):
        self.player.stop()
        if not self.editing:
            self.selected_event = self.playback_start_index
            self.update_note_styles()
            self.set_status(f"stopped • reset to note {self.selected_event + 1}")

    def toggle_pause(self):
        if self.player.pause():
            self.set_status("paused")
            return
        if self.player.resume():
            self.set_status("playing")

    def get_playback_options(self):
        if not self.loop_enabled:
            return self.selected_event, len(self.events) - 1, 1
        try:
            repeat_count = int(self.loop_repeats)
        except ValueError:
            self.set_status("enter a whole number of plays")
            return None
        if repeat_count < 0:
            self.set_status("plays cannot be negative")
            return None
        if self.loop_scope == "whole sheet":
            return 0, len(self.events) - 1, repeat_count
        try:
            range_start = int(self.loop_start) - 1
            range_end = int(self.loop_end) - 1
        except ValueError:
            self.set_status("enter whole-number range limits")
            return None
        if not 0 <= range_start <= range_end < len(self.events):
            self.set_status(f"range must be between 1 and {len(self.events)}")
            return None
        return range_start, range_end, repeat_count

    def show_log(self):
        try:
            with open(paths.LOG_PATH, "r", encoding="utf-8") as handle:
                content = handle.read()
        except FileNotFoundError:
            content = "no log written yet - hit start first"
        QMessageBox.information(self, "debug log", content)

    def show_admin_overlay(self):
        msg = QMessageBox(self)
        msg.setWindowTitle("not running as administrator")
        msg.setText("key presses may not reach some games without elevated permissions.")
        msg.setInformativeText("You can request admin now, or continue without admin.")
        msg.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        msg.button(QMessageBox.StandardButton.Yes).setText("request admin")
        msg.button(QMessageBox.StandardButton.No).setText("continue without admin")
        result = msg.exec()
        if result == QMessageBox.StandardButton.Yes:
            if relaunch_as_admin():
                self.closing = True
                self.close()
            else:
                QMessageBox.warning(self, "elevation cancelled", "The admin request was cancelled or failed.")
        else:
            self.overlay_active = False

    def _register_hotkeys(self):
        try:
            keyboard.add_hotkey(START_HOTKEY, self.start)
            keyboard.add_hotkey(STOP_HOTKEY, self.stop)
            keyboard.add_hotkey(PAUSE_HOTKEY, self.toggle_pause)
            keyboard.add_hotkey(EXIT_HOTKEY, self.exit_program)
        except Exception as error:
            log.exception("hotkey registration failed - try running as admin")
            self.set_status(f"hotkey error: {error}")

    def exit_program(self):
        self.save_current_sheet()
        self.player.stop()
        try:
            keyboard.unhook_all_hotkeys()
        except Exception:
            pass
        self.close()


def main():
    app = QApplication(sys.argv)
    window = App()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

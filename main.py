import ctypes
import json
import logging
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import keyboard
import mido
import psutil
import pydirectinput
import win32con
import win32gui
import win32process
from PySide6.QtCore import (
    Qt,
    QTimer,
    QPropertyAnimation,
    QEasingCurve,
    QPoint,
    QRect,
    QRectF,
    Property,
    QParallelAnimationGroup,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QPainter,
    QPainterPath,
    QLinearGradient,
    QFont,
    QPen,
    QBrush,
)
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QGraphicsDropShadowEffect,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressDialog,
    QScrollArea,
    QSlider,
    QVBoxLayout,
    QWidget,
    QPushButton,
    QTextEdit,
    QComboBox,
    QCheckBox,
    QFrame,
)

import paths
import updater

CURRENT_VERSION = "1.4.0"
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

# ── design tokens ──────────────────────────────────────────────────────────
# minimal dark glass palette - desaturated, layered translucency, one mint accent
BG_BASE = QColor(10, 12, 16)
BG_TOP = QColor(16, 19, 25)
GLASS_FILL = QColor(255, 255, 255, 10)
GLASS_FILL_HOVER = QColor(255, 255, 255, 16)
GLASS_BORDER = QColor(255, 255, 255, 22)
GLASS_BORDER_SOFT = QColor(255, 255, 255, 12)

ACCENT = "#7ee8b8"
ACCENT_DIM = "#4fa87f"
ACCENT_QC = QColor("#7ee8b8")
DANGER = "#ff8a94"
WARN = "#f3c675"

TEXT_PRIMARY = "#f2f5f8"
TEXT_SECONDARY = "#9aa4b2"
TEXT_FAINT = "#5b6472"

FONT_FAMILY = "Segoe UI"
MONO_FAMILY = "Cascadia Mono"

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
        self.sheet_settings = {}
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

        sheet_settings = content.get("sheet_settings", {})
        if isinstance(sheet_settings, dict):
            self.sheet_settings = {
                name: value for name, value in sheet_settings.items()
                if isinstance(name, str) and isinstance(value, dict)
            }

    def save(self):
        try:
            os.makedirs(self.storage_directory, exist_ok=True)
            temporary_path = f"{self.storage_path}.tmp"
            with open(temporary_path, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "active_sheet_name": self.active_sheet_name,
                        "settings": self.settings,
                        "sheet_settings": self.sheet_settings,
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

    def get_sheet_setting(self, sheet_name, name, default):
        settings = self.sheet_settings.get(sheet_name, {})
        if not isinstance(settings, dict):
            return default
        return settings.get(name, default)

    def set_sheet_settings(self, sheet_name, settings):
        if sheet_name not in self.sheets:
            return False
        if not isinstance(settings, dict):
            return False
        self.sheet_settings.setdefault(sheet_name, {})
        self.sheet_settings[sheet_name].update(settings)
        return True

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

    @staticmethod
    def _parse_line(line, line_number):
        events = []
        i = 0
        line_length = len(line)
        while i < line_length:
            char = line[i]
            if char == " ":
                events.append({"type": "rest", "length": 0.35, "source": " ", "line": line_number})
                i += 1
                continue
            if char == "-":
                count = 0
                while i < line_length and line[i] == "-":
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
                    action = SheetParser.key_to_action(item)
                    if action is None:
                        raise ValueError(f"unsupported character {item!r} on line {line_number + 1}")
                    actions.append(action)
                if not actions:
                    raise ValueError(f"empty chord on line {line_number + 1}")
                events.append({"type": "chord", "actions": actions, "source": line[i:end + 1], "line": line_number, "beats": 1.0})
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
                    action = SheetParser.key_to_action(item)
                    if action is None:
                        raise ValueError(f"unsupported character {item!r} on line {line_number + 1}")
                    actions.append(action)
                if not actions:
                    raise ValueError(f"empty run on line {line_number + 1}")
                events.append({"type": "run", "actions": actions, "source": line[i:end + 1], "line": line_number, "beats": 1.0})
                i = end + 1
                continue
            action = SheetParser.key_to_action(char)
            if action is None:
                raise ValueError(f"unsupported character {char!r} on line {line_number + 1}")
            events.append({"type": "note", "actions": [action], "source": char, "line": line_number, "beats": 1.0})
            i += 1
        return events

    @classmethod
    def parse(cls, text, max_workers=None):
        lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        if not lines:
            return []

        if max_workers is None:
            max_workers = min(8, max(1, os.cpu_count() or 1))

        # Small texts do not benefit from thread startup overhead.
        if len(lines) <= 2 or max_workers <= 1:
            events = []
            for line_number, line in enumerate(lines):
                events.extend(cls._parse_line(line, line_number))
            return events

        chunk_size = max(1, len(lines) // max_workers)
        tasks = []
        for start in range(0, len(lines), chunk_size):
            end = min(start + chunk_size, len(lines))
            tasks.append((start, lines[start:end]))

        events_by_line = []
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = []
            for line_index, chunk_lines in tasks:
                futures.append(executor.submit(cls._parse_lines_chunk, line_index, chunk_lines))
            for future in futures:
                events_by_line.extend(future.result())

        merged = []
        for _, line_events in sorted(events_by_line, key=lambda item: item[0]):
            merged.extend(line_events)
        return merged

    @staticmethod
    def _parse_lines_chunk(start_index, lines):
        processed = []
        for offset, line in enumerate(lines):
            line_number = start_index + offset
            processed.append((line_number, SheetParser._parse_line(line, line_number)))
        return processed


def midi_note_to_sheet_char(note_number, base_note=60):
    """Map a MIDI note number to one of the 13 chromatic keys the sheet
    format supports. The sheet format only has one octave's worth of keys,
    so notes are transposed by whole octaves toward `base_note` (middle C)
    rather than wrapped with a naive mod — this keeps the melodic shape
    closer to correct, but notes more than half an octave from the piece's
    center will still lose their original register."""
    mapping = "awsedftgyhujk"
    octave = 12
    while note_number - base_note > octave // 2:
        note_number -= octave
    while base_note - note_number > octave // 2:
        note_number += octave
    return mapping[(note_number - base_note) % octave]


def apply_timing_scale(event, scale_factor):
    if not isinstance(scale_factor, (int, float)) or scale_factor <= 0:
        raise ValueError("timing scale must be a positive number")
    scale_factor = float(scale_factor)
    if event["type"] == "rest":
        event["length"] = max(0.25, float(event.get("length", 0.5)) * scale_factor)
        return event
    if event["type"] in {"note", "chord", "run"}:
        event["beats"] = max(0.25, float(event.get("beats", 1.0)) * scale_factor)
        return event
    return event


def midi_to_sheet_text(midi_path):
    midi_file = mido.MidiFile(midi_path)
    ticks_per_beat = max(1, midi_file.ticks_per_beat)
    merged = mido.merge_tracks(midi_file.tracks)

    note_starts = {}
    note_events = []
    current_tick = 0

    for message in merged:
        current_tick += message.time
        if not hasattr(message, "note"):
            continue
        key = (getattr(message, "channel", 0), message.note)
        if message.type == "note_on" and message.velocity > 0:
            note_starts[key] = current_tick
        elif message.type == "note_off" or (message.type == "note_on" and message.velocity == 0):
            start_tick = note_starts.pop(key, None)
            if start_tick is not None:
                note_events.append((start_tick, current_tick, message.note))

    if not note_events:
        return ""

    note_events.sort(key=lambda item: (item[0], item[2]))

    tolerance_ticks = max(1, ticks_per_beat // 32)
    groups = []
    for start_tick, end_tick, note in note_events:
        if groups and start_tick - groups[-1]["start"] <= tolerance_ticks:
            groups[-1]["notes"].append(note)
            groups[-1]["end"] = max(groups[-1]["end"], end_tick)
        else:
            groups.append({"start": start_tick, "end": end_tick, "notes": [note]})

    def ticks_to_beats(ticks):
        return ticks / ticks_per_beat

    def format_beats(beats):
        rounded = round(beats, 2)
        return f"{rounded:g}"

    lines, current_line = [], []

    def flush_token(token):
        current_line.append(token)
        if len(current_line) >= 20:
            lines.append("".join(current_line))
            current_line.clear()

    previous_end_tick = groups[0]["start"]
    for group in groups:
        gap_ticks = group["start"] - previous_end_tick
        if gap_ticks > tolerance_ticks:
            rest_count = max(1, round(ticks_to_beats(gap_ticks)))
            flush_token("-" * rest_count)

        beats = max(0.1, ticks_to_beats(group["end"] - group["start"]))
        chars = [midi_note_to_sheet_char(n) for n in sorted(set(group["notes"]))]
        body = chars[0] if len(chars) == 1 else "[" + "".join(chars) + "]"
        flush_token(f"{body}:{format_beats(beats)}")

        previous_end_tick = group["end"]

    if current_line:
        lines.append("".join(current_line))

    return "\n".join(lines)

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
            return float(event.get("length", 0.5)) if event.get("source") != " " else 0.5
        return float(event.get("beats", 1.0))

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


# ── glass ui primitives ─────────────────────────────────────────────────────

class GlassPanel(QFrame):
    """A rounded, faintly translucent panel with a hairline border - the base
    surface every section sits on. Painted manually so radius/opacity stay
    crisp regardless of stylesheet cascade."""

    def __init__(self, radius=14, fill=None, border=None, parent=None):
        super().__init__(parent)
        self.radius = radius
        self.fill = fill or GLASS_FILL
        self.border = border or GLASS_BORDER_SOFT
        self.setAttribute(Qt.WA_StyledBackground, False)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(rect, self.radius, self.radius)
        painter.fillPath(path, QBrush(self.fill))
        painter.setPen(QPen(self.border, 1))
        painter.drawPath(path)


class AnimatedButton(QPushButton):
    """Push button with a hover-lift + color glide instead of an instant
    stylesheet swap - keeps hover state feeling alive without being gimmicky."""

    def __init__(self, text, kind="default", parent=None):
        super().__init__(text, parent)
        self.kind = kind
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(36)
        self._apply_palette()
        self._hover_progress = 0.0
        self._anim = QPropertyAnimation(self, b"hoverProgress")
        self._anim.setDuration(140)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)

    def _apply_palette(self):
        palettes = {
            "default": (QColor(255, 255, 255, 14), QColor(255, 255, 255, 26), TEXT_PRIMARY),
            "accent": (QColor(126, 232, 184, 40), QColor(126, 232, 184, 90), "#0c1712"),
            "danger": (QColor(255, 138, 148, 30), QColor(255, 138, 148, 70), "#2a0f12"),
        }
        self.base_fill, self.hover_fill, self.text_color = palettes.get(self.kind, palettes["default"])

    def getHoverProgress(self):
        return self._hover_progress

    def setHoverProgress(self, value):
        self._hover_progress = value
        self.update()

    hoverProgress = Property(float, getHoverProgress, setHoverProgress)

    def enterEvent(self, event):
        self._anim.stop()
        self._anim.setStartValue(self._hover_progress)
        self._anim.setEndValue(1.0)
        self._anim.start()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._anim.stop()
        self._anim.setStartValue(self._hover_progress)
        self._anim.setEndValue(0.0)
        self._anim.start()
        super().leaveEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(rect, 9, 9)

        t = self._hover_progress
        fill = QColor(
            int(self.base_fill.red() + (self.hover_fill.red() - self.base_fill.red()) * t),
            int(self.base_fill.green() + (self.hover_fill.green() - self.base_fill.green()) * t),
            int(self.base_fill.blue() + (self.hover_fill.blue() - self.base_fill.blue()) * t),
            int(self.base_fill.alpha() + (self.hover_fill.alpha() - self.base_fill.alpha()) * t),
        )
        if not self.isEnabled():
            fill = QColor(255, 255, 255, 6)

        # subtle lift: shift content rect up by 1px at full hover
        lift = -1 * t
        painter.translate(0, lift)
        painter.fillPath(path, QBrush(fill))
        painter.setPen(QPen(QColor(255, 255, 255, int(18 + 20 * t)), 1))
        painter.drawPath(path)
        painter.translate(0, -lift)

        painter.setPen(QColor(self.text_color) if self.isEnabled() else QColor(TEXT_FAINT))
        font = self.font()
        font.setWeight(QFont.DemiBold)
        painter.setFont(font)
        painter.drawText(self.rect().adjusted(0, int(lift), 0, int(lift)), Qt.AlignCenter, self.text())


class PulseDot(QWidget):
    """Small breathing status dot - green when idle/ready, amber mid-motion,
    red on error. Loops a soft radius pulse via QPropertyAnimation."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(10, 10)
        self._radius = 3.0
        self.color = QColor(ACCENT)
        self._anim = QPropertyAnimation(self, b"radius")
        self._anim.setDuration(1100)
        self._anim.setStartValue(3.0)
        self._anim.setEndValue(4.6)
        self._anim.setEasingCurve(QEasingCurve.InOutSine)
        self._anim.setLoopCount(-1)
        self._anim.finished.connect(self._reverse)
        self._direction = 1

    def _reverse(self):
        pass

    def start(self):
        self._anim.start()

    def getRadius(self):
        return self._radius

    def setRadius(self, value):
        self._radius = value
        self.update()

    radius = Property(float, getRadius, setRadius)

    def set_color(self, hex_color):
        self.color = QColor(hex_color)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        center = QPoint(5, 5)
        glow = QColor(self.color)
        glow.setAlpha(60)
        painter.setBrush(QBrush(glow))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(center, self._radius + 3, self._radius + 3)
        painter.setBrush(QBrush(self.color))
        painter.drawEllipse(center, self._radius, self._radius)


class BackgroundCanvas(QWidget):
    """Root background: vertical gradient + two soft radial glows, painted
    once behind everything so glass panels have something to sit on top of."""

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = self.rect()

        gradient = QLinearGradient(0, 0, 0, rect.height())
        gradient.setColorAt(0.0, BG_TOP)
        gradient.setColorAt(1.0, BG_BASE)
        painter.fillRect(rect, gradient)

        glow1 = QLinearGradient(0, 0, rect.width() * 0.5, rect.height() * 0.5)
        radial_color = QColor(ACCENT)
        radial_color.setAlpha(10)
        painter.setBrush(QBrush(radial_color))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(QPoint(int(rect.width() * 0.15), int(rect.height() * 0.05)), 380, 260)

        radial_color2 = QColor("#6ea8fe")
        radial_color2.setAlpha(7)
        painter.setBrush(QBrush(radial_color2))
        painter.drawEllipse(QPoint(int(rect.width() * 0.95), int(rect.height() * 0.9)), 340, 240)


def make_shadow(blur=28, alpha=140, y_offset=6):
    effect = QGraphicsDropShadowEffect()
    effect.setBlurRadius(blur)
    effect.setOffset(0, y_offset)
    effect.setColor(QColor(0, 0, 0, alpha))
    return effect


class NotePill(QPushButton):
    """One note/chord/rest token in the playback view. Selection and
    'already played' states glide smoothly via a background-color animation
    instead of hard style swaps, so scanning the sheet during playback feels
    fluid rather than flickery."""

    def __init__(self, text, is_rest, parent=None):
        super().__init__(text, parent)
        self.is_rest = is_rest
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumSize(30, 30)
        self.setFont(QFont(MONO_FAMILY, 11, QFont.DemiBold))
        self._state = "upcoming"  # upcoming | played | current
        self._bg = QColor(24, 28, 34, 235)
        self._anim = QPropertyAnimation(self, b"bgColor")
        self._anim.setDuration(220)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)

    def getBgColor(self):
        return self._bg

    def setBgColor(self, value):
        self._bg = value
        self.update()

    bgColor = Property(QColor, getBgColor, setBgColor)

    def set_state(self, state):
        if state == self._state:
            return
        self._state = state
        targets = {
            "current": QColor(126, 232, 184, 235),
            "played": QColor(255, 255, 255, 10),
            "upcoming": QColor(255, 255, 255, 18) if not self.is_rest else QColor(255, 255, 255, 6),
        }
        self._anim.stop()
        self._anim.setStartValue(self._bg)
        self._anim.setEndValue(targets[state])
        self._anim.start()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(rect, 7, 7)
        painter.fillPath(path, QBrush(self._bg))

        if self._state == "current":
            painter.setPen(QPen(QColor(126, 232, 184, 255), 1.4))
            painter.drawPath(path)
            text_color = QColor("#0c1712")
        elif self._state == "played":
            painter.setPen(QPen(QColor(255, 255, 255, 14), 1))
            painter.drawPath(path)
            text_color = QColor(TEXT_FAINT)
        else:
            painter.setPen(QPen(QColor(255, 255, 255, 20), 1))
            painter.drawPath(path)
            text_color = QColor(TEXT_FAINT) if self.is_rest else QColor(TEXT_PRIMARY)

        painter.setPen(text_color)
        painter.setFont(self.font())
        painter.drawText(self.rect(), Qt.AlignCenter, self.text())


class App(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("virtual piano player")
        self.resize(1180, 800)
        self.setMinimumSize(900, 640)

        self.events = []
        self.note_buttons = {}
        self.selected_event = 0
        self.selected_events = set()
        self.drag_selecting = False
        self.drag_start_index = None
        self.playback_start_index = 0
        self.editing = True
        self.closing = False
        self.sheet_library = SheetLibrary()
        self.editor_save_timer = None
        self.is_loading_sheet = False
        self.sidebar_visible = self.sheet_library.get_setting("sidebar_visible", True)
        self.overlay_active = False
        self.tempo = self.sheet_library.get_sheet_setting(self.sheet_library.get_active_sheet_name(), "tempo", DEFAULT_TEMPO)
        self.loop_enabled = self.sheet_library.get_sheet_setting(self.sheet_library.get_active_sheet_name(), "loop_enabled", False)
        self.loop_scope = self.sheet_library.get_sheet_setting(self.sheet_library.get_active_sheet_name(), "loop_scope", "whole sheet")
        self.loop_start = str(self.sheet_library.get_sheet_setting(self.sheet_library.get_active_sheet_name(), "loop_start", 1))
        self.loop_end = str(self.sheet_library.get_sheet_setting(self.sheet_library.get_active_sheet_name(), "loop_end", 1))
        self.loop_repeats = str(self.sheet_library.get_sheet_setting(self.sheet_library.get_active_sheet_name(), "loop_repeats", 1))
        self.status = "paste a sheet"
        self.position = "0 / 0"
        self.target_mode = self.sheet_library.get_setting("target_mode", "foreground")
        self.selected_window_label = "(none selected)"
        self.window_targets = []
        self.active_target = None
        self.player = Player(self.note_changed, self.player_error, self.playback_finished)
        self._sidebar_anim = None
        self._sidebar_width = 260
        self._last_status_key = None
        self._last_note_states = {}
        self._build_ui()
        self.load_active_sheet()
        self.refresh_sheet_list()
        self._register_hotkeys()
        QTimer.singleShot(600, self.check_for_updates)
        if not is_admin():
            QTimer.singleShot(200, self.show_admin_overlay)

    # ── ui construction ─────────────────────────────────────────────────

    def _build_ui(self):
        canvas = BackgroundCanvas(self)
        self.setCentralWidget(canvas)

        root_layout = QVBoxLayout(canvas)
        root_layout.setContentsMargins(20, 18, 20, 18)
        root_layout.setSpacing(12)

        root_layout.addWidget(self._build_header())
        root_layout.addWidget(self._build_target_bar())

        self.workspace = QWidget()
        split_layout = QHBoxLayout(self.workspace)
        split_layout.setContentsMargins(0, 0, 0, 0)
        split_layout.setSpacing(12)

        self.sidebar = self._build_sidebar()
        split_layout.addWidget(self.sidebar)
        split_layout.addWidget(self._build_content_panel(), 1)

        root_layout.addWidget(self.workspace, 1)
        root_layout.addWidget(self._build_controls())
        root_layout.addWidget(self._build_status_bar())
        root_layout.addWidget(self._build_loop_panel())

        if not self.sidebar_visible:
            self.sidebar.setMaximumWidth(0)
            self.sidebar_toggle_button.setText("show sheets")

    def _section_label(self, text, faint=None):
        label = QLabel(text)
        label.setStyleSheet(f"color:{faint or TEXT_SECONDARY}; font-size:11px; letter-spacing:0.5px;")
        return label

    def _build_header(self):
        header = GlassPanel(radius=16)
        header.setGraphicsEffect(make_shadow(24, 120, 4))
        layout = QHBoxLayout(header)
        layout.setContentsMargins(20, 14, 18, 14)
        layout.setSpacing(12)

        badge = QLabel("♪")
        badge.setFixedSize(34, 34)
        badge.setAlignment(Qt.AlignCenter)
        badge.setStyleSheet(
            f"background: rgba(126,232,184,0.16); color:{ACCENT}; font-size:17px; "
            f"font-weight:700; border-radius:9px; border: 1px solid rgba(126,232,184,0.35);"
        )
        layout.addWidget(badge)

        title_box = QVBoxLayout()
        title_box.setSpacing(1)
        title = QLabel("virtual piano player")
        title.setStyleSheet(f"color:{TEXT_PRIMARY}; font-size:17px; font-weight:650;")
        sub = QLabel("click any note to jump playback there")
        sub.setStyleSheet(f"color:{TEXT_FAINT}; font-size:11px;")
        title_box.addWidget(title)
        title_box.addWidget(sub)
        layout.addLayout(title_box)
        layout.addStretch()

        self.status_dot_small = PulseDot()
        self.status_dot_small.start()
        layout.addWidget(self.status_dot_small)

        self.sidebar_toggle_button = AnimatedButton("hide sheets")
        self.sidebar_toggle_button.setFixedWidth(112)
        self.sidebar_toggle_button.clicked.connect(self.toggle_sidebar)
        layout.addWidget(self.sidebar_toggle_button)

        return header

    def _build_target_bar(self):
        self.target_bar = GlassPanel(radius=14)
        layout = QHBoxLayout(self.target_bar)
        layout.setContentsMargins(18, 10, 18, 10)
        layout.setSpacing(10)

        label = QLabel("send input to")
        label.setStyleSheet(f"color:{TEXT_SECONDARY}; font-weight:600; font-size:12px;")
        layout.addWidget(label)

        self.mode_box = QComboBox()
        self.mode_box.addItems(["whatever's focused", "specific window/exe"])
        self.mode_box.setCurrentIndex(1 if self.target_mode == "specific" else 0)
        self.mode_box.currentIndexChanged.connect(self.on_mode_change)
        self.mode_box.setStyleSheet(self._combo_style())
        layout.addWidget(self.mode_box)

        self.window_box = QComboBox()
        self.window_box.setMinimumWidth(300)
        self.window_box.currentIndexChanged.connect(self.on_window_selected)
        self.window_box.setStyleSheet(self._combo_style())
        layout.addWidget(self.window_box)

        self.refresh_button = AnimatedButton("refresh")
        self.refresh_button.setFixedWidth(90)
        self.refresh_button.clicked.connect(self.refresh_windows)
        layout.addWidget(self.refresh_button)

        self.target_label = QLabel("(none selected)")
        self.target_label.setStyleSheet(f"color:{TEXT_FAINT}; font-size:12px;")
        layout.addWidget(self.target_label)
        layout.addStretch()

        return self.target_bar

    def _build_sidebar(self):
        sidebar = GlassPanel(radius=14)
        sidebar.setFixedWidth(self._sidebar_width)
        sidebar.setGraphicsEffect(make_shadow(20, 100, 4))
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(8)

        title = QLabel("Sheets")
        title.setStyleSheet(f"color:{TEXT_PRIMARY}; font-size:15px; font-weight:650;")
        layout.addWidget(title)

        caption = self._section_label("saved locally on this device", TEXT_FAINT)
        layout.addWidget(caption)

        self.sheet_list = QListWidget()
        self.sheet_list.setStyleSheet(f"""
            QListWidget {{
                background: rgba(255,255,255,0.03);
                border: 1px solid rgba(255,255,255,0.08);
                border-radius: 9px;
                color: {TEXT_PRIMARY};
                padding: 4px;
                font-size: 13px;
                outline: 0;
            }}
            QListWidget::item {{
                padding: 8px 8px;
                border-radius: 6px;
                margin: 1px 0;
            }}
            QListWidget::item:selected {{
                background: rgba(126,232,184,0.18);
                color: {ACCENT};
            }}
            QListWidget::item:hover:!selected {{
                background: rgba(255,255,255,0.05);
            }}
        """)
        self.sheet_list.itemClicked.connect(self.on_sheet_selected)
        layout.addWidget(self.sheet_list, 1)

        for label, callback, kind in [
            ("New sheet", self.create_sheet, "default"),
            ("Rename", self.rename_sheet, "default"),
            ("Delete", self.delete_sheet, "danger"),
            ("Validate", self.validate_active_sheet, "default"),
            ("Import sheet", self.import_sheet, "default"),
            ("Export sheet", self.export_sheet, "default"),
        ]:
            btn = AnimatedButton(label, kind=kind)
            btn.clicked.connect(callback)
            layout.addWidget(btn)

        return sidebar

    def _build_content_panel(self):
        self.content_panel = QWidget()
        content_layout = QVBoxLayout(self.content_panel)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)

        self.editor_wrap = GlassPanel(radius=14)
        self.editor_wrap.setGraphicsEffect(make_shadow(20, 100, 4))
        editor_layout = QVBoxLayout(self.editor_wrap)
        editor_layout.setContentsMargins(4, 4, 4, 4)

        self.editor = QTextEdit()
        self.editor.setStyleSheet(f"""
            QTextEdit {{
                background: rgba(0,0,0,0.22);
                color: {TEXT_PRIMARY};
                border: none;
                border-radius: 11px;
                padding: 14px;
                font-family: '{MONO_FAMILY}';
                font-size: 13px;
                selection-background-color: rgba(126,232,184,0.3);
            }}
        """)
        self.editor.textChanged.connect(self.on_editor_changed)
        editor_layout.addWidget(self.editor)
        content_layout.addWidget(self.editor_wrap, 1)

        self.notes_wrap = GlassPanel(radius=14)
        self.notes_wrap.setGraphicsEffect(make_shadow(20, 100, 4))
        notes_wrap_layout = QVBoxLayout(self.notes_wrap)
        notes_wrap_layout.setContentsMargins(4, 4, 4, 4)

        self.sheet_notes = QWidget()
        self.sheet_notes.setStyleSheet("background: transparent;")
        self.sheet_notes_layout = QVBoxLayout(self.sheet_notes)
        self.sheet_notes_layout.setContentsMargins(14, 14, 14, 14)
        self.sheet_notes_layout.setSpacing(6)

        self.notes_scroll = QScrollArea()
        self.notes_scroll.setWidget(self.sheet_notes)
        self.notes_scroll.setWidgetResizable(True)
        self.notes_scroll.setStyleSheet("""
            QScrollArea { background: rgba(0,0,0,0.22); border:none; border-radius:11px; }
            QScrollBar:vertical { background: transparent; width: 10px; margin: 4px; }
            QScrollBar::handle:vertical { background: rgba(255,255,255,0.14); border-radius: 5px; min-height: 30px; }
            QScrollBar::handle:vertical:hover { background: rgba(255,255,255,0.24); }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
        """)
        self.notes_scroll.setVisible(False)
        notes_wrap_layout.addWidget(self.notes_scroll)
        content_layout.addWidget(self.notes_wrap, 1)
        self.notes_wrap.setVisible(False)

        return self.content_panel

    def _build_controls(self):
        self.controls = GlassPanel(radius=14)
        self.controls.setGraphicsEffect(make_shadow(20, 100, 4))
        layout = QHBoxLayout(self.controls)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)

        self.transport_buttons = {}
        for key, label, callback, kind, enabled in [
            ("start", "▶  start   f6", self.start, "accent", True),
            ("stop", "■  stop   f7", self.stop, "danger", True),
            ("pause", "❚❚  pause   f9", self.toggle_pause, "default", False),
            ("submit", "submit", self.submit, "default", True),
            ("edit", "edit", self.edit, "default", True),
            ("exit", "exit   f8", self.exit_program, "default", True),
            ("log", "view log", self.show_log, "default", True),
        ]:
            btn = AnimatedButton(label, kind=kind)
            btn.clicked.connect(callback)
            btn.setEnabled(enabled)
            btn.setMinimumWidth(70)
            self.transport_buttons[key] = btn
            layout.addWidget(btn)

        layout.addStretch()

        tempo_label = QLabel("tempo")
        tempo_label.setStyleSheet(f"color:{TEXT_SECONDARY}; font-size:12px; font-weight:600;")
        layout.addWidget(tempo_label)

        self.tempo_value_label = QLabel(str(self.tempo))
        self.tempo_value_label.setStyleSheet(f"color:{ACCENT}; font-size:12px; font-weight:700; min-width:30px;")
        layout.addWidget(self.tempo_value_label)

        self.tempo_slider = QSlider(Qt.Horizontal)
        self.tempo_slider.setRange(MIN_TEMPO, MAX_TEMPO)
        self.tempo_slider.setValue(self.tempo)
        self.tempo_slider.valueChanged.connect(self.set_tempo)
        self.tempo_slider.setMinimumWidth(190)
        self.tempo_slider.setStyleSheet(self._slider_style())
        layout.addWidget(self.tempo_slider)

        self.timing_down_button = AnimatedButton("-10%")
        self.timing_down_button.setFixedWidth(82)
        self.timing_down_button.clicked.connect(lambda: self.apply_selection_timing(0.9))
        layout.addWidget(self.timing_down_button)

        self.timing_up_button = AnimatedButton("+10%")
        self.timing_up_button.setFixedWidth(82)
        self.timing_up_button.clicked.connect(lambda: self.apply_selection_timing(1.1))
        layout.addWidget(self.timing_up_button)

        return self.controls

    def _build_status_bar(self):
        self.status_widget = GlassPanel(radius=12)
        layout = QHBoxLayout(self.status_widget)
        layout.setContentsMargins(16, 9, 16, 9)

        self.status_dot = PulseDot()
        self.status_dot.start()
        layout.addWidget(self.status_dot)

        self.status_label = QLabel(self.status)
        self.status_label.setStyleSheet(f"color:{TEXT_PRIMARY}; font-weight:600; font-size:12px; margin-left:6px;")
        layout.addWidget(self.status_label)
        layout.addStretch()

        self.position_label = QLabel(self.position)
        self.position_label.setStyleSheet(f"color:{TEXT_FAINT}; font-size:12px;")
        layout.addWidget(self.position_label)

        return self.status_widget

    def _build_loop_panel(self):
        self.loop_panel = GlassPanel(radius=12)
        layout = QHBoxLayout(self.loop_panel)
        layout.setContentsMargins(16, 9, 16, 9)
        layout.setSpacing(8)

        self.loop_checkbox = QCheckBox("repeat playback")
        self.loop_checkbox.setChecked(self.loop_enabled)
        self.loop_checkbox.toggled.connect(self.set_loop_enabled)
        self.loop_checkbox.setStyleSheet(f"""
            QCheckBox {{ color:{TEXT_SECONDARY}; font-size:12px; font-weight:600; spacing:8px; }}
            QCheckBox::indicator {{ width:16px; height:16px; border-radius:4px; border:1px solid rgba(255,255,255,0.25); background: rgba(255,255,255,0.04); }}
            QCheckBox::indicator:checked {{ background:{ACCENT}; border-color:{ACCENT}; }}
        """)
        layout.addWidget(self.loop_checkbox)

        self.loop_scope_box = QComboBox()
        self.loop_scope_box.addItems(["whole sheet", "selected range"])
        self.loop_scope_box.setCurrentText(self.loop_scope)
        self.loop_scope_box.currentTextChanged.connect(self.set_loop_scope)
        self.loop_scope_box.setStyleSheet(self._combo_style())
        layout.addWidget(self.loop_scope_box)

        layout.addWidget(self._section_label("from"))
        self.loop_start_box = QLineEdit(self.loop_start)
        self.loop_start_box.setFixedWidth(50)
        self.loop_start_box.setStyleSheet(self._line_edit_style())
        layout.addWidget(self.loop_start_box)

        layout.addWidget(self._section_label("to"))
        self.loop_end_box = QLineEdit(self.loop_end)
        self.loop_end_box.setFixedWidth(50)
        self.loop_end_box.setStyleSheet(self._line_edit_style())
        layout.addWidget(self.loop_end_box)

        layout.addWidget(self._section_label("plays (0 = infinite)"))
        self.loop_repeats_box = QLineEdit(self.loop_repeats)
        self.loop_repeats_box.setFixedWidth(50)
        self.loop_repeats_box.setStyleSheet(self._line_edit_style())
        layout.addWidget(self.loop_repeats_box)

        layout.addStretch()
        return self.loop_panel

    # ── style helpers ────────────────────────────────────────────────────

    def _combo_style(self):
        return f"""
            QComboBox {{
                background: rgba(255,255,255,0.05);
                color: {TEXT_PRIMARY};
                border: 1px solid rgba(255,255,255,0.12);
                border-radius: 7px;
                padding: 6px 10px;
                font-size: 12px;
                font-weight: 600;
            }}
            QComboBox:hover {{ border-color: rgba(255,255,255,0.24); }}
            QComboBox::drop-down {{ border: none; width: 22px; }}
            QComboBox QAbstractItemView {{
                background: #14181f;
                color: {TEXT_PRIMARY};
                border: 1px solid rgba(255,255,255,0.14);
                selection-background-color: rgba(126,232,184,0.2);
                outline: 0;
            }}
        """

    def _line_edit_style(self):
        return f"""
            QLineEdit {{
                background: rgba(255,255,255,0.05);
                color: {TEXT_PRIMARY};
                border: 1px solid rgba(255,255,255,0.12);
                border-radius: 7px;
                padding: 5px 8px;
                font-size: 12px;
                font-weight: 600;
            }}
            QLineEdit:focus {{ border-color: {ACCENT}; }}
        """

    def _slider_style(self):
        return f"""
            QSlider::groove:horizontal {{
                height: 4px;
                background: rgba(255,255,255,0.12);
                border-radius: 2px;
            }}
            QSlider::sub-page:horizontal {{
                background: {ACCENT};
                border-radius: 2px;
            }}
            QSlider::handle:horizontal {{
                background: {TEXT_PRIMARY};
                width: 14px;
                height: 14px;
                margin: -5px 0;
                border-radius: 7px;
            }}
            QSlider::handle:horizontal:hover {{
                background: {ACCENT};
            }}
        """

    # ── sidebar animation ────────────────────────────────────────────────

    def toggle_sidebar(self):
        self.sidebar_visible = not self.sidebar_visible
        target_width = self._sidebar_width if self.sidebar_visible else 0

        if self._sidebar_anim is not None:
            self._sidebar_anim.stop()
        self._sidebar_anim = QPropertyAnimation(self.sidebar, b"maximumWidth")
        self._sidebar_anim.setDuration(240)
        self._sidebar_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._sidebar_anim.setStartValue(self.sidebar.maximumWidth() if self.sidebar.maximumWidth() < 10000 else self.sidebar.width())
        self._sidebar_anim.setEndValue(target_width)
        self._sidebar_anim.start()

        self.sidebar_toggle_button.setText("show sheets" if not self.sidebar_visible else "hide sheets")
        self.save_library()

    # ── data / behavior (unchanged from original) ───────────────────────

    def set_tempo(self, value):
        self.tempo = value
        self.tempo_value_label.setText(str(value))
        self.save_library()

    def apply_selection_timing(self, scale_factor):
        if self.editing:
            selection = self.selected_events or {self.selected_event}
            if not self.events:
                self.set_status("there is no sheet to edit", is_error=True)
                return
            for idx in sorted(selection):
                if 0 <= idx < len(self.events):
                    apply_timing_scale(self.events[idx], scale_factor)
            self.render_notes()
            self.set_status(f"timing x{scale_factor:.2f} on {len(selection)} event(s)")
            return
        self.set_status("submit the sheet to edit timing", is_error=True)

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
        self.save_active_sheet_settings()
        self.save_current_sheet()
        self.sheet_library.set_active_sheet_name(sheet_name)
        self.apply_sheet_settings(sheet_name)
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

    def save_active_sheet_settings(self):
        sheet_name = self.sheet_library.get_active_sheet_name()
        self.sheet_library.set_sheet_settings(sheet_name, {
            "loop_enabled": self.loop_enabled,
            "loop_end": self.loop_end,
            "loop_repeats": self.loop_repeats,
            "loop_scope": self.loop_scope,
            "loop_start": self.loop_start,
            "tempo": self.tempo,
        })

    def apply_sheet_settings(self, sheet_name):
        self.tempo = self.sheet_library.get_sheet_setting(sheet_name, "tempo", DEFAULT_TEMPO)
        self.loop_enabled = self.sheet_library.get_sheet_setting(sheet_name, "loop_enabled", False)
        self.loop_scope = self.sheet_library.get_sheet_setting(sheet_name, "loop_scope", "whole sheet")
        self.loop_start = str(self.sheet_library.get_sheet_setting(sheet_name, "loop_start", 1))
        self.loop_end = str(self.sheet_library.get_sheet_setting(sheet_name, "loop_end", 1))
        self.loop_repeats = str(self.sheet_library.get_sheet_setting(sheet_name, "loop_repeats", 1))
        if hasattr(self, "tempo_slider"):
            self.tempo_slider.blockSignals(True)
            self.tempo_slider.setValue(self.tempo)
            self.tempo_slider.blockSignals(False)
        if hasattr(self, "tempo_value_label"):
            self.tempo_value_label.setText(str(self.tempo))
        if hasattr(self, "loop_checkbox"):
            self.loop_checkbox.blockSignals(True)
            self.loop_checkbox.setChecked(self.loop_enabled)
            self.loop_checkbox.blockSignals(False)
        if hasattr(self, "loop_scope_box"):
            self.loop_scope_box.blockSignals(True)
            self.loop_scope_box.setCurrentText(self.loop_scope)
            self.loop_scope_box.blockSignals(False)
        if hasattr(self, "loop_start_box"):
            self.loop_start_box.setText(self.loop_start)
        if hasattr(self, "loop_end_box"):
            self.loop_end_box.setText(self.loop_end)
        if hasattr(self, "loop_repeats_box"):
            self.loop_repeats_box.setText(self.loop_repeats)

    def save_library(self):
        self.save_active_sheet_settings()
        self.sheet_library.set_settings({
            "sidebar_visible": self.sidebar_visible,
            "target_mode": self.target_mode,
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
        self.apply_sheet_settings(self.sheet_library.get_active_sheet_name())
        self.editing = True
        self.notes_wrap.setVisible(False)
        self.editor_wrap.setVisible(True)
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
        file_path, _ = QFileDialog.getOpenFileName(self, "Import sheet", "", "Piano sheet (*.piano-sheet.json *.txt *.mid *.midi)")
        if not file_path:
            return

        if file_path.lower().endswith((".mid", ".midi")):
            try:
                sheet_text = midi_to_sheet_text(file_path)
            except Exception as error:
                QMessageBox.critical(self, "Cannot import MIDI", f"could not read MIDI file: {error}")
                return
            imported_sheet = {"name": os.path.splitext(os.path.basename(file_path))[0], "sheet": sheet_text}
            self.show_validation_preview(imported_sheet["sheet"], "Import preview", "Import sheet", lambda _events: self.finish_import(imported_sheet))
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
        self.set_status(message, is_error=True)

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
        self.editor_wrap.setVisible(False)
        self.notes_wrap.setVisible(True)
        self.notes_scroll.setVisible(True)
        self.render_notes()
        self.set_status(f"ready • {len(self.events)} notes")

    def edit(self):
        if self.player.playing:
            return
        self.editing = True
        self.drag_selecting = False
        self.drag_start_index = None
        self.notes_wrap.setVisible(False)
        self.editor_wrap.setVisible(True)
        self.set_status("editing")

    def render_notes(self):
        while self.sheet_notes_layout.count():
            child = self.sheet_notes_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
        self.note_buttons = {}
        self.selected_events = set()

        lines = {}
        for index, event in enumerate(self.events):
            lines.setdefault(event["line"], []).append((index, event))

        max_line = max(lines.keys(), default=0)
        for line_number in range(max_line + 1):
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(4)
            line_label = QLabel(f"{line_number + 1:03}")
            line_label.setStyleSheet(f"color:{TEXT_FAINT}; min-width: 30px; font-family:'{MONO_FAMILY}'; font-size:11px;")
            row_layout.addWidget(line_label)
            for index, event in lines.get(line_number, []):
                pill = NotePill(event["source"], event["type"] == "rest")
                pill.clicked.connect(lambda _checked, idx=index: self.select_note(idx))
                pill.pressed.connect(lambda _checked, idx=index: self.begin_drag_selection(idx))
                pill.mouseMoveEvent = lambda event, idx=index: self.drag_select_note(event, idx)
                row_layout.addWidget(pill)
                self.note_buttons[index] = pill
            row_layout.addStretch()
            self.sheet_notes_layout.addWidget(row)

        self.update_note_styles()

    def begin_drag_selection(self, index):
        if self.editing:
            self.drag_selecting = True
            self.drag_start_index = index
            self.selected_events = {index}
            self.selected_event = index
            self.update_note_styles()

    def drag_select_note(self, event, index):
        if not self.drag_selecting or not self.editing:
            return
        if index == self.drag_start_index:
            return
        start, end = sorted((self.drag_start_index, index))
        self.selected_events = set(range(start, end + 1))
        self.selected_event = index
        self.update_note_styles()

    def update_note_styles(self):
        if not self.note_buttons:
            return

        next_states = {}
        for idx, pill in self.note_buttons.items():
            if self.editing and self.selected_events and idx in self.selected_events:
                next_states[idx] = "current"
            elif idx < self.selected_event:
                next_states[idx] = "played"
            elif idx == self.selected_event:
                next_states[idx] = "current"
            else:
                next_states[idx] = "upcoming"

        for idx, pill in self.note_buttons.items():
            state = next_states[idx]
            previous_state = self._last_note_states.get(idx)
            if previous_state != state:
                pill.set_state(state)
                self._last_note_states[idx] = state

        self.position = f"{self.selected_event + 1} / {len(self.events)}"
        self.position_label.setText(self.position)

    def select_note(self, index):
        if self.editing:
            if self.drag_selecting:
                return
            self.selected_event = index
            self.selected_events = {index}
            self.update_note_styles()
            self.scroll_to_note(index)
            self.set_status(f"selected note {index + 1}")
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
        self.transport_buttons["pause"].setEnabled(False)
        self.status_dot.set_color(ACCENT)
        self.status_dot_small.set_color(ACCENT)
        self.set_status("finished" if not was_stopped else "stopped")

    def set_status(self, message, is_error=False):
        status_key = (message, is_error)
        if status_key == self._last_status_key:
            return
        self._last_status_key = status_key
        self.status = message
        self.status_label.setText(message)
        color = DANGER if is_error else ACCENT
        self.status_dot.set_color(color)
        self.status_dot_small.set_color(color)

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
                self.set_status("pick a window first", is_error=True)
                return
            if not self.active_target.is_alive():
                self.set_status("target window closed - pick again", is_error=True)
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
        self.transport_buttons["pause"].setEnabled(True)
        self.set_status(f"playing notes {start_index + 1}–{end_index + 1}")

    def stop(self):
        self.player.stop()
        self.transport_buttons["pause"].setEnabled(False)
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
            self.set_status("enter a whole number of plays", is_error=True)
            return None
        if repeat_count < 0:
            self.set_status("plays cannot be negative", is_error=True)
            return None
        if self.loop_scope == "whole sheet":
            return 0, len(self.events) - 1, repeat_count
        try:
            range_start = int(self.loop_start) - 1
            range_end = int(self.loop_end) - 1
        except ValueError:
            self.set_status("enter whole-number range limits", is_error=True)
            return None
        if not 0 <= range_start <= range_end < len(self.events):
            self.set_status(f"range must be between 1 and {len(self.events)}", is_error=True)
            return None
        return range_start, range_end, repeat_count

    def show_log(self):
        try:
            with open(paths.LOG_PATH, "r", encoding="utf-8") as handle:
                content = handle.read()
        except FileNotFoundError:
            content = "no log written yet - hit start first"
        QMessageBox.information(self, "debug log", content)

    def check_for_updates(self):
        try:
            update = updater.check_for_update(CURRENT_VERSION)
        except Exception:
            log.exception("update check crashed")
            return

        if update is None:
            return

        skipped_version = None
        try:
            with open(paths.SKIPPED_VERSION_PATH, "r", encoding="utf-8") as handle:
                skipped_version = handle.read().strip()
        except FileNotFoundError:
            skipped_version = None
        except OSError:
            log.exception("failed to read skipped version file")
            skipped_version = None

        if skipped_version == update.version and not update.mandatory:
            return

        dialog = QMessageBox(self)
        dialog.setWindowTitle("update available")
        dialog.setText(f"Version {update.version} is available.")
        message = "This will download the new installer, uninstall the old version, and relaunch the app."
        if update.changelog:
            message = f"{message}\n\n{update.changelog}"
        dialog.setInformativeText(message)
        dialog.setIcon(QMessageBox.Information)

        if update.mandatory:
            dialog.setStandardButtons(QMessageBox.StandardButton.Ok)
        else:
            dialog.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            dialog.button(QMessageBox.StandardButton.Yes).setText("update now")
            dialog.button(QMessageBox.StandardButton.No).setText("remind later")

        result = dialog.exec()

        if update.mandatory:
            accepted = result == QMessageBox.StandardButton.Ok
        else:
            accepted = result == QMessageBox.StandardButton.Yes

        if not accepted:
            if not update.mandatory:
                try:
                    with open(paths.SKIPPED_VERSION_PATH, "w", encoding="utf-8") as handle:
                        handle.write(update.version)
                except OSError:
                    log.exception("failed to persist skipped version")
            return

        self.set_status("downloading update…")
        try:
            self._download_and_apply_update(update)
        except Exception:
            log.exception("update install failed")
            self.set_status("update failed - see log", is_error=True)
            QMessageBox.critical(
                self,
                "update failed",
                "The new installer could not be downloaded or launched. Please retry later.",
            )

    def _download_and_apply_update(self, update):
        dialog = QProgressDialog("Downloading installer…", "Cancel", 0, 0, self)
        dialog.setWindowModality(Qt.WindowModal)
        dialog.setMinimumDuration(0)
        dialog.show()

        def progress(bytes_read, total):
            if total > 0:
                dialog.setValue(int(bytes_read / total * 100))
            QApplication.processEvents()

        temp_installer = updater.download_update(update.download_url, progress_callback=progress)
        dialog.close()

        if not updater.apply_update_and_relaunch(temp_installer):
            raise RuntimeError("installer update launcher failed")

        self.exit_program()

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
            self.set_status(f"hotkey error: {error}", is_error=True)

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
    app.setStyle("Fusion")
    font = QFont(FONT_FAMILY, 10)
    app.setFont(font)
    window = App()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
import ctypes
import logging
import os
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

import keyboard
import psutil
import pydirectinput
import win32con
import win32gui
import win32process

import updater

CURRENT_VERSION = "1.0.0"
LOG_PATH = "piano_player.log"

logging.basicConfig(
    filename=LOG_PATH,
    filemode="w",
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

log = logging.getLogger("piano")


def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        log.exception("admin check failed")
        return False


def relaunch_as_admin():
    """
    re-launches this same script elevated via the UAC prompt.
    the current (non-elevated) process keeps running underneath -
    caller is expected to close it right after calling this.
    """
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

        # ShellExecuteW returns <= 32 on failure (e.g. user clicked "no" on UAC)
        log.info("relaunch_as_admin ShellExecuteW result=%s", result)

        return result > 32

    except Exception:
        log.exception("relaunch_as_admin failed")
        return False


pydirectinput.PAUSE = 0
pydirectinput.FAILSAFE = True


DEFAULT_TEMPO = 120
MIN_TEMPO = 30
MAX_TEMPO = 300

START_HOTKEY = "f6"
STOP_HOTKEY = "f7"
PAUSE_HOTKEY = "f9"
EXIT_HOTKEY = "f8"

# how long to wait after focusing a window before trusting it's actually focused
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


class WindowTarget:
    """holds the hwnd/pid pair for whichever window input should go to"""

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
        """
        bring this window to the foreground so SendInput actually
        reaches it. windows blocks SetForegroundWindow from apps that
        aren't already focused, so we use the alt-key tap trick to
        satisfy that restriction first. returns (success, reason).
        """
        if not self.is_alive():
            return False, "window no longer exists"

        try:
            if win32gui.IsIconic(self.hwnd):
                win32gui.ShowWindow(self.hwnd, win32con.SW_RESTORE)

            # workaround for SetForegroundWindow's focus-stealing lockout
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
    """returns WindowTarget entries for visible top-level windows with a title"""
    results = []
    seen_pids_titles = set()

    def callback(hwnd, _extra):
        if not win32gui.IsWindowVisible(hwnd):
            return

        title = win32gui.GetWindowText(hwnd)

        if not title.strip():
            return

        # skip windows with no size (tray/helper windows)
        rect = win32gui.GetWindowRect(hwnd)

        if rect[2] - rect[0] <= 0 or rect[3] - rect[1] <= 0:
            return

        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            process = psutil.Process(pid)
            exe_name = process.name()
        except (psutil.NoSuchProcess, psutil.AccessDenied) as error:
            # protected/elevated processes (antivirus, some games, system
            # windows) refuse the query - this is expected and not worth
            # surfacing to the user, but log it so "why isn't my window
            # showing up in the list" is answerable from the log instead
            # of a total mystery
            log.debug(
                "skipping window %r (pid=%s): %s",
                title,
                pid,
                error,
            )
            return
        except Exception:
            log.exception(
                "unexpected error inspecting window %r (hwnd=%s)",
                title,
                hwnd,
            )
            return

        key = (pid, title)

        if key in seen_pids_titles:
            return

        seen_pids_titles.add(key)

        results.append(
            WindowTarget(hwnd, pid, title, exe_name)
        )

    win32gui.EnumWindows(callback, None)

    results.sort(key=lambda w: w.exe_name.lower())

    return results


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

        lines = (
            text
            .replace("\r\n", "\n")
            .replace("\r", "\n")
            .split("\n")
        )

        for line_number, line in enumerate(lines):
            i = 0

            while i < len(line):
                char = line[i]

                # normal spaces become timing gaps
                if char == " ":
                    events.append({
                        "type": "rest",
                        "length": 0.35,
                        "source": " ",
                        "line": line_number,
                    })
                    i += 1
                    continue

                # dashes become longer timing gaps
                if char == "-":
                    count = 0

                    while i < len(line) and line[i] == "-":
                        count += 1
                        i += 1

                    events.append({
                        "type": "rest",
                        "length": float(count),
                        "source": "-" * count,
                        "line": line_number,
                    })
                    continue

                # chords
                if char == "[":
                    end = line.find("]", i + 1)

                    if end == -1:
                        raise ValueError(
                            f"missing ] on line {line_number + 1}"
                        )

                    contents = line[i + 1:end]
                    actions = []

                    for item in contents:
                        if item.isspace():
                            continue

                        action = cls.key_to_action(item)

                        if action is None:
                            raise ValueError(
                                f"unsupported character {item!r} "
                                f"on line {line_number + 1}"
                            )

                        actions.append(action)

                    if not actions:
                        raise ValueError(
                            f"empty chord on line {line_number + 1}"
                        )

                    events.append({
                        "type": "chord",
                        "actions": actions,
                        "source": line[i:end + 1],
                        "line": line_number,
                    })

                    i = end + 1
                    continue

                # optional fast runs
                if char == "{":
                    end = line.find("}", i + 1)

                    if end == -1:
                        raise ValueError(
                            f"missing }} on line {line_number + 1}"
                        )

                    contents = line[i + 1:end]
                    actions = []

                    for item in contents:
                        if item.isspace():
                            continue

                        action = cls.key_to_action(item)

                        if action is None:
                            raise ValueError(
                                f"unsupported character {item!r} "
                                f"on line {line_number + 1}"
                            )

                        actions.append(action)

                    if not actions:
                        raise ValueError(
                            f"empty run on line {line_number + 1}"
                        )

                    events.append({
                        "type": "run",
                        "actions": actions,
                        "source": line[i:end + 1],
                        "line": line_number,
                    })

                    i = end + 1
                    continue

                action = cls.key_to_action(char)

                if action is None:
                    raise ValueError(
                        f"unsupported character {char!r} "
                        f"on line {line_number + 1}"
                    )

                events.append({
                    "type": "note",
                    "actions": [action],
                    "source": char,
                    "line": line_number,
                })

                i += 1

        return events


class Player:
    # how much of a note/chord's beat slot is spent held down vs
    # released before the next event - 0.9 reads as legato/connected,
    # closer to 0.5 would sound more staccato/detached
    HOLD_RATIO = 0.9

    # seconds of real-world slack subtracted from every sleep to
    # absorb python/OS scheduling jitter - keeps us from ever
    # sleeping slightly too long and drifting late
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
            result_shift = pydirectinput.keyDown("shift")
            result_key = pydirectinput.keyDown(key)
        else:
            result_shift = None
            result_key = pydirectinput.keyDown(key)

        log.debug(
            "hold_action key=%s shift=%s -> key_result=%s shift_result=%s",
            key,
            shift,
            result_key,
            result_shift,
        )

    @staticmethod
    def release_action(action):
        key, shift = action

        if shift:
            result_key = pydirectinput.keyUp(key)
            result_shift = pydirectinput.keyUp("shift")
        else:
            result_key = pydirectinput.keyUp(key)
            result_shift = None

        log.debug(
            "release_action key=%s shift=%s -> key_result=%s shift_result=%s",
            key,
            shift,
            result_key,
            result_shift,
        )

    @classmethod
    def press_chord_down(cls, actions):
        """
        presses every key in a chord down as close to simultaneously
        as pydirectinput allows. returns nothing - caller times the hold.
        """
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
        keys = (
            "abcdefghijklmnopqrstuvwxyz"
            "0123456789"
            "`-=[]\\;',./"
            "shift"
        )

        for key in keys:
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

    def start(
        self,
        events,
        start_index,
        end_index,
        repeat_count,
        tempo,
        target,
    ):
        if self.playing:
            log.debug("start() ignored - already playing")
            return

        self.events = events
        self.target = target
        self.playing = True
        self.stop_requested = False
        self.paused = False

        log.info(
            "starting playback: start=%s end=%s repeats=%s tempo=%s target=%s events=%s",
            start_index,
            end_index,
            repeat_count,
            tempo,
            target,
            len(self.events),
        )

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
            log.warning("target window no longer alive")
            self.on_error("target window closed")
            return False

        # only refocus if we've lost it - avoids stealing focus every single note
        current_fg = win32gui.GetForegroundWindow()

        if current_fg != self.target.hwnd:
            log.debug(
                "foreground mismatch (current=%s target=%s) - refocusing",
                current_fg,
                self.target.hwnd,
            )

            success, reason = self.target.focus()

            if not success:
                log.warning("focus failed: %s", reason)
                self.on_error(f"stopped: {reason}")
                return False

        return True

    def _sleep_until(self, clock_start, target_beats, beat_seconds):
        """
        sleeps until clock_start + target_beats*beat_seconds, using an
        absolute wall-clock target rather than a fixed-duration sleep.
        this is what keeps 858 events from drifting - every sleep is
        computed against real elapsed time, not stacked estimates, so
        occasional slow syscalls (pydirectinput/win32 calls) don't
        compound into the song running progressively later.
        """
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

    def _run(self, start_index, end_index, repeat_count, tempo):
        beat_seconds = 60.0 / tempo

        # cumulative beat position at the start of each event, computed
        # once up front so scheduling is just "where should we be by
        # now" instead of "sleep this long, then this long, then..."
        beat_offsets = [0.0] * len(self.events)
        cursor = 0.0

        for index, event in enumerate(self.events):
            beat_offsets[index] = cursor
            cursor += self._event_beats(event)

        try:
            completed_plays = 0

            while repeat_count == 0 or completed_plays < repeat_count:
                clock_start = (
                    time.perf_counter()
                    - beat_offsets[start_index] * beat_seconds
                )

                for index in range(start_index, end_index + 1):
                    if self.stop_requested:
                        log.debug("stop requested at index %s", index)
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

                    log.debug(
                        "event %s/%s type=%s source=%r beats=%.3f",
                        index,
                        end_index,
                        event["type"],
                        event["source"],
                        event_beats,
                    )

                    self.on_note(index)

                    if not self._ensure_focus():
                        log.warning("aborting playback - focus check failed at index %s", index)
                        self.stop()
                        break

                    if event["type"] == "rest":
                        clock_start = self._sleep_until(
                            clock_start,
                            event_start_beats + event_beats,
                            beat_seconds,
                        )

                    elif event["type"] == "note":
                        action = event["actions"][0]

                        log.debug("key down: %s", action)
                        self.hold_action(action)

                        clock_start = self._sleep_until(
                            clock_start,
                            event_start_beats + event_beats * self.HOLD_RATIO,
                            beat_seconds,
                        )

                        if clock_start is None:
                            break

                        log.debug("key up: %s", action)
                        self.release_action(action)

                        clock_start = self._sleep_until(
                            clock_start,
                            event_start_beats + event_beats,
                            beat_seconds,
                        )

                    elif event["type"] == "chord":
                        log.debug("chord: %s", event["actions"])
                        self.press_chord_down(event["actions"])

                        clock_start = self._sleep_until(
                            clock_start,
                            event_start_beats + event_beats * self.HOLD_RATIO,
                            beat_seconds,
                        )

                        if clock_start is None:
                            break

                        self.press_chord_up(event["actions"])

                        clock_start = self._sleep_until(
                            clock_start,
                            event_start_beats + event_beats,
                            beat_seconds,
                        )

                    elif event["type"] == "run":
                        actions = event["actions"]
                        per_key_beats = event_beats / max(len(actions), 1)

                        for key_index, action in enumerate(actions):
                            if self.stop_requested:
                                break

                            log.debug("run key: %s", action)
                            self.hold_action(action)

                            key_start = event_start_beats + key_index * per_key_beats

                            clock_start = self._sleep_until(
                                clock_start,
                                key_start + per_key_beats * self.HOLD_RATIO,
                                beat_seconds,
                            )

                            if clock_start is None:
                                break

                            self.release_action(action)

                            clock_start = self._sleep_until(
                                clock_start,
                                key_start + per_key_beats,
                                beat_seconds,
                            )

                    if self.stop_requested or clock_start is None:
                        break

                if self.stop_requested or clock_start is None:
                    break

                completed_plays += 1
                log.info("playback completed pass %s", completed_plays)

            if not self.stop_requested:
                log.info("playback reached end of selection")

        except Exception:
            # this is the fix for "dies silently" - any exception in the
            # loop above used to just vanish because there was no except
            # clause, only finally. now it's logged AND shown to the user.
            log.exception("playback crashed")
            self.on_error("playback crashed - check piano_player.log")

        finally:
            was_stopped = self.stop_requested
            log.debug("playback loop ending, releasing all keys")
            self.release_everything()
            self.playing = False
            self.stop_requested = False
            self.paused = False
            self.on_finished(was_stopped)

    @staticmethod
    def _event_beats(event):
        """
        beat-length of a single event slot. computed from the sheet's
        own notation (rest length, run size) rather than a flat magic
        constant, so a triple-dash rest is genuinely 3x a single dash,
        and every note/chord occupies exactly one beat.
        """
        if event["type"] == "rest":
            # a bare space is a short breath - shorter than a full beat.
            # dashes are explicit rest counts from the sheet itself.
            return event["length"] if event["source"] != " " else 0.5

        # notes, chords, and runs each occupy one full beat's slot
        return 1.0


class NoteWidget:
    def __init__(
        self,
        parent,
        app,
        event_index,
        event,
    ):
        self.app = app
        self.event_index = event_index
        self.event = event

        self.frame = tk.Frame(
            parent,
            bg="#181c22",
            highlightthickness=1,
            highlightbackground="#292f37",
            cursor="hand2",
        )

        self.label = tk.Label(
            self.frame,
            text=event["source"],
            font=("cascadia mono", 11, "bold"),
            fg="#787f88",
            bg="#181c22",
            padx=7,
            pady=6,
            cursor="hand2",
        )

        self.label.pack()

        self.frame.bind(
            "<Enter>",
            self.mouse_enter,
        )

        self.frame.bind(
            "<Leave>",
            self.mouse_leave,
        )

        self.frame.bind(
            "<Button-1>",
            self.clicked,
        )

        self.label.bind(
            "<Enter>",
            self.mouse_enter,
        )

        self.label.bind(
            "<Leave>",
            self.mouse_leave,
        )

        self.label.bind(
            "<Button-1>",
            self.clicked,
        )

    def mouse_enter(self, _event):
        if self.event_index == self.app.selected_event:
            return

        self.frame.configure(
            bg="#252b33",
            highlightbackground="#3d4651",
        )

        self.label.configure(
            bg="#252b33",
            fg="#cfd4da",
        )

    def mouse_leave(self, _event):
        if self.event_index == self.app.selected_event:
            return

        self.set_neutral()

    def clicked(self, _event):
        self.app.select_note(
            self.event_index
        )

    def set_neutral(self):
        if self.event["type"] == "rest":
            fg = "#454b53"
        else:
            fg = "#6f767f"

        self.frame.configure(
            bg="#181c22",
            highlightbackground="#292f37",
        )

        self.label.configure(
            bg="#181c22",
            fg=fg,
        )

    def set_played(self):
        self.frame.configure(
            bg="#15181d",
            highlightbackground="#20252b",
        )

        self.label.configure(
            bg="#15181d",
            fg="#464c54",
        )

    def set_next(self):
        self.frame.configure(
            bg="#181c22",
            highlightbackground="#292f37",
        )

        self.label.configure(
            bg="#181c22",
            fg="#ffffff",
        )

    def set_current(self):
        self.frame.configure(
            bg="#247f4c",
            highlightbackground="#55d988",
        )

        self.label.configure(
            bg="#247f4c",
            fg="#ffffff",
        )


class App:
    def __init__(self, root):
        self.root = root

        self.root.title(
            "virtual piano player"
        )

        self.root.geometry(
            "1080x800"
        )

        self.root.minsize(
            800,
            640,
        )

        self.root.configure(
            bg="#101216"
        )

        self.events = []
        self.note_widgets = []
        self.selected_event = 0
        self.playback_start_index = 0

        self.editing = True
        self.closing = False

        # true while the "not running as admin" overlay is covering the
        # window - the overlay blocks mouse clicks on the buttons
        # underneath it, but the F6/F7/F8 hotkeys are global and don't
        # go through tkinter at all, so without this flag F6 could
        # still start playback while the overlay is up
        self.overlay_active = False

        self.tempo = tk.IntVar(
            value=DEFAULT_TEMPO
        )

        self.loop_enabled = tk.BooleanVar(value=False)
        self.loop_scope = tk.StringVar(value="whole sheet")
        self.loop_start = tk.StringVar(value="1")
        self.loop_end = tk.StringVar(value="1")
        self.loop_repeats = tk.StringVar(value="1")

        self.status = tk.StringVar(
            value="paste a sheet"
        )

        self.position = tk.StringVar(
            value="0 / 0"
        )

        self.target_mode = tk.StringVar(
            value="foreground"
        )

        self.selected_window_label = tk.StringVar(
            value="(none selected)"
        )

        self.window_targets = []
        self.active_target = None

        self.build_ui()

        self.player = Player(
            self.note_changed,
            self.player_error,
            self.playback_finished,
        )

        self.root.protocol(
            "WM_DELETE_WINDOW",
            self.exit_program,
        )

        threading.Thread(
            target=self.hotkey_loop,
            daemon=True,
        ).start()

        if not is_admin():
            log.warning("not running as admin - showing elevation prompt")
            # deferred slightly so the main window has finished laying
            # itself out before the overlay measures/covers it
            self.root.after(50, self.show_admin_overlay)
        else:
            log.info("running as admin")

        # update check runs on a background thread since it hits the
        # network - never block the window from opening while waiting
        # on github, and never crash startup if the request fails
        threading.Thread(
            target=self.check_for_update_background,
            daemon=True,
        ).start()

    def build_ui(self):
        header = tk.Frame(
            self.root,
            bg="#101216",
        )

        header.pack(
            fill="x",
            padx=22,
            pady=(18, 10),
        )

        tk.Label(
            header,
            text="virtual piano player",
            font=(
                "segoe ui",
                21,
                "bold",
            ),
            fg="#f2f4f7",
            bg="#101216",
        ).pack(side="left")

        tk.Label(
            header,
            text="click notes to jump",
            font=(
                "segoe ui",
                9,
            ),
            fg="#747d87",
            bg="#101216",
        ).pack(
            side="left",
            padx=12,
            pady=(7, 0),
        )

        self.build_target_bar()

        self.editor = tk.Text(
            self.root,
            wrap=tk.NONE,
            font=(
                "cascadia mono",
                12,
            ),
            bg="#0d0f13",
            fg="#eeeeee",
            insertbackground="#ffffff",
            selectbackground="#345b8b",
            relief="flat",
            bd=1,
            padx=15,
            pady=15,
        )

        self.editor.pack(
            fill="both",
            expand=True,
            padx=22,
            pady=8,
        )

        self.sheet_area = tk.Frame(
            self.root,
            bg="#0d0f13",
            highlightthickness=1,
            highlightbackground="#292e35",
        )

        self.canvas = tk.Canvas(
            self.sheet_area,
            bg="#0d0f13",
            highlightthickness=0,
            bd=0,
        )

        self.scrollbar = tk.Scrollbar(
            self.sheet_area,
            orient="vertical",
            command=self.canvas.yview,
        )

        self.canvas.configure(
            yscrollcommand=self.scrollbar.set
        )

        self.scrollbar.pack(
            side="right",
            fill="y",
        )

        self.canvas.pack(
            side="left",
            fill="both",
            expand=True,
        )

        self.content = tk.Frame(
            self.canvas,
            bg="#0d0f13",
        )

        self.canvas_window = self.canvas.create_window(
            (0, 0),
            window=self.content,
            anchor="nw",
        )

        self.content.bind(
            "<Configure>",
            self.update_scroll_region,
        )

        self.canvas.bind(
            "<Configure>",
            self.resize_content,
        )

        self.canvas.bind_all(
            "<MouseWheel>",
            self.on_mousewheel,
        )

        controls = tk.Frame(
            self.root,
            bg="#101216",
        )

        controls.pack(
            fill="x",
            padx=22,
            pady=(8, 6),
        )

        self.start_button = self.make_button(
            controls,
            "start  [f6]",
            self.start,
        )

        self.start_button.pack(
            side="left",
            padx=(0, 6),
        )

        self.stop_button = self.make_button(
            controls,
            "stop  [f7]",
            self.stop,
        )

        self.stop_button.pack(
            side="left",
            padx=6,
        )

        self.pause_button = self.make_button(
            controls,
            "pause  [f9]",
            self.toggle_pause,
        )

        self.pause_button.pack(
            side="left",
            padx=6,
        )

        self.submit_button = self.make_button(
            controls,
            "submit",
            self.submit,
        )

        self.submit_button.pack(
            side="left",
            padx=6,
        )

        self.edit_button = self.make_button(
            controls,
            "edit",
            self.edit,
        )

        self.edit_button.pack(
            side="left",
            padx=6,
        )

        self.exit_button = self.make_button(
            controls,
            "exit  [f8]",
            self.exit_program,
        )

        self.exit_button.pack(
            side="left",
            padx=6,
        )

        self.log_button = self.make_button(
            controls,
            "view log",
            self.show_log,
        )

        self.log_button.pack(
            side="left",
            padx=6,
        )

        tempo_frame = tk.Frame(
            controls,
            bg="#101216",
        )

        tempo_frame.pack(
            side="right"
        )

        tk.Label(
            tempo_frame,
            text="tempo",
            font=(
                "segoe ui",
                9,
            ),
            fg="#808995",
            bg="#101216",
        ).pack(side="left")

        tk.Scale(
            tempo_frame,
            from_=MIN_TEMPO,
            to=MAX_TEMPO,
            variable=self.tempo,
            orient="horizontal",
            showvalue=True,
            length=210,
            bg="#101216",
            fg="#e7eaed",
            troughcolor="#282d34",
            highlightthickness=0,
            bd=0,
            activebackground="#55b77f",
        ).pack(
            side="left",
            padx=(8, 0),
        )

        loop_controls = tk.Frame(
            self.root,
            bg="#101216",
        )

        loop_controls.pack(
            fill="x",
            padx=22,
            pady=(0, 6),
        )

        tk.Checkbutton(
            loop_controls,
            text="repeat playback",
            variable=self.loop_enabled,
            font=("segoe ui", 9, "bold"),
            fg="#e8ebef",
            bg="#101216",
            activeforeground="#e8ebef",
            activebackground="#101216",
            selectcolor="#20252b",
            highlightthickness=0,
        ).pack(side="left")

        ttk.Combobox(
            loop_controls,
            textvariable=self.loop_scope,
            values=("whole sheet", "selected range"),
            state="readonly",
            width=14,
        ).pack(side="left", padx=(10, 6))

        tk.Label(
            loop_controls,
            text="from",
            font=("segoe ui", 9),
            fg="#808995",
            bg="#101216",
        ).pack(side="left")

        tk.Spinbox(
            loop_controls,
            from_=1,
            to=99999,
            textvariable=self.loop_start,
            width=5,
            justify="center",
        ).pack(side="left", padx=(4, 8))

        tk.Label(
            loop_controls,
            text="to",
            font=("segoe ui", 9),
            fg="#808995",
            bg="#101216",
        ).pack(side="left")

        tk.Spinbox(
            loop_controls,
            from_=1,
            to=99999,
            textvariable=self.loop_end,
            width=5,
            justify="center",
        ).pack(side="left", padx=(4, 12))

        tk.Label(
            loop_controls,
            text="plays (0 = infinite)",
            font=("segoe ui", 9),
            fg="#808995",
            bg="#101216",
        ).pack(side="left")

        tk.Spinbox(
            loop_controls,
            from_=0,
            to=99999,
            textvariable=self.loop_repeats,
            width=5,
            justify="center",
        ).pack(side="left", padx=(6, 0))

        status = tk.Frame(
            self.root,
            bg="#181b20",
        )

        status.pack(
            fill="x",
            padx=22,
            pady=(2, 15),
        )

        tk.Label(
            status,
            textvariable=self.status,
            font=(
                "segoe ui",
                9,
                "bold",
            ),
            fg="#59ce87",
            bg="#181b20",
        ).pack(
            side="left",
            padx=12,
            pady=8,
        )

        tk.Label(
            status,
            textvariable=self.position,
            font=(
                "segoe ui",
                9,
            ),
            fg="#77808a",
            bg="#181b20",
        ).pack(
            side="right",
            padx=12,
        )

        tk.Label(
            self.root,
            text=(
                "[abc] = chord   "
                "- = rest   "
                "{abc} = fast run   "
                "click a note to jump"
            ),
            font=(
                "segoe ui",
                8,
            ),
            fg="#69727d",
            bg="#101216",
        ).pack(
            fill="x",
            padx=24,
            pady=(0, 12),
        )

        self.sheet_area.pack_forget()
        self.edit_button.configure(
            state="disabled"
        )
        self.pause_button.configure(state="disabled")

    def build_target_bar(self):
        bar = tk.Frame(
            self.root,
            bg="#15181d",
            highlightthickness=1,
            highlightbackground="#252b33",
        )

        bar.pack(
            fill="x",
            padx=22,
            pady=(0, 10),
        )

        inner = tk.Frame(bar, bg="#15181d")

        inner.pack(
            fill="x",
            padx=14,
            pady=10,
        )

        tk.Label(
            inner,
            text="send input to",
            font=("segoe ui", 9, "bold"),
            fg="#c7ccd2",
            bg="#15181d",
        ).pack(side="left")

        style = ttk.Style()
        style.theme_use("default")

        style.configure(
            "Dark.TCombobox",
            fieldbackground="#20252b",
            background="#20252b",
            foreground="#e8ebef",
            arrowcolor="#e8ebef",
        )

        self.mode_box = ttk.Combobox(
            inner,
            state="readonly",
            width=20,
            style="Dark.TCombobox",
            values=[
                "whatever's focused",
                "specific window/exe",
            ],
        )

        self.mode_box.current(0)

        self.mode_box.pack(
            side="left",
            padx=(10, 6),
        )

        self.mode_box.bind(
            "<<ComboboxSelected>>",
            self.on_mode_change,
        )

        self.window_box = ttk.Combobox(
            inner,
            state="readonly",
            width=46,
            style="Dark.TCombobox",
            values=[],
        )

        self.window_box.bind(
            "<<ComboboxSelected>>",
            self.on_window_selected,
        )

        self.refresh_button = self.make_button(
            inner,
            "refresh",
            self.refresh_windows,
        )

        # window_box and refresh_button are shown/hidden by on_mode_change

        self.target_label = tk.Label(
            inner,
            textvariable=self.selected_window_label,
            font=("segoe ui", 9),
            fg="#77808a",
            bg="#15181d",
        )

    def check_for_update_background(self):
        info = updater.check_for_update(CURRENT_VERSION)

        if info is None:
            log.info("no update available")
            return

        skipped_path = self.skipped_version_path()

        if not info.mandatory and os.path.exists(skipped_path):
            with open(skipped_path, "r") as handle:
                skipped_version = handle.read().strip()

            if skipped_version == info.version:
                log.info("update %s already skipped by user", info.version)
                return

        self.root.after(0, lambda: self.show_update_dialog(info))

    @staticmethod
    def skipped_version_path():
        return os.path.join(tempfile.gettempdir(), "piano_player_skip.txt")

    def show_update_dialog(self, info):
        window = tk.Toplevel(self.root)
        window.title("update available")
        window.configure(bg="#15181d")
        window.resizable(False, False)
        window.grab_set()  # modal - can't ignore this by clicking elsewhere

        content = tk.Frame(window, bg="#15181d")
        content.pack(padx=32, pady=26)

        tk.Label(
            content,
            text=f"version {info.version} is available",
            font=("segoe ui", 13, "bold"),
            fg="#f2f4f7",
            bg="#15181d",
        ).pack(anchor="w")

        tk.Label(
            content,
            text=f"you're on {CURRENT_VERSION}",
            font=("segoe ui", 9),
            fg="#77808a",
            bg="#15181d",
        ).pack(anchor="w", pady=(2, 14))

        if info.changelog:
            changelog_box = tk.Text(
                content,
                width=48,
                height=6,
                font=("cascadia mono", 9),
                bg="#0d0f13",
                fg="#c7ccd2",
                relief="flat",
                wrap=tk.WORD,
                padx=10,
                pady=8,
            )

            changelog_box.insert("1.0", info.changelog)
            changelog_box.configure(state="disabled")
            changelog_box.pack(pady=(0, 16))

        if info.mandatory:
            tk.Label(
                content,
                text="this update is required to continue",
                font=("segoe ui", 9, "bold"),
                fg="#e2635a",
                bg="#15181d",
            ).pack(anchor="w", pady=(0, 14))

        progress_label = tk.StringVar(value="")

        tk.Label(
            content,
            textvariable=progress_label,
            font=("segoe ui", 9),
            fg="#77808a",
            bg="#15181d",
        ).pack(anchor="w")

        button_row = tk.Frame(content, bg="#15181d")
        button_row.pack(anchor="w", pady=(10, 0))

        update_button = tk.Button(
            button_row,
            text="update now",
            font=("segoe ui", 10, "bold"),
            fg="#ffffff",
            bg="#2f6fed",
            activebackground="#3d7dff",
            activeforeground="#ffffff",
            relief="flat",
            bd=0,
            cursor="hand2",
            padx=16,
            pady=9,
            command=lambda: self.begin_update(
                info,
                window,
                update_button,
                progress_label,
            ),
        )

        update_button.pack(side="left", padx=(0, 8))

        if not info.mandatory:
            tk.Button(
                button_row,
                text="skip this version",
                font=("segoe ui", 9),
                fg="#9aa2ac",
                bg="#20252b",
                activebackground="#2a313b",
                activeforeground="#ffffff",
                relief="flat",
                bd=0,
                cursor="hand2",
                padx=14,
                pady=9,
                command=lambda: self.skip_update(info, window),
            ).pack(side="left", padx=(0, 8))

            tk.Button(
                button_row,
                text="remind me later",
                font=("segoe ui", 9),
                fg="#9aa2ac",
                bg="#20252b",
                activebackground="#2a313b",
                activeforeground="#ffffff",
                relief="flat",
                bd=0,
                cursor="hand2",
                padx=14,
                pady=9,
                command=window.destroy,
            ).pack(side="left")

        if info.mandatory:
            # no way to close a mandatory update dialog except updating
            window.protocol("WM_DELETE_WINDOW", lambda: None)

    def skip_update(self, info, window):
        log.info("user skipped update %s", info.version)

        try:
            with open(self.skipped_version_path(), "w") as handle:
                handle.write(info.version)
        except Exception:
            log.exception("failed to persist skipped version")

        window.destroy()

    def begin_update(self, info, window, update_button, progress_label):
        update_button.configure(state="disabled")
        progress_label.set("downloading...")

        threading.Thread(
            target=self.run_update_download,
            args=(info, window, progress_label),
            daemon=True,
        ).start()

    def run_update_download(self, info, window, progress_label):
        def on_progress(bytes_read, total):
            if total > 0:
                percent = int(bytes_read / total * 100)
                text = f"downloading... {percent}% ({bytes_read // 1024} KB / {total // 1024} KB)"
            else:
                text = f"downloading... {bytes_read // 1024} KB"

            self.root.after(0, lambda: progress_label.set(text))

        try:
            new_exe_path = updater.download_update(
                info.download_url,
                progress_callback=on_progress,
            )

        except Exception:
            log.exception("update download failed")
            self.root.after(
                0,
                lambda: progress_label.set(
                    "download failed - check piano_player.log"
                ),
            )
            return

        self.root.after(
            0,
            lambda: progress_label.set("installing and relaunching..."),
        )

        applied = updater.apply_update_and_relaunch(new_exe_path)

        if applied:
            log.info("update applied, closing for relaunch")
            self.closing = True
            self.root.destroy()
        else:
            # not running as a frozen exe (dev/script mode) - nothing
            # to swap, just let them know instead of pretending it worked
            self.root.after(
                0,
                lambda: progress_label.set(
                    "not running as a packaged exe - can't self-update "
                    "in script mode"
                ),
            )

    def show_admin_overlay(self):
        """
        covers the whole window with a dimmed/blurred-look layer and an
        elevation prompt. blocks interaction with everything underneath
        until the user picks a button - real window blur isn't exposed
        through tkinter, so this fakes the effect with a semi-opaque
        dark frame plus a soft-bordered card on top.
        """
        overlay = tk.Frame(
            self.root,
            bg="#0a0b0d",
        )

        overlay.place(
            x=0,
            y=0,
            relwidth=1,
            relheight=1,
        )

        # blocks clicks from passing through to widgets underneath
        overlay.bind("<Button-1>", lambda _e: "break")

        # faux-blur: a few stacked, slightly offset translucent-looking
        # bars behind the card to read as a soft blurred backdrop
        for offset in range(6):
            tk.Frame(
                overlay,
                bg="#0a0b0d",
                height=2,
            ).place(
                x=0,
                y=offset * 3,
                relwidth=1,
            )

        card = tk.Frame(
            overlay,
            bg="#15181d",
            highlightthickness=1,
            highlightbackground="#2a313b",
        )

        card.place(
            relx=0.5,
            rely=0.5,
            anchor="center",
        )

        content = tk.Frame(card, bg="#15181d")

        content.pack(padx=48, pady=40)

        tk.Label(
            content,
            text="not running as administrator",
            font=("segoe ui", 15, "bold"),
            fg="#f2f4f7",
            bg="#15181d",
        ).pack(pady=(0, 10))

        tk.Label(
            content,
            text=(
                "key presses may not reach some games (e.g. roblox)\n"
                "without elevated permissions. you can request admin\n"
                "now, or continue anyway."
            ),
            font=("segoe ui", 10),
            fg="#9aa2ac",
            bg="#15181d",
            justify="center",
        ).pack(pady=(0, 26))

        button_row = tk.Frame(content, bg="#15181d")

        button_row.pack()

        elevate_button = tk.Button(
            button_row,
            text="request admin",
            command=self.request_elevation,
            font=("segoe ui", 10, "bold"),
            fg="#ffffff",
            bg="#2f6fed",
            activebackground="#3d7dff",
            activeforeground="#ffffff",
            relief="flat",
            bd=0,
            cursor="hand2",
            padx=18,
            pady=10,
        )

        elevate_button.pack(side="left", padx=(0, 10))

        continue_button = tk.Button(
            button_row,
            text="continue without admin",
            command=lambda: self.dismiss_admin_overlay(overlay),
            font=("segoe ui", 10, "bold"),
            fg="#c7ccd2",
            bg="#20252b",
            activebackground="#2a313b",
            activeforeground="#ffffff",
            relief="flat",
            bd=0,
            cursor="hand2",
            padx=18,
            pady=10,
        )

        continue_button.pack(side="left")

        self.admin_overlay = overlay
        self.overlay_active = True

    def dismiss_admin_overlay(self, overlay):
        log.info("user chose to continue without admin")
        self.overlay_active = False
        overlay.destroy()

    def request_elevation(self):
        log.info("user requested elevation - relaunching")

        if relaunch_as_admin():
            # the elevated copy is now starting up separately -
            # close this non-elevated instance so there's only one
            self.closing = True
            self.root.destroy()
        else:
            messagebox.showwarning(
                "elevation cancelled",
                "the admin request was cancelled or failed. "
                "you can keep using the app without admin, or try again.",
                parent=self.root,
            )

    def on_mode_change(self, _event=None):
        if self.mode_box.current() == 0:
            self.target_mode.set("foreground")
            self.active_target = None
            self.window_box.pack_forget()
            self.refresh_button.pack_forget()
            self.target_label.pack_forget()
        else:
            self.target_mode.set("specific")
            self.refresh_windows()
            self.window_box.pack(side="left", padx=6)
            self.refresh_button.pack(side="left", padx=6)
            self.target_label.pack(side="left", padx=(10, 0))

    def refresh_windows(self):
        self.window_targets = list_windows()

        labels = [str(target) for target in self.window_targets]

        self.window_box.configure(values=labels)

        # keep current selection if it's still present, else clear it
        if self.active_target is not None:
            try:
                index = labels.index(str(self.active_target))
                self.window_box.current(index)
                return
            except ValueError:
                pass

        self.active_target = None
        self.window_box.set("")
        self.selected_window_label.set("(none selected)")

    def on_window_selected(self, _event=None):
        index = self.window_box.current()

        if index < 0 or index >= len(self.window_targets):
            return

        self.active_target = self.window_targets[index]
        self.selected_window_label.set(f"-> {self.active_target}")

    def player_error(self, message):
        log.warning("player_error: %s", message)
        self.root.after(0, lambda: self.status.set(message))

    def show_log(self):
        window = tk.Toplevel(self.root)
        window.title("debug log")
        window.geometry("820x520")
        window.configure(bg="#0d0f13")

        text = tk.Text(
            window,
            bg="#0d0f13",
            fg="#c7ccd2",
            font=("cascadia mono", 9),
            wrap=tk.WORD,
            padx=10,
            pady=10,
        )

        text.pack(fill="both", expand=True)

        try:
            with open(LOG_PATH, "r", encoding="utf-8") as handle:
                content = handle.read()
        except FileNotFoundError:
            content = "no log written yet - hit start first"

        text.insert("1.0", content)
        text.see("end")
        text.configure(state="disabled")

        refresh_button = self.make_button(
            window,
            "refresh",
            lambda: self.refresh_log_text(text),
        )

        refresh_button.pack(pady=(0, 10))

    def refresh_log_text(self, text_widget):
        try:
            with open(LOG_PATH, "r", encoding="utf-8") as handle:
                content = handle.read()
        except FileNotFoundError:
            content = "no log written yet - hit start first"

        text_widget.configure(state="normal")
        text_widget.delete("1.0", "end")
        text_widget.insert("1.0", content)
        text_widget.see("end")
        text_widget.configure(state="disabled")

    def make_button(
        self,
        parent,
        text,
        command,
    ):
        return tk.Button(
            parent,
            text=text,
            command=command,
            font=(
                "segoe ui",
                9,
                "bold",
            ),
            width=12,
            fg="#e8ebef",
            bg="#20252b",
            activebackground="#30373f",
            activeforeground="#ffffff",
            relief="flat",
            bd=0,
            cursor="hand2",
            padx=9,
            pady=8,
        )

    def submit(self):
        if self.player.playing:
            return

        sheet = self.editor.get(
            "1.0",
            "end-1c",
        )

        if not sheet.strip():
            self.status.set(
                "paste a sheet first"
            )
            return

        try:
            self.events = SheetParser.parse(
                sheet
            )

        except ValueError as error:
            log.warning("sheet parse failed: %s", error)
            messagebox.showerror(
                "invalid sheet",
                str(error),
                parent=self.root,
            )
            return

        log.info("sheet parsed: %s events", len(self.events))

        if not self.events:
            self.status.set(
                "nothing to play"
            )
            return

        self.editing = False
        self.selected_event = 0
        self.playback_start_index = 0
        self.loop_start.set("1")
        self.loop_end.set(str(len(self.events)))

        self.editor.pack_forget()

        self.sheet_area.pack(
            fill="both",
            expand=True,
            padx=22,
            pady=8,
            before=self.start_button.master,
        )

        self.submit_button.configure(
            state="disabled"
        )

        self.edit_button.configure(
            state="normal"
        )

        self.render_notes()

        self.status.set(
            f"ready • {len(self.events)} notes"
        )

    def edit(self):
        if self.player.playing:
            return

        self.editing = True

        self.sheet_area.pack_forget()

        self.editor.pack(
            fill="both",
            expand=True,
            padx=22,
            pady=8,
            before=self.start_button.master,
        )

        self.submit_button.configure(
            state="normal"
        )

        self.edit_button.configure(
            state="disabled"
        )

        self.status.set(
            "editing"
        )

    def render_notes(self):
        for widget in self.note_widgets:
            widget.frame.destroy()

        self.note_widgets.clear()

        # group events by their original source line
        lines = {}

        for index, event in enumerate(
            self.events
        ):
            lines.setdefault(
                event["line"],
                [],
            ).append(
                (index, event)
            )

        max_line = max(
            lines.keys(),
            default=0,
        )

        for line_number in range(
            max_line + 1
        ):
            row = tk.Frame(
                self.content,
                bg="#0d0f13",
            )

            row.pack(
                fill="x",
                anchor="w",
                padx=14,
                pady=3,
            )

            # small line number gutter
            tk.Label(
                row,
                text=f"{line_number + 1:03}",
                width=4,
                anchor="e",
                font=(
                    "cascadia mono",
                    8,
                ),
                fg="#3e444c",
                bg="#0d0f13",
            ).pack(
                side="left",
                padx=(0, 10),
            )

            for index, event in lines.get(
                line_number,
                [],
            ):
                widget = NoteWidget(
                    row,
                    self,
                    index,
                    event,
                )

                widget.frame.pack(
                    side="left",
                    padx=2,
                )

                self.note_widgets.append(
                    widget
                )

        self.update_note_styles()

    def update_note_styles(self):
        for widget in self.note_widgets:
            index = widget.event_index

            if index < self.selected_event:
                widget.set_played()

            elif index == self.selected_event:
                widget.set_current()

            else:
                widget.set_next()

        self.position.set(
            f"{self.selected_event + 1} / "
            f"{len(self.events)}"
        )

    def select_note(self, index):
        if self.editing:
            return

        self.player.stop()

        self.selected_event = index

        self.update_note_styles()

        self.scroll_to_note(
            index
        )

        self.status.set(
            f"selected note {index + 1}"
        )

    def scroll_to_note(self, index):
        if not self.note_widgets:
            return

        widget = self.note_widgets[index]

        self.canvas.update_idletasks()

        y = (
            widget.frame.winfo_rooty()
            - self.content.winfo_rooty()
        )

        content_height = max(
            self.content.winfo_height(),
            1,
        )

        self.canvas.yview_moveto(
            max(
                0,
                min(
                    y / content_height,
                    1,
                ),
            )
        )

    def note_changed(self, index):
        self.root.after(
            0,
            lambda: self.on_note_changed(
                index
            ),
        )

    def on_note_changed(self, index):
        self.selected_event = index

        self.update_note_styles()

        self.scroll_to_note(
            index
        )

    def playback_finished(self, was_stopped):
        self.root.after(
            0,
            lambda: self.on_playback_finished(was_stopped),
        )

    def on_playback_finished(self, was_stopped):
        self.pause_button.configure(state="disabled", text="pause  [f9]")

        if was_stopped:
            return

        if not was_stopped:
            self.status.set("finished")

    def start(self):
        log.debug(
            "App.start() called: editing=%s events=%s player.playing=%s mode=%s overlay_active=%s",
            self.editing,
            len(self.events),
            self.player.playing,
            self.target_mode.get(),
            self.overlay_active,
        )

        if self.overlay_active:
            log.debug("start() aborted - admin overlay still up")
            return

        if self.editing:
            log.debug("start() aborted - still in editing mode")
            self.status.set(
                "submit the sheet first"
            )
            return

        if not self.events:
            log.debug("start() aborted - no events parsed (did submit() run?)")
            self.status.set(
                "no sheet submitted"
            )
            return

        if self.player.playing:
            log.debug("start() aborted - already playing")
            return

        playback_options = self.get_playback_options()

        if playback_options is None:
            return

        start_index, end_index, repeat_count = playback_options

        if self.target_mode.get() == "specific":
            if self.active_target is None:
                log.debug("start() aborted - specific mode but no window picked")
                self.status.set(
                    "pick a window first"
                )
                return

            if not self.active_target.is_alive():
                log.debug("start() aborted - target window no longer alive")
                self.status.set(
                    "target window closed - pick again"
                )
                self.refresh_windows()
                return

            # confirm we can actually focus it before spawning the
            # playback thread, so failures show up immediately
            success, reason = self.active_target.focus()

            if not success:
                log.warning("start() aborted - initial focus failed: %s", reason)
                messagebox.showerror(
                    "can't focus target",
                    f"couldn't switch to the target window: {reason}\n\n"
                    "try clicking the target window manually once, "
                    "then hit start again.",
                    parent=self.root,
                )
                return

            target = self.active_target
        else:
            target = None

        self.player.start(
            self.events,
            start_index,
            end_index,
            repeat_count,
            self.tempo.get(),
            target,
        )

        self.playback_start_index = start_index
        self.pause_button.configure(state="normal", text="pause  [f9]")

        self.status.set(
            f"playing notes {start_index + 1}–{end_index + 1}"
        )

    def stop(self):
        self.player.stop()

        if not self.editing:
            self.selected_event = self.playback_start_index
            self.update_note_styles()
            self.status.set(
                f"stopped • reset to note "
                f"{self.selected_event + 1}"
            )

        self.pause_button.configure(state="disabled", text="pause  [f9]")

    def toggle_pause(self):
        if self.player.pause():
            self.pause_button.configure(text="resume  [f9]")
            self.status.set("paused")
            return

        if self.player.resume():
            self.pause_button.configure(text="pause  [f9]")
            self.status.set("playing")

    def get_playback_options(self):
        if not self.loop_enabled.get():
            return self.selected_event, len(self.events) - 1, 1

        try:
            repeat_count = int(self.loop_repeats.get())
        except ValueError:
            self.status.set("enter a whole number of plays")
            return None

        if repeat_count < 0:
            self.status.set("plays cannot be negative")
            return None

        if self.loop_scope.get() == "whole sheet":
            return 0, len(self.events) - 1, repeat_count

        try:
            range_start = int(self.loop_start.get()) - 1
            range_end = int(self.loop_end.get()) - 1
        except ValueError:
            self.status.set("enter whole-number range limits")
            return None

        if not 0 <= range_start <= range_end < len(self.events):
            self.status.set(
                f"range must be between 1 and {len(self.events)}"
            )
            return None

        return range_start, range_end, repeat_count

    def update_scroll_region(
        self,
        _event=None,
    ):
        self.canvas.configure(
            scrollregion=self.canvas.bbox(
                "all"
            )
        )

    def resize_content(self, event):
        self.canvas.itemconfigure(
            self.canvas_window,
            width=event.width,
        )

    def on_mousewheel(self, event):
        if not self.editing:
            self.canvas.yview_scroll(
                int(-event.delta / 120),
                "units",
            )

    def hotkey_loop(self):
        try:
            keyboard.add_hotkey(
                START_HOTKEY,
                self.start,
            )

            keyboard.add_hotkey(
                STOP_HOTKEY,
                self.stop,
            )

            keyboard.add_hotkey(
                PAUSE_HOTKEY,
                self.toggle_pause,
            )

            keyboard.add_hotkey(
                EXIT_HOTKEY,
                self.exit_program,
            )

            log.info(
                "hotkeys registered: %s/%s/%s/%s",
                START_HOTKEY,
                STOP_HOTKEY,
                PAUSE_HOTKEY,
                EXIT_HOTKEY,
            )

            while not self.closing:
                time.sleep(0.1)

        except Exception as error:
            # on windows this usually means the process needs to run
            # as administrator for global key hooks to register
            log.exception("hotkey registration failed - try running as admin")
            self.status.set(
                f"hotkey error: {error}"
            )

    def exit_program(self):
        if self.closing:
            return

        self.closing = True

        self.player.stop()

        try:
            keyboard.unhook_all_hotkeys()
        except Exception:
            pass

        self.root.destroy()


def main():
    log.info("app starting")
    root = tk.Tk()

    def log_tk_errors(exc_type, exc_value, exc_tb):
        # tkinter swallows exceptions raised inside button callbacks and
        # only prints them to stderr - if there's no console attached
        # (e.g. launched by double-click), that output goes nowhere and
        # buttons silently appear to do nothing. this routes it to the log.
        log.error(
            "tkinter callback error",
            exc_info=(exc_type, exc_value, exc_tb),
        )

    root.report_callback_exception = log_tk_errors

    App(root)
    root.mainloop()
    log.info("app closed")


if __name__ == "__main__":
    main()

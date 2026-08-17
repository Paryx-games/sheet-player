"""
self-update logic against a version.json manifest hosted on github.

kept separate from main.py on purpose - update mechanics have nothing
to do with the piano player itself, and this way the updater can be
reused or swapped independently.
"""

import json
import logging
import os
import subprocess
import sys
import tempfile
import urllib.request

log = logging.getLogger("piano.updater")

version_manifest_url = (
    "https://raw.githubusercontent.com/"
    "paryx-games/sheet-player/main/version.json"
)

request_timeout_seconds = 6


class update_info:
    def __init__(self, version, download_url, changelog, mandatory):
        self.version = version
        self.download_url = download_url
        self.changelog = changelog
        self.mandatory = mandatory


def parse_version(text):
    """turns '1.4.2' into (1, 4, 2) for numerical comparison."""
    parts = []

    for piece in text.strip().split("."):
        try:
            parts.append(int(piece))
        except ValueError:
            parts.append(0)

    return tuple(parts)


def check_for_update(current_version):
    """
    fetches version.json and returns update_info when a newer version
    is available. failures are logged and treated as no update.
    """
    try:
        request = urllib.request.Request(
            version_manifest_url,
            headers={"user-agent": "piano-player-updater"},
        )

        with urllib.request.urlopen(
            request,
            timeout=request_timeout_seconds,
        ) as response:
            raw = response.read().decode("utf-8")

        manifest = json.loads(raw)

        latest_version = manifest["version"]
        download_url = manifest["download_url"]
        changelog = manifest.get("changelog", "")
        mandatory = bool(manifest.get("mandatory", False))

        log.info(
            "version check: current=%s latest=%s mandatory=%s",
            current_version,
            latest_version,
            mandatory,
        )

        if parse_version(latest_version) > parse_version(current_version):
            return update_info(
                latest_version,
                download_url,
                changelog,
                mandatory,
            )

        return None

    except Exception:
        log.exception("update check failed - continuing without update")
        return None


def download_update(download_url, progress_callback=None):
    """
    downloads the new exe to a temporary file and returns its path.
    """
    request = urllib.request.Request(
        download_url,
        headers={"user-agent": "piano-player-updater"},
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        total = int(response.headers.get("content-length", -1))

        fd, temp_path = tempfile.mkstemp(
            suffix=".exe",
            prefix="piano_player_update_",
        )

        bytes_read = 0
        chunk_size = 262144

        with os.fdopen(fd, "wb") as out_file:
            while True:
                chunk = response.read(chunk_size)

                if not chunk:
                    break

                out_file.write(chunk)
                bytes_read += len(chunk)

                if progress_callback is not None:
                    progress_callback(bytes_read, total)

    if not os.path.isfile(temp_path):
        raise RuntimeError("downloaded update does not exist")

    if os.path.getsize(temp_path) == 0:
        raise RuntimeError("downloaded update is empty")

    log.info(
        "update downloaded to %s (%s bytes)",
        temp_path,
        bytes_read,
    )

    return temp_path


def apply_update_and_relaunch(new_exe_path):
    """
    launches a temporary batch updater which waits for the current
    process to exit, replaces the executable, then starts the new one.
    """
    if not getattr(sys, "frozen", False):
        log.warning(
            "apply_update_and_relaunch called while running as a "
            "script, not a frozen exe"
        )
        return False

    current_exe = os.path.abspath(sys.executable)
    new_exe_path = os.path.abspath(new_exe_path)

    if not os.path.isfile(new_exe_path):
        log.error("update file does not exist: %s", new_exe_path)
        return False

    log.info("current exe: %s", current_exe)
    log.info("new exe: %s", new_exe_path)

    batch_path = os.path.join(
        tempfile.gettempdir(),
        "piano_player_update.bat",
    )

    pid = os.getpid()

    batch_contents = f"""@echo off
setlocal

:wait_loop
tasklist /fi "PID eq {pid}" 2>NUL | find "{pid}" >NUL
if not errorlevel 1 (
    timeout /t 1 /nobreak >NUL
    goto wait_loop
)

if not exist "{new_exe_path}" (
    echo update file does not exist
    exit /b 1
)

move /y "{new_exe_path}" "{current_exe}"

if errorlevel 1 (
    echo failed to replace executable
    exit /b 1
)

start "" "{current_exe}"

if errorlevel 1 (
    echo failed to launch updated executable
    exit /b 1
)

del "%~f0"
"""

    try:
        with open(batch_path, "w", encoding="utf-8") as handle:
            handle.write(batch_contents)

        log.info("launching update script: %s", batch_path)

        subprocess.Popen(
            ["cmd.exe", "/c", batch_path],
            creationflags=subprocess.CREATE_NO_WINDOW,
            close_fds=True,
        )

        return True

    except Exception:
        log.exception("failed to launch update script")
        return False
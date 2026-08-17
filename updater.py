"""
self-update logic against a version.json manifest hosted on GitHub.

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

# raw.githubusercontent.com serves repo files directly, no API rate
# limiting the way api.github.com does for unauthenticated requests
VERSION_MANIFEST_URL = (
    "https://raw.githubusercontent.com/"
    "paryx-games/piano-player/main/version.json"
)

REQUEST_TIMEOUT_SECONDS = 6


class UpdateInfo:
    def __init__(self, version, download_url, changelog, mandatory):
        self.version = version
        self.download_url = download_url
        self.changelog = changelog
        self.mandatory = mandatory


def parse_version(text):
    """turns '1.4.2' into (1, 4, 2) so versions compare numerically,
    not as strings (which would wrongly say '1.10.0' < '1.9.0')"""
    parts = []

    for piece in text.strip().split("."):
        try:
            parts.append(int(piece))
        except ValueError:
            parts.append(0)

    return tuple(parts)


def check_for_update(current_version):
    """
    fetches version.json and returns an UpdateInfo if the manifest's
    version is newer than current_version, else None. never raises -
    network/parse failures just mean "no update found," logged but
    not surfaced, so a flaky connection never blocks the app from
    starting.
    """
    try:
        request = urllib.request.Request(
            VERSION_MANIFEST_URL,
            headers={"User-Agent": "piano-player-updater"},
        )

        with urllib.request.urlopen(
            request,
            timeout=REQUEST_TIMEOUT_SECONDS,
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
            return UpdateInfo(
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
    downloads the new exe to a temp file and returns its path.
    progress_callback(bytes_read, total_bytes) is called periodically
    if given - total_bytes may be -1 if the server didn't send a
    content-length header.
    """
    request = urllib.request.Request(
        download_url,
        headers={"User-Agent": "piano-player-updater"},
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        total = int(response.headers.get("Content-Length", -1))

        fd, temp_path = tempfile.mkstemp(
            suffix=".exe",
            prefix="piano_player_update_",
        )

        bytes_read = 0
        chunk_size = 262144  # 256kb chunks

        with os.fdopen(fd, "wb") as out_file:
            while True:
                chunk = response.read(chunk_size)

                if not chunk:
                    break

                out_file.write(chunk)
                bytes_read += len(chunk)

                if progress_callback is not None:
                    progress_callback(bytes_read, total)

    log.info("update downloaded to %s (%s bytes)", temp_path, bytes_read)

    return temp_path


def apply_update_and_relaunch(new_exe_path):
    """
    swaps the currently-running exe for the newly downloaded one and
    relaunches. a running exe can't overwrite itself on windows, so
    this writes a tiny batch script that waits for this process to
    exit, does the file swap, starts the new exe, then deletes itself.

    only meaningful when actually running as a frozen exe (sys.frozen
    is set by pyinstaller) - if you're running main.py directly as a
    script, there's no exe to replace, so this just logs and returns.
    """
    if not getattr(sys, "frozen", False):
        log.warning(
            "apply_update_and_relaunch called while running as a "
            "script, not a frozen exe - nothing to replace"
        )
        return False

    current_exe = sys.executable
    batch_path = os.path.join(
        tempfile.gettempdir(),
        "piano_player_update.bat",
    )

    # ping loop as a crude "wait for the old process to fully exit"
    # since batch has no native sleep/wait-for-pid - del retries a
    # few times in case the OS hasn't released the file handle yet
    batch_contents = f"""@echo off
:wait_loop
tasklist /fi "PID eq {os.getpid()}" 2>NUL | find "{os.getpid()}" >NUL
if not errorlevel 1 (
    ping 127.0.0.1 -n 2 >NUL
    goto wait_loop
)

move /y "{new_exe_path}" "{current_exe}" >NUL
start "" "{current_exe}"
del "%~f0"
"""

    with open(batch_path, "w") as handle:
        handle.write(batch_contents)

    log.info("launching update swap script: %s", batch_path)

    subprocess.Popen(
        ["cmd.exe", "/c", batch_path],
        creationflags=subprocess.CREATE_NO_WINDOW,
    )

    return True

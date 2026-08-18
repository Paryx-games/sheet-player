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

import paths

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


def find_installer_uninstall_exe(current_exe_path):
    """Return the Inno Setup uninstaller if it exists next to the app."""
    install_dir = os.path.dirname(os.path.abspath(current_exe_path))
    candidates = [
        os.path.join(install_dir, "unins000.exe"),
        os.path.join(install_dir, "unins001.exe"),
        os.path.join(install_dir, "unins.exe"),
    ]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return None


def create_installer_update_batch(current_exe_path, new_installer_path, uninstaller_path=None):
    """Create a batch file that silently uninstalls the old version and then
    runs the new Inno Setup installer. This is required because the app is
    installed to Program Files and the running executable cannot be replaced in
    place while in use."""
    current_exe_path = os.path.abspath(current_exe_path)
    new_installer_path = os.path.abspath(new_installer_path)
    uninstaller_path = os.path.abspath(uninstaller_path) if uninstaller_path else None

    pid = os.getpid()

    uninstall_block = ""
    if uninstaller_path:
        uninstall_block = f'''if exist "{uninstaller_path}" (
    echo uninstalling previous version
    start /wait "" "{uninstaller_path}" /VERYSILENT /NORESTART /SUPPRESSMSGBOXES
    if errorlevel 1 (
        echo previous version uninstall returned code %errorlevel%
    )
)
'''

    batch_contents = f'''@echo off
setlocal

:wait_loop
tasklist /fi "PID eq {pid}" 2>NUL | find "{pid}" >NUL
if not errorlevel 1 (
    timeout /t 1 /nobreak >NUL
    goto wait_loop
)

if not exist "{new_installer_path}" (
    echo update installer does not exist
    exit /b 1
)

{uninstall_block}if not exist "{new_installer_path}" (
    echo update installer missing before install
    exit /b 1
)

start /wait "" "{new_installer_path}" /VERYSILENT /NORESTART /SUPPRESSMSGBOXES
if errorlevel 1 (
    echo installer exited with code %errorlevel%
    exit /b 1
)

start "" "{current_exe_path}"
if errorlevel 1 (
    echo failed to relaunch updated app
    exit /b 1
)

del "%~f0"
'''

    return batch_contents


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
    downloads the new installer to a temporary file and returns its path.
    """
    request = urllib.request.Request(
        download_url,
        headers={"user-agent": "piano-player-updater"},
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        total = int(response.headers.get("content-length", -1))

        fd, temp_path = tempfile.mkstemp(
            suffix=".exe",
            prefix=paths.TEMP_UPDATE_EXE_PREFIX,
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
    """Wait for the running app to exit, silently uninstall the previous
    installer version if needed, then run the downloaded installer and relaunch
    the app. The new installer is the real upgrade mechanism when the app is
    installed via Inno Setup."""
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

    uninstaller_path = find_installer_uninstall_exe(current_exe)
    batch_contents = create_installer_update_batch(
        current_exe,
        new_exe_path,
        uninstaller_path,
    )

    batch_path = str(paths.TEMP_UPDATE_BATCH_PATH)

    try:
        with open(batch_path, "w", encoding="utf-8") as handle:
            handle.write(batch_contents)

        log.info("current exe: %s", current_exe)
        log.info("new installer: %s", new_exe_path)
        log.info("uninstaller: %s", uninstaller_path)
        log.info("launching installer update script: %s", batch_path)

        subprocess.Popen(
            ["cmd.exe", "/c", batch_path],
            creationflags=subprocess.CREATE_NO_WINDOW,
            close_fds=True,
        )

        return True

    except Exception:
        log.exception("failed to launch installer update script")
        return False
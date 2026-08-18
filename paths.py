import os
import tempfile
from pathlib import Path


def _default_local_app_data_dir():
    return Path.home() / "AppData" / "Local"


APP_DATA_DIR = Path(
    os.environ.get("LOCALAPPDATA") or str(_default_local_app_data_dir())
) / "piano player"

# Persistent user-writable app data lives in %LOCALAPPDATA%\piano player\.
# This is created on import so the app can always log or save settings.
os.makedirs(APP_DATA_DIR, exist_ok=True)

LOG_PATH = APP_DATA_DIR / "piano_player.log"
SKIPPED_VERSION_PATH = APP_DATA_DIR / "skipped_version.txt"
SHEET_LIBRARY_FILE_NAME = "sheets.json"
SHEET_LIBRARY_PATH = APP_DATA_DIR / SHEET_LIBRARY_FILE_NAME
CONFIG_PATH = SHEET_LIBRARY_PATH

TEMP_DIR = Path(tempfile.gettempdir())
TEMP_UPDATE_EXE_PREFIX = "piano_player_update_"
TEMP_UPDATE_BATCH_PATH = TEMP_DIR / "piano_player_update.bat"


def ensure_app_data_dir():
    os.makedirs(APP_DATA_DIR, exist_ok=True)
    return APP_DATA_DIR

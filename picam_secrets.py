"""
Centralized Pi credential loader for PiCam Windows-side scripts.

Loads from (in order):
  1. C:\\Users\\Tarik\\.picam_secrets.json   (default)
  2. env var PICAM_SECRETS_PATH if set       (override for tests)

Prefers SSH key auth when the key file exists; falls back to password.
Both face_watcher.py and clip-sync.py import this so neither has the
password hardcoded in source.
"""

import json
import os
from pathlib import Path

_DEFAULT_PATH = Path.home() / ".picam_secrets.json"


def _load_raw():
    path = Path(os.environ.get("PICAM_SECRETS_PATH", _DEFAULT_PATH))
    if not path.exists():
        raise FileNotFoundError(
            f"PiCam secrets file not found: {path}\n"
            "Create it from the template in the project README, or set "
            "PICAM_SECRETS_PATH to point at it."
        )
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_pi_creds():
    """Return a dict suitable for passing to paramiko.SSHClient.connect(**creds).

    Always includes hostname, username, timeout.
    Includes key_filename when the configured key file exists on disk
    (paramiko will try the key first, fall back to password if also set).
    Includes password when configured (used as fallback or when key absent).
    """
    raw = _load_raw()
    creds = {
        "hostname": raw["pi_host"],
        "username": raw["pi_user"],
        "timeout": 5,
    }
    key_path = raw.get("pi_ssh_key")
    if key_path and Path(key_path).exists():
        creds["key_filename"] = key_path
    pw = raw.get("pi_password")
    if pw:
        creds["password"] = pw
    return creds


def load_sudo_password():
    """Return the Pi sudo password (used by Shutdown PiCam.bat).
    Returns None if no sudoers NOPASSWD line has been set up yet."""
    return _load_raw().get("pi_sudo_password")


def load_pi_host():
    return _load_raw()["pi_host"]

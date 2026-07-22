"""Runtime settings persisted to /data/settings.json.

Env vars bootstrap the app; anything the admin changes in the dashboard
lands here and takes precedence. The file lives on the data volume so
settings survive image upgrades.
"""

import json
import os
import secrets
import threading

SETTINGS_PATH = os.environ.get("SETTINGS_PATH", "/data/settings.json")

_lock = threading.Lock()

DEFAULTS = {
    "secret_key": None,           # session-signing key; rotated on password change
    "admin_password_hash": None,  # None -> fall back to ADMIN_PASSWORD env
    "turnstile_enabled": False,
    "turnstile_site_key": "",
    "turnstile_secret_key": "",
    "origin_locked": False,
    "default_origin": None,       # None -> DEFAULT_ORIGIN env
}


def load():
    try:
        with open(SETTINGS_PATH) as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    return {**DEFAULTS, **data}


def save(updates):
    with _lock:
        current = load()
        current.update(updates)
        os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
        tmp = SETTINGS_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(current, f, indent=2)
        os.replace(tmp, SETTINGS_PATH)
        return current


def ensure_secret_key():
    s = load()
    if not s["secret_key"]:
        s = save({"secret_key": secrets.token_hex(32)})
    return s["secret_key"]


def rotate_secret_key():
    """Invalidates every session — used when the password changes."""
    return save({"secret_key": secrets.token_hex(32)})["secret_key"]

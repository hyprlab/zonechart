import hmac
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from functools import lru_cache, wraps

from flask import (Flask, abort, jsonify, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

import settings
from zonechart import parse_chart

CHARTS_DIR = os.environ.get("CHARTS_DIR", "/data/charts")
# bundled seed so a fresh install boots with a working origin; the full
# dataset is fetched from the admin page
LEGACY_CHART = os.environ.get(
    "CHART_PATH", os.path.join(os.path.dirname(__file__), "seed", "439.xls"))
ENV_DEFAULT_ORIGIN = os.environ.get("DEFAULT_ORIGIN", "439")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
STATUS_PATH = os.environ.get("REFRESH_STATUS_PATH", "/data/refresh_status.json")
CANCEL_PATH = STATUS_PATH + ".cancel"
TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"

app = Flask(__name__)
app.secret_key = settings.ensure_secret_key()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")


def check_admin_password(supplied):
    """Dashboard-set password (hashed) wins; ADMIN_PASSWORD env is the
    bootstrap fallback."""
    s = settings.load()
    if s["admin_password_hash"]:
        return check_password_hash(s["admin_password_hash"], supplied)
    return bool(ADMIN_PASSWORD) and hmac.compare_digest(supplied,
                                                        ADMIN_PASSWORD)


def default_origin():
    s = settings.load()
    return (s["default_origin"] or ENV_DEFAULT_ORIGIN)[:3]


def turnstile_config():
    s = settings.load()
    active = bool(s["turnstile_enabled"] and s["turnstile_site_key"]
                  and s["turnstile_secret_key"])
    return active, s["turnstile_site_key"], s["turnstile_secret_key"]


def verify_turnstile(token):
    active, _, secret = turnstile_config()
    if not active:
        return True, None
    if not token:
        return False, "Please complete the human check."
    data = urllib.parse.urlencode({
        "secret": secret, "response": token,
        "remoteip": request.headers.get("CF-Connecting-IP",
                                        request.remote_addr or ""),
    }).encode()
    try:
        with urllib.request.urlopen(TURNSTILE_VERIFY_URL, data,
                                    timeout=10) as r:
            ok = json.load(r).get("success")
    except OSError:
        return False, "Could not reach the verification service — try again."
    return (True, None) if ok else (False, "Human check failed — try again.")

with open(os.path.join(os.path.dirname(__file__), "prefix_states.json")) as f:
    PREFIX_STATES = json.load(f)


def discover_charts():
    """Map origin prefix -> workbook path. data/charts/*.xls[x] plus the
    original single-chart path as a fallback seed."""
    charts = {}
    if os.path.isdir(CHARTS_DIR):
        for fn in sorted(os.listdir(CHARTS_DIR)):
            m = re.fullmatch(r"(\d{3})\.(xlsx?|XLSX?)", fn)
            if m:
                charts[m.group(1)] = os.path.join(CHARTS_DIR, fn)
    m = re.search(r"(\d{3})\.xlsx?$", LEGACY_CHART or "", re.I)
    if m and os.path.isfile(LEGACY_CHART):
        charts.setdefault(m.group(1), LEGACY_CHART)
    return charts


CHARTS = discover_charts()


@lru_cache(maxsize=128)
def chart_for(prefix):
    return parse_chart(CHARTS[prefix])


def normalize_origin(raw):
    raw = (raw or "").strip()
    if not re.fullmatch(r"\d{3}(\d{2})?", raw):
        return None, None
    return raw[:3], raw if len(raw) == 5 else None


@app.get("/")
def index():
    return render_template("index.html", default_origin=default_origin())


@app.get("/api/origins")
def api_origins():
    s = settings.load()
    d = default_origin()
    return jsonify({
        "default": d if d in CHARTS else
                   (sorted(CHARTS)[0] if CHARTS else None),
        "locked": bool(s["origin_locked"]),
        "available": [
            {"prefix": p, "state": PREFIX_STATES.get(p)}
            for p in sorted(CHARTS)
        ],
        "total_mapped": len(PREFIX_STATES),
    })


@app.get("/api/chart")
def api_chart():
    prefix, zip5 = normalize_origin(request.args.get("origin",
                                                     default_origin()))
    if prefix is None:
        abort(400, "origin must be a 3- or 5-digit ZIP")
    if settings.load()["origin_locked"] and prefix != default_origin():
        abort(403, "the origin is locked by the administrator")
    if prefix not in CHARTS:
        abort(404, f"no zone chart on file for origin prefix {prefix}")
    data = dict(chart_for(prefix))
    data["origin"] = {
        "prefix": prefix,
        "zip5": zip5,
        "state": PREFIX_STATES.get(prefix),
    }
    resp = jsonify(data)
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp


@app.get("/healthz")
def healthz():
    return {"status": "ok", "charts": len(CHARTS)}


# ---------- admin ----------

def require_admin(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if not session.get("admin"):
            if request.method != "GET":
                abort(401)
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return wrapped


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    ts_active, ts_site_key, _ = turnstile_config()
    if request.method == "POST":
        human, ts_error = verify_turnstile(
            request.form.get("cf-turnstile-response", ""))
        if not human:
            error = ts_error
        elif check_admin_password(request.form.get("password", "")):
            session["admin"] = True
            return redirect(request.args.get("next") or url_for("admin"))
        else:
            time.sleep(1)  # blunt brute-force throttle
            no_password = (not ADMIN_PASSWORD
                           and not settings.load()["admin_password_hash"])
            error = ("Admin password not set — add ADMIN_PASSWORD to "
                     "docker-compose.yml" if no_password
                     else "That password didn't match.")
    return render_template("login.html", error=error,
                           turnstile_site_key=ts_site_key if ts_active
                           else None)


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


def read_refresh_status():
    try:
        with open(STATUS_PATH) as f:
            status = json.load(f)
    except (OSError, ValueError):
        return {"state": "idle"}
    # a job that died without writing a terminal state shows as error
    if status.get("state") in ("starting", "running"):
        pid = status.get("pid")
        try:
            os.kill(pid, 0)
        except (TypeError, ProcessLookupError, PermissionError):
            status["state"] = "error"
            status["error"] = "refresh process died unexpectedly"
    return status


def refresh_is_running(status=None):
    return (status or read_refresh_status()).get("state") in ("starting",
                                                              "running")


@app.get("/admin")
@require_admin
def admin():
    return render_template("admin.html")


@app.get("/admin/info")
@require_admin
def admin_info():
    mtimes = []
    if os.path.isdir(CHARTS_DIR):
        mtimes = [os.path.getmtime(os.path.join(CHARTS_DIR, fn))
                  for fn in os.listdir(CHARTS_DIR)
                  if re.fullmatch(r"\d{3}\.xlsx?", fn, re.I)]
    return jsonify({
        "charts": len(CHARTS),
        "mapped_prefixes": len(PREFIX_STATES),
        "newest": max(mtimes) if mtimes else None,
        "oldest": min(mtimes) if mtimes else None,
    })


@app.get("/admin/refresh/status")
@require_admin
def refresh_status():
    status = read_refresh_status()
    # once a run finishes, fold the new charts into the live registry
    if status.get("state") == "done" and status.get("finished_at") and \
            status["finished_at"] != app.config.get("last_reload"):
        global CHARTS
        CHARTS = discover_charts()
        chart_for.cache_clear()
        app.config["last_reload"] = status["finished_at"]
    return jsonify(status)


@app.post("/admin/refresh")
@require_admin
def refresh_start():
    if refresh_is_running():
        abort(409, "a refresh is already running")
    body = request.get_json(silent=True) or {}
    force = bool(body.get("force"))
    cmd = [sys.executable, os.path.join(os.path.dirname(__file__),
                                        "refresher.py")]
    if force:
        cmd.append("--force")
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    return jsonify({"started": True}), 202


@app.post("/admin/refresh/cancel")
@require_admin
def refresh_cancel():
    if not refresh_is_running():
        abort(409, "no refresh is running")
    open(CANCEL_PATH, "w").close()
    return jsonify({"cancelling": True})


@app.get("/admin/settings")
@require_admin
def admin_settings():
    s = settings.load()
    return jsonify({
        "turnstile_enabled": bool(s["turnstile_enabled"]),
        "turnstile_site_key": s["turnstile_site_key"],
        "turnstile_secret_set": bool(s["turnstile_secret_key"]),
        "origin_locked": bool(s["origin_locked"]),
        "default_origin": s["default_origin"] or ENV_DEFAULT_ORIGIN,
        "password_customized": bool(s["admin_password_hash"]),
    })


@app.post("/admin/settings/frontend")
@require_admin
def admin_settings_frontend():
    body = request.get_json(silent=True) or {}
    locked = bool(body.get("origin_locked"))
    origin = (body.get("default_origin") or "").strip()
    updates = {"origin_locked": locked}
    if origin:
        prefix, _ = normalize_origin(origin)
        if prefix is None:
            abort(400, "origin must be a 3- or 5-digit ZIP")
        if prefix not in CHARTS:
            abort(400, f"no chart on file for prefix {prefix} — "
                       "download it first")
        updates["default_origin"] = origin
    settings.save(updates)
    return jsonify({"saved": True})


@app.post("/admin/settings/turnstile")
@require_admin
def admin_settings_turnstile():
    body = request.get_json(silent=True) or {}
    enabled = bool(body.get("enabled"))
    site_key = (body.get("site_key") or "").strip()
    secret_key = (body.get("secret_key") or "").strip()
    updates = {"turnstile_enabled": enabled}
    if site_key:
        updates["turnstile_site_key"] = site_key
    if secret_key:  # blank leaves the stored secret unchanged
        updates["turnstile_secret_key"] = secret_key
    saved = settings.save(updates)
    if enabled and not (saved["turnstile_site_key"]
                        and saved["turnstile_secret_key"]):
        settings.save({"turnstile_enabled": False})
        abort(400, "both the site key and secret key are needed "
                   "before Turnstile can be enabled")
    return jsonify({"saved": True})


@app.post("/admin/settings/password")
@require_admin
def admin_settings_password():
    body = request.get_json(silent=True) or {}
    current = body.get("current") or ""
    new = body.get("new") or ""
    if not check_admin_password(current):
        time.sleep(1)
        abort(403, "current password didn't match")
    if len(new) < 8:
        abort(400, "new password must be at least 8 characters")
    settings.save({"admin_password_hash": generate_password_hash(new)})
    # rotate the signing key: every session (including this one) signs out
    app.secret_key = settings.rotate_secret_key()
    return jsonify({"saved": True, "signed_out": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)

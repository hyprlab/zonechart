import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import time
from functools import lru_cache, wraps

from flask import (Flask, abort, jsonify, redirect, render_template, request,
                   session, url_for)

from zonechart import parse_chart

CHARTS_DIR = os.environ.get("CHARTS_DIR", "/data/charts")
# bundled seed so a fresh install boots with a working origin; the full
# dataset is fetched from the admin page
LEGACY_CHART = os.environ.get(
    "CHART_PATH", os.path.join(os.path.dirname(__file__), "seed", "439.xls"))
DEFAULT_ORIGIN = os.environ.get("DEFAULT_ORIGIN", "439")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
STATUS_PATH = os.environ.get("REFRESH_STATUS_PATH", "/data/refresh_status.json")
CANCEL_PATH = STATUS_PATH + ".cancel"

app = Flask(__name__)
# stable across restarts so sessions survive; derived, never stored
app.secret_key = hashlib.sha256(
    f"zonechart-session:{ADMIN_PASSWORD}".encode()).digest()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")

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
    return render_template("index.html", default_origin=DEFAULT_ORIGIN)


@app.get("/api/origins")
def api_origins():
    return jsonify({
        "default": DEFAULT_ORIGIN if DEFAULT_ORIGIN in CHARTS else
                   (sorted(CHARTS)[0] if CHARTS else None),
        "available": [
            {"prefix": p, "state": PREFIX_STATES.get(p)}
            for p in sorted(CHARTS)
        ],
        "total_mapped": len(PREFIX_STATES),
    })


@app.get("/api/chart")
def api_chart():
    prefix, zip5 = normalize_origin(request.args.get("origin", DEFAULT_ORIGIN))
    if prefix is None:
        abort(400, "origin must be a 3- or 5-digit ZIP")
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
    if request.method == "POST":
        supplied = request.form.get("password", "")
        if ADMIN_PASSWORD and hmac.compare_digest(supplied, ADMIN_PASSWORD):
            session["admin"] = True
            return redirect(request.args.get("next") or url_for("admin"))
        time.sleep(1)  # blunt brute-force throttle
        error = ("Admin password not set — add ADMIN_PASSWORD to "
                 "docker-compose.yml" if not ADMIN_PASSWORD
                 else "That password didn't match.")
    return render_template("login.html", error=error)


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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)

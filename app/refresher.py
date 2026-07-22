"""Chart refresh job: re-download UPS zone charts through headless Chromium.

Runs as a subprocess of the web app (see /admin) or standalone:

    python3 refresher.py [--force] [--prefixes 439 902 ...]

Progress is written after every prefix to STATUS_PATH as JSON, which the
admin UI polls. A cancel is requested by touching CANCEL_PATH; the job
checks it between prefixes.

ups.com sits behind Akamai bot protection that kills connections from
non-browser TLS fingerprints, and flags the stock headless User-Agent —
so this drives real Chromium with "HeadlessChrome" masked, and warms up
a session on the Daily Rates page first so the bot-sensor cookies exist.
"""

import argparse
import json
import os
import random
import re
import sys
import time

CHARTS_DIR = os.environ.get("CHARTS_DIR", "/data/charts")
STATUS_PATH = os.environ.get("REFRESH_STATUS_PATH", "/data/refresh_status.json")
CANCEL_PATH = STATUS_PATH + ".cancel"

WARMUP_URL = ("https://www.ups.com/us/en/support/shipping-support/"
              "shipping-costs-rates/daily-rates")
URL_PATTERNS = [
    "https://www.ups.com/media/us/currentrates/zone-csv/{p}.xls",
    "https://www.ups.com/media/us/currentrates/zone-excel/{p}.xls",
]
EXCEL_MAGICS = (b"PK\x03\x04", b"\xd0\xcf\x11\xe0")

FETCH_JS = """async (u) => {
  const r = await fetch(u, { credentials: "include" });
  if (!r.ok) return { status: r.status };
  const buf = new Uint8Array(await r.arrayBuffer());
  let bin = "";
  for (let o = 0; o < buf.length; o += 32768)
    bin += String.fromCharCode.apply(null, buf.subarray(o, o + 32768));
  return { status: r.status, b64: btoa(bin) };
}"""


def all_prefixes():
    geo_path = os.path.join(os.path.dirname(__file__),
                            "static", "geo", "zip3.geojson")
    with open(geo_path) as f:
        geo = json.load(f)
    return sorted(feat["properties"]["z"] for feat in geo["features"])


def is_valid_chart(path):
    try:
        with open(path, "rb") as f:
            head = f.read(8)
        return any(head.startswith(m) for m in EXCEL_MAGICS)
    except OSError:
        return False


def write_status(status):
    tmp = STATUS_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(status, f)
    os.replace(tmp, STATUS_PATH)


def masked_user_agent(browser):
    major = browser.version.split(".")[0]
    return (f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36")


def warm_session(p, chromium_args):
    """Launch + warm up; returns (browser, page) or raises."""
    # channel="chromium" = the full browser in new-headless mode; the default
    # headless-shell build gets stream-killed by Akamai
    browser = p.chromium.launch(
        headless=True, channel="chromium",
        args=["--no-sandbox", "--disable-gpu",
              "--disable-blink-features=AutomationControlled", *chromium_args])
    ctx = browser.new_context(user_agent=masked_user_agent(browser),
                              viewport={"width": 1366, "height": 900},
                              locale="en-US")
    page = ctx.new_page()
    # "networkidle" never fires on ups.com (persistent analytics
    # connections) — wait for load, then give the bot-sensor time to run
    page.goto(WARMUP_URL, wait_until="load", timeout=60000)
    time.sleep(5)
    return browser, page


def run(force=False, prefixes=None):
    from base64 import b64decode

    from playwright.sync_api import sync_playwright

    os.makedirs(CHARTS_DIR, exist_ok=True)
    if os.path.exists(CANCEL_PATH):
        os.remove(CANCEL_PATH)

    todo = prefixes or all_prefixes()
    results = {}
    for pfx in todo:
        path = os.path.join(CHARTS_DIR, f"{pfx}.xls")
        if not force and is_valid_chart(path):
            results[pfx] = "cached"
        else:
            results[pfx] = "pending"
    pending = [pfx for pfx in todo if results[pfx] == "pending"]

    status = {
        "state": "starting", "pid": os.getpid(), "force": force,
        "total": len(todo), "results": results, "current": None,
        "counts": {"downloaded": 0,
                   "cached": len(todo) - len(pending), "missing": 0},
        "started_at": time.time(), "finished_at": None, "error": None,
    }
    write_status(status)

    if not pending:
        status.update(state="done", finished_at=time.time())
        write_status(status)
        return 0

    try:
        with sync_playwright() as p:
            # escalation ladder: http/2, then http/1.1 if the h2
            # fingerprint is stream-killed
            try:
                browser, page = warm_session(p, [])
            except Exception:
                browser, page = warm_session(p, ["--disable-http2"])

            status["state"] = "running"
            for i, pfx in enumerate(pending):
                if os.path.exists(CANCEL_PATH):
                    status.update(state="cancelled", current=None,
                                  finished_at=time.time())
                    write_status(status)
                    os.remove(CANCEL_PATH)
                    browser.close()
                    return 1
                status["current"] = pfx
                write_status(status)

                saved = False
                for pattern in URL_PATTERNS:
                    try:
                        res = page.evaluate(FETCH_JS, pattern.format(p=pfx))
                    except Exception:
                        continue
                    if not res.get("b64"):
                        continue
                    blob = b64decode(res["b64"])
                    if any(blob.startswith(m) for m in EXCEL_MAGICS):
                        with open(os.path.join(CHARTS_DIR, f"{pfx}.xls"),
                                  "wb") as f:
                            f.write(blob)
                        saved = True
                        break
                results[pfx] = "downloaded" if saved else "missing"
                status["counts"]["downloaded" if saved else "missing"] += 1
                if i < len(pending) - 1:
                    time.sleep(1.5 + random.random() * 1.5)  # polite pacing
            browser.close()

        status.update(state="done", current=None, finished_at=time.time())
        write_status(status)
        return 0
    except Exception as e:
        status.update(state="error", current=None, finished_at=time.time(),
                      error=f"{type(e).__name__}: {e}"[:300])
        write_status(status)
        return 2


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--prefixes", nargs="*")
    args = ap.parse_args()
    prefixes = [p for p in (args.prefixes or []) if re.fullmatch(r"\d{3}", p)]
    sys.exit(run(force=args.force, prefixes=prefixes or None))

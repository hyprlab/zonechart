#!/usr/bin/env python3
"""Download the UPS zone chart for every origin 3-digit ZIP prefix.

UPS publishes one chart per origin prefix (the "Download Zone Charts" box on
dailyrates.ups.com serves them). This script fetches them all so the app can
offer any origin without uploads.

Uses only the standard library — run it on any machine with open internet
access (the app's container doesn't need network):

    python3 scripts/fetch_charts.py                # full run into data/charts/
    python3 scripts/fetch_charts.py --test         # try one prefix, show result
    python3 scripts/fetch_charts.py --prefixes 439 902 100

Re-running skips files already downloaded and valid, so it's safe to resume
an interrupted run — and it's the refresh mechanism when UPS updates rates
(annually, usually late December): add --force to re-download everything.
"""

import argparse
import json
import pathlib
import sys
import time
import urllib.error
import urllib.request

REPO = pathlib.Path(__file__).resolve().parent.parent

# Tried in order per prefix; first URL that returns a real Excel file wins.
URL_PATTERNS = [
    "https://www.ups.com/media/us/currentrates/zone-csv/{p}.xls",
    "https://www.ups.com/media/us/currentrates/zone-excel/{p}.xls",
]

EXCEL_MAGICS = (b"PK\x03\x04", b"\xd0\xcf\x11\xe0")  # xlsx zip / legacy BIFF
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def all_prefixes():
    """Every prefix that exists on the map — same universe the app renders."""
    geo = json.loads((REPO / "app/static/geo/zip3.geojson").read_text())
    return sorted(f["properties"]["z"] for f in geo["features"])


def is_valid_chart(path):
    try:
        with open(path, "rb") as f:
            head = f.read(8)
        return any(head.startswith(m) for m in EXCEL_MAGICS)
    except OSError:
        return False


def fetch_one(prefix, out_dir, retries=3, verbose=False):
    dest = out_dir / f"{prefix}.xls"
    if is_valid_chart(dest):
        return "cached"
    for url in URL_PATTERNS:
        u = url.format(p=prefix)
        for attempt in range(retries):
            if verbose:
                print(f"  GET {u} (attempt {attempt + 1}, 20s timeout)…",
                      flush=True)
            try:
                req = urllib.request.Request(u, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=20) as r:
                    blob = r.read()
                if any(blob.startswith(m) for m in EXCEL_MAGICS):
                    dest.write_bytes(blob)
                    return "downloaded"
                if verbose:
                    print(f"  → got {len(blob)} bytes but not an Excel file "
                          "(HTML error page?) — trying next pattern", flush=True)
                break  # got HTML/error page — try next pattern, not a retry
            except urllib.error.HTTPError as e:
                if verbose:
                    print(f"  → HTTP {e.code}", flush=True)
                if e.code == 404:
                    break  # prefix not offered at this pattern
                time.sleep(2 ** attempt * 2)
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                if verbose:
                    print(f"  → {type(e).__name__}: {e}", flush=True)
                time.sleep(2 ** attempt * 2)
    return "missing"


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=str(REPO / "data/charts"),
                    help="output directory (default: data/charts)")
    ap.add_argument("--prefixes", nargs="*",
                    help="specific prefixes (default: every mapped prefix)")
    ap.add_argument("--delay", type=float, default=2.0,
                    help="seconds between requests (default: 2)")
    ap.add_argument("--test", action="store_true",
                    help="fetch a single prefix (439) and report, then exit")
    ap.add_argument("--force", action="store_true",
                    help="re-download even if a valid file exists")
    args = ap.parse_args()

    out_dir = pathlib.Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    prefixes = args.prefixes or all_prefixes()
    if args.test:
        # test the network path, so pick a prefix NOT already on disk
        not_cached = [p for p in prefixes
                      if not is_valid_chart(out_dir / f"{p}.xls")]
        if not not_cached:
            print("every prefix is already downloaded — nothing to test")
            return 0
        prefixes = not_cached[:1]

    counts = {"downloaded": 0, "cached": 0, "missing": 0}
    missing = []
    for i, p in enumerate(prefixes):
        if args.force:
            (out_dir / f"{p}.xls").unlink(missing_ok=True)
        result = fetch_one(p, out_dir, verbose=args.test)
        counts[result] += 1
        if result == "missing":
            missing.append(p)
        if result == "downloaded":
            time.sleep(args.delay)
        print(f"\r[{i + 1}/{len(prefixes)}] {p}: {result}   ",
              end="", flush=True)

    print(f"\n\ndownloaded {counts['downloaded']}, "
          f"already had {counts['cached']}, unavailable {counts['missing']}")
    if missing:
        print("unavailable prefixes:", " ".join(missing))
        print("\nIf most prefixes are unavailable, UPS may have moved the "
              "download URLs — grab one chart manually from the Zone Charts "
              "box at https://www.ups.com/us/en/support/shipping-support/"
              "shipping-costs-rates/daily-rates and note the URL your browser "
              "used, then update URL_PATTERNS in this script.")
    return 1 if counts["downloaded"] + counts["cached"] == 0 else 0


if __name__ == "__main__":
    sys.exit(main())

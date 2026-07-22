#!/usr/bin/env node
/* Download UPS zone charts through a real Chrome engine.
 *
 * ups.com sits behind Akamai bot protection that kills connections from
 * curl/python TLS fingerprints (HTTP/2 INTERNAL_ERROR), so this fetcher
 * drives headless Chrome instead: it opens ups.com once to pick up the
 * Akamai session cookies, then fetches each chart from page context with
 * the browser's own network stack.
 *
 *   node scripts/fetch_charts_browser.js --test        # one missing prefix
 *   node scripts/fetch_charts_browser.js               # everything missing
 *   node scripts/fetch_charts_browser.js --prefixes 902 100
 *
 * Resumable: valid files already in data/charts/ are skipped.
 */

const fs = require("fs");
const path = require("path");

const REPO = path.resolve(__dirname, "..");
const OUT_DIR = path.join(REPO, "data", "charts");
const WARMUP_URL =
  "https://www.ups.com/us/en/support/shipping-support/shipping-costs-rates/daily-rates";
const URL_PATTERNS = [
  "https://www.ups.com/media/us/currentrates/zone-csv/{p}.xls",
  "https://www.ups.com/media/us/currentrates/zone-excel/{p}.xls",
];
const CHROME_CANDIDATES = [
  process.env.CHROME_PATH,
  path.join(process.env.HOME || "", ".cache/ms-playwright/chromium-1228/chrome-linux64/chrome"),
  "/usr/bin/google-chrome",
  "/usr/bin/chromium",
].filter(Boolean);

function isValidChart(file) {
  try {
    const fd = fs.openSync(file, "r");
    const head = Buffer.alloc(8);
    fs.readSync(fd, head, 0, 8, 0);
    fs.closeSync(fd);
    return (
      head.subarray(0, 4).equals(Buffer.from("PK\x03\x04", "binary")) ||
      head.equals(Buffer.from([0xd0, 0xcf, 0x11, 0xe0, 0xa1, 0xb1, 0x1a, 0xe1]))
    );
  } catch {
    return false;
  }
}

function allPrefixes() {
  const geo = JSON.parse(
    fs.readFileSync(path.join(REPO, "app/static/geo/zip3.geojson"), "utf8"));
  return geo.features.map(f => f.properties.z).sort();
}

const sleep = ms => new Promise(r => setTimeout(r, ms));

async function main() {
  const args = process.argv.slice(2);
  const test = args.includes("--test");
  const pIdx = args.indexOf("--prefixes");
  const wanted = pIdx >= 0 ? args.slice(pIdx + 1).filter(a => /^\d{3}$/.test(a)) : null;

  const chrome = CHROME_CANDIDATES.find(c => fs.existsSync(c));
  if (!chrome) {
    console.error("No Chrome binary found — set CHROME_PATH");
    process.exit(2);
  }
  fs.mkdirSync(OUT_DIR, { recursive: true });

  let prefixes = (wanted && wanted.length ? wanted : allPrefixes())
    .filter(p => !isValidChart(path.join(OUT_DIR, `${p}.xls`)));
  if (!prefixes.length) {
    console.log("every requested prefix is already downloaded");
    return;
  }
  if (test) prefixes = prefixes.slice(0, 1);
  console.log(`using ${chrome}\nfetching ${prefixes.length} chart(s)…`);

  const puppeteer = (await import("puppeteer-core")).default;

  const BASE_ARGS = ["--no-sandbox", "--disable-gpu",
                     "--disable-blink-features=AutomationControlled"];

  async function launchAndWarm(extraArgs, label) {
    const browser = await puppeteer.launch({
      executablePath: chrome,
      headless: "new",
      args: [...BASE_ARGS, ...extraArgs],
    });
    const page = await browser.newPage();
    await page.setViewport({ width: 1366, height: 900 });
    // the headless build advertises "HeadlessChrome" — an instant bot flag
    const ua = (await browser.userAgent()).replace("HeadlessChrome", "Chrome");
    await page.setUserAgent(ua);
    await page.setExtraHTTPHeaders({ "Accept-Language": "en-US,en;q=0.9" });
    console.log(`warming up Akamai session (${label})…`);
    try {
      await page.goto(WARMUP_URL, { waitUntil: "networkidle2", timeout: 60000 });
      await sleep(4000); // let the bot-sensor script run and set its cookies
      console.log(`warmup OK (${label}): "${await page.title()}"`);
      return { browser, page, ok: true };
    } catch (e) {
      console.log(`warmup failed (${label}): ${e.message}`);
      return { browser, page, ok: false };
    }
  }

  // escalation ladder: normal HTTP/2 first, then HTTP/1.1 if the h2
  // fingerprint is being stream-killed
  let session = await launchAndWarm([], "http/2");
  if (!session.ok) {
    await session.browser.close();
    session = await launchAndWarm(["--disable-http2"], "http/1.1");
    if (!session.ok) console.log("both warmups failed — trying fetches anyway");
  }
  const { browser, page } = session;

  const counts = { downloaded: 0, missing: 0 };
  const missing = [];
  for (let i = 0; i < prefixes.length; i++) {
    const p = prefixes[i];
    let saved = false;
    for (const pattern of URL_PATTERNS) {
      const url = pattern.replace("{p}", p);
      if (test) console.log(`  GET ${url}`);
      let res;
      try {
        res = await page.evaluate(async u => {
          const r = await fetch(u, { credentials: "include" });
          if (!r.ok) return { status: r.status };
          const buf = new Uint8Array(await r.arrayBuffer());
          let bin = "";
          for (let o = 0; o < buf.length; o += 32768)
            bin += String.fromCharCode.apply(null, buf.subarray(o, o + 32768));
          return { status: r.status, b64: btoa(bin) };
        }, url);
      } catch (e) {
        if (test) console.log(`  → page error: ${e.message}`);
        continue;
      }
      if (test && !res.b64) console.log(`  → HTTP ${res.status}`);
      if (!res.b64) continue;
      const blob = Buffer.from(res.b64, "base64");
      const magicOk =
        blob.subarray(0, 4).equals(Buffer.from("PK\x03\x04", "binary")) ||
        blob.subarray(0, 4).equals(Buffer.from([0xd0, 0xcf, 0x11, 0xe0]));
      if (!magicOk) {
        if (test) console.log(`  → ${blob.length} bytes but not Excel (blocked or error page)`);
        continue;
      }
      fs.writeFileSync(path.join(OUT_DIR, `${p}.xls`), blob);
      saved = true;
      break;
    }
    counts[saved ? "downloaded" : "missing"]++;
    if (!saved) missing.push(p);
    process.stdout.write(`\r[${i + 1}/${prefixes.length}] ${p}: ${saved ? "downloaded" : "MISSING"}   `);
    await sleep(1500 + Math.random() * 1500); // polite, jittered
  }

  console.log(`\n\ndownloaded ${counts.downloaded}, unavailable ${counts.missing}`);
  if (missing.length) console.log("unavailable:", missing.join(" "));
  await browser.close();
}

main().catch(e => { console.error(e); process.exit(1); });

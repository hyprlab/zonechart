/* Admin dashboard: trigger the chart refresh and watch it run.
 * Polls /admin/refresh/status while a job is active and paints a
 * per-prefix grid — one cell per origin chart. */

(function () {
  "use strict";

  const $ = id => document.getElementById(id);
  const btn = $("refresh-btn"), cancelBtn = $("cancel-btn"), forceBox = $("force-box");
  const wrap = $("progress-wrap"), fill = $("progress-fill");
  const label = $("progress-label"), current = $("progress-current");
  const grid = $("prefix-grid"), summary = $("run-summary");

  let cells = null;   // prefix -> cell element
  let timer = null;

  function fmtAge(ts) {
    if (!ts) return "";
    const days = Math.floor((Date.now() / 1000 - ts) / 86400);
    return days === 0 ? "updated today" : `updated ${days} day${days === 1 ? "" : "s"} ago`;
  }

  async function loadInfo() {
    const info = await fetch("/admin/info").then(r => r.json());
    $("stat-charts").textContent = info.charts;
    $("stat-of").textContent = `of ${info.mapped_prefixes} mapped prefixes`;
    $("stat-age").textContent = info.newest ? fmtAge(info.newest) : "no charts yet";
  }

  function buildGrid(results) {
    grid.innerHTML = "";
    cells = {};
    for (const p of Object.keys(results).sort()) {
      const c = document.createElement("i");
      c.title = p;
      grid.appendChild(c);
      cells[p] = c;
    }
  }

  function paint(status) {
    const running = status.state === "starting" || status.state === "running";
    btn.disabled = running;
    cancelBtn.hidden = !running;
    wrap.hidden = !running && status.state !== "done" && status.state !== "cancelled" && status.state !== "error";

    if (status.results) {
      if (!cells || Object.keys(cells).length !== Object.keys(status.results).length)
        buildGrid(status.results);
      for (const [p, st] of Object.entries(status.results)) {
        cells[p].className =
          st === "downloaded" ? "sw-done" :
          st === "cached" ? "sw-cached" :
          st === "missing" ? "sw-missing" : "sw-pending";
      }
      if (status.current && cells[status.current]) cells[status.current].className = "sw-active";

      const c = status.counts || {};
      const done = (c.downloaded || 0) + (c.cached || 0) + (c.missing || 0);
      const pct = status.total ? Math.round(100 * done / status.total) : 0;
      fill.style.width = pct + "%";
      label.textContent = running
        ? `${pct}% — ${c.downloaded || 0} downloaded · ${c.cached || 0} current · ${c.missing || 0} unavailable`
        : "";
      current.textContent = status.current ? `fetching ${status.current}…` : "";
    }

    if (!running) {
      current.textContent = "";
      if (status.state === "done") {
        const c = status.counts || {};
        const took = status.finished_at && status.started_at
          ? ` in ${Math.round((status.finished_at - status.started_at) / 60)} min` : "";
        summary.textContent = `✓ Refresh complete${took}: ${c.downloaded || 0} downloaded, `
          + `${c.cached || 0} already current, ${c.missing || 0} unavailable.`;
        summary.className = "run-summary ok";
      } else if (status.state === "cancelled") {
        summary.textContent = "Refresh cancelled — completed charts were kept; run again to resume.";
        summary.className = "run-summary";
      } else if (status.state === "error") {
        summary.textContent = `Refresh failed: ${status.error || "unknown error"}. Re-running resumes where it stopped.`;
        summary.className = "run-summary error";
      }
    } else {
      summary.textContent = "";
    }
  }

  async function poll() {
    const status = await fetch("/admin/refresh/status").then(r => r.json());
    paint(status);
    if (status.state === "starting" || status.state === "running") {
      timer = setTimeout(poll, 1500);
    } else {
      timer = null;
      loadInfo();
    }
  }

  btn.addEventListener("click", async () => {
    const force = forceBox.checked;
    if (force && !confirm("Force re-download of every chart? This takes about 45 minutes."))
      return;
    btn.disabled = true;
    const r = await fetch("/admin/refresh", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ force }),
    });
    if (!r.ok && r.status !== 409) {
      btn.disabled = false;
      summary.textContent = "Could not start the refresh.";
      summary.className = "run-summary error";
      return;
    }
    setTimeout(poll, 800);
  });

  cancelBtn.addEventListener("click", () => fetch("/admin/refresh/cancel", { method: "POST" }));

  /* ---------- settings ---------- */
  const postJSON = (url, body) => fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

  function say(el, text, isError) {
    el.textContent = text;
    el.classList.toggle("error", !!isError);
    if (!isError && text) setTimeout(() => { el.textContent = ""; }, 4000);
  }

  async function apiError(r, fallback) {
    const text = await r.text();
    const m = text.match(/<p>(.+?)<\/p>/);  // flask abort() html body
    return (m && m[1]) || fallback;
  }

  async function loadSettings() {
    const s = await fetch("/admin/settings").then(r => r.json());
    $("lock-box").checked = s.origin_locked;
    $("lock-origin").value = s.default_origin || "";
    $("ts-box").checked = s.turnstile_enabled;
    $("ts-site").value = s.turnstile_site_key || "";
    $("ts-secret").placeholder = s.turnstile_secret_set ? "•••••• (saved)" : "";
  }

  $("frontend-save").addEventListener("click", async () => {
    const r = await postJSON("/admin/settings/frontend", {
      origin_locked: $("lock-box").checked,
      default_origin: $("lock-origin").value.trim(),
    });
    say($("frontend-msg"),
      r.ok ? "Saved." : await apiError(r, "Could not save."), !r.ok);
  });

  $("ts-save").addEventListener("click", async () => {
    const r = await postJSON("/admin/settings/turnstile", {
      enabled: $("ts-box").checked,
      site_key: $("ts-site").value.trim(),
      secret_key: $("ts-secret").value.trim(),
    });
    if (r.ok) $("ts-secret").value = "";
    say($("ts-msg"),
      r.ok ? "Saved — check the sign-in page." : await apiError(r, "Could not save."), !r.ok);
    loadSettings();
  });

  $("pw-save").addEventListener("click", async () => {
    const nw = $("pw-new").value, confirm = $("pw-confirm").value;
    if (nw !== confirm)
      return say($("pw-msg"), "New passwords don't match.", true);
    const r = await postJSON("/admin/settings/password", {
      current: $("pw-current").value, new: nw,
    });
    if (r.ok) {
      say($("pw-msg"), "Password changed — signing you out…");
      setTimeout(() => { location.href = "/login?next=/admin"; }, 1200);
    } else {
      say($("pw-msg"), await apiError(r, "Could not change the password."), true);
    }
  });

  loadInfo();
  loadSettings();
  poll();
})();

"use strict";

let S = null;                 // last /api/state snapshot
let activeQueueId = null;     // which configured queue the Videos/Queue/
                               // Automation views currently show
let browseAccountId = "";     // which channel Playlists/My YouTube browse
let checked = new Set();      // checked vod keys (within the active queue)
let lastSeq = 0;              // event log cursor
let editorKey = null;
let lastVodsJson = "", lastQueueJson = "", lastPlaylistsJson = "";

const el = (id) => document.getElementById(id);

try { activeQueueId = localStorage.getItem("dvr2yt_active_queue") || null; }
catch (e) { /* private window / blocked storage — fine, just no memory */ }

/* ------------------------------------------------------------ helpers */
function fmtSize(n) {
  if (!n) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return n.toFixed(i ? 1 : 0) + " " + units[i];
}
function fmtDur(s) {
  s = Math.floor(s || 0);
  if (!s) return "—";
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return `${h}:${String(m).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}
function toast(msg, ok = false) {
  const t = el("toast");
  t.textContent = msg;
  t.className = "toast" + (ok ? " ok" : "");
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.add("hidden"), 4200);
}
async function api(path, body = null, method = null) {
  const opts = { method: method || "POST",
                 headers: { "Content-Type": "application/json" } };
  if (body !== null) opts.body = JSON.stringify(body);
  const resp = await fetch(path, opts);
  let data = {};
  try { data = await resp.json(); } catch (e) { /* empty */ }
  if (resp.status === 401) { showLogin(true); throw new Error("auth"); }
  if (!resp.ok) {
    toast(data.error || data.detail || `Request failed (${resp.status})`);
    throw new Error("api error");
  }
  refresh();
  return data;
}
// Calls a route scoped to the currently selected queue:
// /api/queues/<activeQueueId><suffix>
function qapi(suffix, body = null, method = null) {
  if (!activeQueueId) { toast("Pick a queue first."); return Promise.reject(new Error("no queue")); }
  return api(`/api/queues/${encodeURIComponent(activeQueueId)}${suffix}`, body, method);
}
function activeQueue() { return (S && activeQueueId) ? S.queues[activeQueueId] : null; }
function queueOrder() { return (S && S.queue_order) || []; }
function accountTitle(id) {
  const a = (S && S.accounts || []).find(a => a.id === id);
  return a ? a.title : (id ? "(session expired)" : "(none)");
}

/* -------------------------------------------------------------- login */
function showLogin(on) {
  el("login-overlay").classList.toggle("hidden", !on);
  if (on) el("login-password").focus();
}
async function doLogin() {
  const resp = await fetch("/api/login", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password: el("login-password").value }),
  });
  if (resp.ok) {
    el("login-password").value = "";
    el("login-error").textContent = "";
    showLogin(false);
    refresh();
    pollEvents();
  } else {
    el("login-error").textContent = "Wrong password — try again.";
  }
}

/* --------------------------------------------------------------- nav */
const QUEUE_SCOPED_VIEWS = new Set(["videos", "queue", "history", "automation"]);
function showView(name) {
  document.querySelectorAll(".nav-btn").forEach(b =>
    b.classList.toggle("active", b.dataset.view === name));
  document.querySelectorAll(".view").forEach(v => v.classList.add("hidden"));
  const view = el("view-" + name);
  view.classList.remove("hidden", "anim");
  void view.offsetWidth;          // restart the entry animation
  view.classList.add("anim");
  el("queue-picker").classList.toggle("hidden", !QUEUE_SCOPED_VIEWS.has(name));
  el("no-queues-notice").classList.toggle(
    "hidden", !(QUEUE_SCOPED_VIEWS.has(name) && queueOrder().length === 0));
  if (name === "about") loadChangelog();
  if (name === "history") loadHistory();
  if (name === "playlists") loadPlaylists();
  if (name === "myyt" && ytVideos.length === 0) loadYt();
}
document.querySelectorAll(".nav-btn").forEach(btn => {
  btn.addEventListener("click", () => showView(btn.dataset.view));
});

/* -------------------------------------------------------------- about */
let changelogLoaded = false;
async function loadChangelog() {
  el("about-version").textContent = S ? S.version : "…";
  if (changelogLoaded) return;
  try {
    const resp = await fetch("/api/changelog");
    const data = await resp.json();
    changelogLoaded = true;
    el("changelog").innerHTML = data.changelog
      ? mdToHtml(data.changelog)
      : `<p class="muted">No bundled notes — see the
         <a href="https://github.com/mrfroncu/TwitchDVR-to-YOUTUBE/releases"
            target="_blank" rel="noopener">releases page</a>.</p>`;
  } catch (e) {
    el("changelog").textContent = "Could not load release notes.";
  }
}
function mdToHtml(md) {
  const lines = esc(md).split("\n");
  const out = [];
  let inList = false;
  const closeList = () => { if (inList) { out.push("</ul>"); inList = false; } };
  for (let ln of lines) {
    ln = ln.replace(/`([^`]+)`/g, "<code>$1</code>")
           .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>");
    if (ln.startsWith("### ")) { closeList(); out.push("<h4>" + ln.slice(4) + "</h4>"); }
    else if (ln.startsWith("## ")) { closeList(); out.push("<h3>" + ln.slice(3) + "</h3>"); }
    else if (ln.startsWith("# ")) { closeList(); out.push("<h2>" + ln.slice(2) + "</h2>"); }
    else if (ln.startsWith("- ")) {
      if (!inList) { out.push("<ul>"); inList = true; }
      out.push("<li>" + ln.slice(2) + "</li>");
    }
    else if (ln.trim() === "") closeList();
    else if (inList) out.push(ln);
    else out.push("<p>" + ln + "</p>");
  }
  closeList();
  return out.join("\n");
}
async function checkUpdates() {
  const data = await api("/api/update/check");
  if (data.update) {
    el("update-banner").removeAttribute("data-dismissed");
    toast(`Version ${data.update.version} is available — see the banner.`, true);
  } else {
    toast("You're running the latest version.", true);
  }
}

/* ---------------------------------------------------------- desktop bits */
function renderDesktop() {
  const isDesktop = !!S.desktop;
  el("browser-signin").classList.toggle("hidden", !isDesktop);
  el("desktop-settings").classList.toggle("hidden", !isDesktop);
  el("about-check-updates").classList.toggle("hidden", !isDesktop);
  el("about-version").textContent = S.version;
  if (isDesktop && document.activeElement !== el("set-ui-mode")) {
    el("set-ui-mode").value = S.cfg.ui_mode || "studio";
  }
  const banner = el("update-banner");
  if (isDesktop && S.update && !banner.dataset.dismissed) {
    el("update-version").textContent = "v" + S.update.version;
    banner.classList.remove("hidden");
  } else if (!S.update) {
    banner.classList.add("hidden");
  }
}
function saveUiMode() {
  api("/api/settings", { ui_mode: el("set-ui-mode").value });
  toast("Interface saved — restart the app to apply.", true);
}
function saveQueueLayout() {
  api("/api/settings", { queue_ui_layout: el("set-queue-layout").value });
}
async function applyUpdate() {
  if (!confirm(`Download v${S.update.version} and restart now?`)) return;
  try {
    await api("/api/update/apply");
    toast("Updating — the app will restart itself…", true);
  } catch (e) { /* toast shown */ }
}

/* ------------------------------------------------------- queue picker */
function statusDotClass(q) {
  if (q.uploading) return "on";
  if (q.cooldown) return "cooldown";
  return "";
}
function queueStatusText(q) {
  if (q.uploading) return "uploading";
  if (!q.enabled) return "disabled";
  if (q.cooldown) return "cooldown until " + q.cooldown;
  if (!q.signed_in) return "not signed in";
  const pending = q.queue.filter(i => i.status === "queued").length;
  return pending ? `${pending} pending` : "idle";
}
function renderQueuePicker() {
  const order = queueOrder();
  if (!activeQueueId || !S.queues[activeQueueId]) {
    activeQueueId = order[0] || null;
    try {
      if (activeQueueId) localStorage.setItem("dvr2yt_active_queue", activeQueueId);
    } catch (e) { /* ignore */ }
  }
  const layout = (S.cfg && S.cfg.queue_ui_layout) || "switcher";
  el("queue-select").classList.toggle("hidden", layout !== "switcher");
  el("queue-tabs").classList.toggle("hidden", layout !== "tabs");

  if (layout === "switcher") {
    const sel = el("queue-select");
    if (document.activeElement !== sel) {
      sel.innerHTML = "";
      for (const id of order) {
        const o = document.createElement("option");
        o.value = id;
        o.textContent = S.queues[id].name;
        sel.appendChild(o);
      }
      if (activeQueueId) sel.value = activeQueueId;
    }
  } else {
    const box = el("queue-tabs");
    box.innerHTML = "";
    for (const id of order) {
      const q = S.queues[id];
      const btn = document.createElement("button");
      btn.className = "queue-tab" + (id === activeQueueId ? " active" : "");
      btn.innerHTML = `<span class="dot ${statusDotClass(q)}"></span>${esc(q.name)}`;
      btn.addEventListener("click", () => setActiveQueue(id));
      box.appendChild(btn);
    }
  }
  const q = activeQueue();
  el("queue-picker-status").textContent = q ? queueStatusText(q) : "";
}
function setActiveQueue(id) {
  if (id === activeQueueId) return;
  activeQueueId = id || null;
  try {
    if (activeQueueId) localStorage.setItem("dvr2yt_active_queue", activeQueueId);
  } catch (e) { /* ignore */ }
  checked = new Set();
  lastVodsJson = lastQueueJson = "";
  renderQueuePicker();
  renderVideos();
  renderQueue();
  renderCooldown();
  renderScan();
  renderAutomationStatus();
  const name = (activeQueue() || {}).name || "";
  el("videos-queue-name").textContent = name ? "— " + name : "";
  el("queue-queue-name").textContent = name ? "— " + name : "";
  el("auto-queue-name").textContent = name ? "— " + name : "";
}

/* ------------------------------------------------------- queues admin */
function renderQueuesTable() {
  const tbody = el("queues-table").querySelector("tbody");
  tbody.innerHTML = "";
  const order = queueOrder();
  if (!order.length) {
    tbody.innerHTML = `<tr><td colspan="7" class="empty">No queues yet — add one above.</td></tr>`;
    return;
  }
  order.forEach((id, idx) => {
    const q = S.queues[id];
    const pending = q.queue.filter(i => i.status === "queued").length;
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><input type="checkbox" ${q.enabled ? "checked" : ""} title="Enabled"></td>
      <td>${esc(q.name)}</td>
      <td class="mono" title="${esc(q.vod_folder)}">${esc(q.vod_folder)}</td>
      <td>${esc(q.channel || accountTitle(q.account_id))}</td>
      <td class="${q.cooldown ? "st-err" : q.uploading ? "st-info" : ""}">${esc(queueStatusText(q))}</td>
      <td>${pending}</td>
      <td class="field-row" style="margin:0;flex-wrap:nowrap">
        <button class="btn btn-ghost sm" data-up ${idx === 0 ? "disabled" : ""}>▲</button>
        <button class="btn btn-ghost sm" data-down ${idx === order.length - 1 ? "disabled" : ""}>▼</button>
        <button class="btn btn-ghost sm" data-rm title="Remove">🗑</button>
      </td>`;
    tr.querySelector("[data-up]").addEventListener("click", (e) => {
      e.stopPropagation(); moveQueueDef(idx, -1);
    });
    tr.querySelector("[data-down]").addEventListener("click", (e) => {
      e.stopPropagation(); moveQueueDef(idx, 1);
    });
    tr.querySelector("[data-rm]").addEventListener("click", (e) => {
      e.stopPropagation(); removeQueueDef(id, q.name);
    });
    tr.querySelector('input[type="checkbox"]').addEventListener("click", async (e) => {
      e.stopPropagation();
      await api(`/api/queues/${encodeURIComponent(id)}`, { enabled: e.target.checked }, "PATCH");
    });
    tr.addEventListener("click", () => openQueueEditor(id));
    tbody.appendChild(tr);
  });
}
async function moveQueueDef(idx, delta) {
  const order = [...queueOrder()];
  const j = idx + delta;
  if (j < 0 || j >= order.length) return;
  [order[idx], order[j]] = [order[j], order[idx]];
  await api("/api/queues/reorder", { order });
}
async function removeQueueDef(id, name) {
  if (!confirm(`Remove queue "${name}"? Its pending items and settings are deleted ` +
               `(the local video files and anything already on YouTube are not).`)) return;
  await api(`/api/queues/${encodeURIComponent(id)}`, null, "DELETE");
  if (activeQueueId === id) { activeQueueId = null; }
  toast("Queue removed.", true);
}

/* ------------------------------------------------------- queue editor */
let qdefEditingId = null;
function fillQueueAccountSelect(current) {
  const sel = el("qd-account");
  sel.innerHTML = '<option value="">(none yet)</option>';
  for (const a of (S.accounts || [])) {
    const o = document.createElement("option");
    o.value = a.id;
    o.textContent = a.title;
    sel.appendChild(o);
  }
  sel.value = current || "";
}
function openQueueEditor(id) {
  qdefEditingId = id;
  const isNew = !id;
  const q = id ? S.queues[id] : null;
  const st = q ? q.settings : {};
  el("qdef-editor-title").textContent = isNew ? "Add queue" : "Edit queue";
  el("qdef-remove-btn").classList.toggle("hidden", isNew);
  el("qd-name").value = q ? q.name : "";
  el("qd-folder").value = q ? q.vod_folder : (S.vods_default || "");
  fillQueueAccountSelect(q ? q.account_id : "");
  el("qd-enabled").checked = q ? !!q.enabled : true;

  el("qd-privacy").value = st.privacy || "private";
  el("qd-category").value = String(st.category_id || "20");
  el("qd-template").value = st.title_template || "{title} | {streamer} VOD {date}";
  el("qd-desc-template").value = st.description_template || "";
  el("qd-notify").checked = !!st.notify_subscribers;
  el("qd-kids").checked = !!st.made_for_kids;
  el("qd-extra-tags").value = st.extra_tags || "";

  el("qd-daily").value = st.daily_upload_limit ?? 0;
  el("qd-uploads-24h").textContent = q ? q.uploads_last_24h : 0;
  el("qd-speed").value = st.upload_speed_limit ?? 0;
  el("qd-cooldown-h").value = st.cooldown_hours ?? 24.5;
  el("qd-verify").checked = st.verify_uploads !== false;

  el("qd-after").value = st.after_upload || "keep";
  el("qd-lsdvr-delete").checked = !!st.lsdvr_delete;

  el("qd-auto-scan").checked = !!st.auto_scan;
  el("qd-auto-interval").value = st.auto_scan_interval_min ?? 10;
  el("qd-auto-queue").checked = st.auto_queue !== false;
  el("qd-auto-start").checked = st.auto_start !== false;
  el("qd-auto-finalized").checked = st.auto_only_finalized !== false;

  const mode = st.playlist_mode || "none";
  const radio = document.querySelector(`input[name="qd-plmode"][value="${mode}"]`);
  if (radio) radio.checked = true;
  el("qd-pl-fixed-title").value = st.playlist_fixed_title || "";
  el("qd-pl-template").value = st.playlist_template || "{streamer} VODs {year}";

  el("qdef-editor").classList.remove("hidden");
}
function closeQueueEditor() {
  qdefEditingId = null;
  el("qdef-editor").classList.add("hidden");
}
async function saveQueueEditor() {
  const settings = {
    privacy: el("qd-privacy").value,
    category_id: el("qd-category").value,
    title_template: el("qd-template").value,
    description_template: el("qd-desc-template").value,
    notify_subscribers: el("qd-notify").checked,
    made_for_kids: el("qd-kids").checked,
    extra_tags: el("qd-extra-tags").value,
    daily_upload_limit: parseInt(el("qd-daily").value) || 0,
    upload_speed_limit: parseFloat(el("qd-speed").value) || 0,
    cooldown_hours: parseFloat(el("qd-cooldown-h").value) || 24.5,
    verify_uploads: el("qd-verify").checked,
    after_upload: el("qd-after").value,
    lsdvr_delete: el("qd-lsdvr-delete").checked,
    auto_scan: el("qd-auto-scan").checked,
    auto_scan_interval_min: parseInt(el("qd-auto-interval").value) || 10,
    auto_queue: el("qd-auto-queue").checked,
    auto_start: el("qd-auto-start").checked,
    auto_only_finalized: el("qd-auto-finalized").checked,
    playlist_mode: document.querySelector('input[name="qd-plmode"]:checked')?.value || "none",
    playlist_fixed_title: el("qd-pl-fixed-title").value,
    playlist_fixed_id: "",
    playlist_template: el("qd-pl-template").value,
  };
  const name = el("qd-name").value.trim() || "Queue";
  const folder = el("qd-folder").value.trim();
  const accountId = el("qd-account").value;
  try {
    if (!qdefEditingId) {
      const created = await api("/api/queues", { name, vod_folder: folder, account_id: accountId });
      await api(`/api/queues/${encodeURIComponent(created.id)}`,
                { ...settings, enabled: el("qd-enabled").checked }, "PATCH");
      setActiveQueue(created.id);
    } else {
      await api(`/api/queues/${encodeURIComponent(qdefEditingId)}`, {
        name, vod_folder: folder, account_id: accountId,
        enabled: el("qd-enabled").checked, ...settings,
      }, "PATCH");
    }
  } catch (e) { return; /* toast already shown */ }
  toast("Queue saved.", true);
  closeQueueEditor();
}
function removeQueueFromEditor() {
  if (!qdefEditingId) return;
  const name = el("qd-name").value || "this queue";
  removeQueueDef(qdefEditingId, name);
  closeQueueEditor();
}

/* ------------------------------------------------------------ videos */
let vodsSort = { col: "", rev: false };
function sortVods(col) {
  vodsSort = { col, rev: vodsSort.col === col && !vodsSort.rev };
  lastVodsJson = "";      // force re-render
  renderVideos();
}
function vodsSorted() {
  const q = activeQueue();
  if (!q) return [];
  if (!vodsSort.col) return q.vods;
  const c = vodsSort.col;
  const key = (v) => (c === "size" || c === "duration" || c === "chapters")
    ? (v[c] || 0) : String(v[c] ?? "").toLowerCase();
  return [...q.vods].sort((a, b) =>
    (key(a) > key(b) ? 1 : key(a) < key(b) ? -1 : 0) * (vodsSort.rev ? -1 : 1));
}
function statusClass(st) {
  if (st.startsWith("uploaded")) return "st-ok";
  if (st.includes("failed") || st.includes("no video")) return "st-err";
  if (st === "uploading" || st === "verifying" || st === "queued") return "st-info";
  return "";
}
function renderVideos() {
  const tbody = el("videos-table").querySelector("tbody");
  tbody.innerHTML = "";
  for (const v of vodsSorted()) {
    const tr = document.createElement("tr");
    if (checked.has(v.key)) tr.classList.add("checked");
    tr.innerHTML = `
      <td><input type="checkbox" ${checked.has(v.key) ? "checked" : ""}></td>
      <td>${v.date || "—"}</td>
      <td>${esc(v.streamer)}</td>
      <td>${esc(v.stream_title)}</td>
      <td>${fmtDur(v.duration)}</td>
      <td>${fmtSize(v.size)}</td>
      <td>${v.chapters}</td>
      <td class="${statusClass(v.status)}">${esc(v.status)}</td>`;
    tr.querySelector("input").addEventListener("click", (e) => {
      e.stopPropagation();
      if (e.target.checked) checked.add(v.key); else checked.delete(v.key);
      tr.classList.toggle("checked", e.target.checked);
    });
    tr.addEventListener("click", () => openEditor(v.key));
    tbody.appendChild(tr);
  }
}
function esc(s) {
  return String(s ?? "").replace(/[&<>"]/g,
    ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[ch]));
}
function checkAll(on) {
  const q = activeQueue();
  checked = on && q ? new Set(q.vods.map(v => v.key)) : new Set();
  renderVideos();
}
async function doScan() {
  await qapi("/scan", {});
  renderScan();   // show "scanning" immediately; polling keeps it updated
}
function renderScan() {
  const chip = el("scan-status");
  const q = activeQueue();
  const scan = (q && q.scan) || {};
  el("scan-btn").disabled = !!scan.active;
  if (document.activeElement !== el("folder-input")) {
    el("folder-input").value = q ? q.vod_folder : "";
  }
  if (scan.active) {
    chip.textContent = scan.total
      ? `⏳ Scanning ${scan.done}/${scan.total}: ${String(scan.current).slice(0, 32)}`
      : "⏳ Scanning…";
    chip.className = "chip chip-muted";
    chip.classList.remove("hidden");
  } else if (scan.error) {
    chip.textContent = "❌ " + scan.error;
    chip.className = "chip chip-err";
    chip.classList.remove("hidden");
  } else if (scan.found === 0) {
    chip.textContent = "⚠ No VOD folders found in this folder";
    chip.className = "chip chip-warn";
    chip.classList.remove("hidden");
  } else if (scan.found != null) {
    chip.textContent = `✅ Found ${scan.found} VOD folder(s)`;
    chip.className = "chip";
    chip.classList.remove("hidden");
  } else {
    chip.classList.add("hidden");
  }
}
async function bulk(action, value = "", confirmFirst = false) {
  if (!checked.size) { toast("Nothing checked — tick some rows first."); return; }
  if (confirmFirst && !confirm(
      `${action === "delete_local"
        ? "PERMANENTLY delete the WHOLE local folders (video + metadata) of"
        : "Apply to"} ${checked.size} checked video(s)?`)) return;
  await qapi("/bulk", { action, keys: [...checked], value });
}
async function forceDeleteLocal() {
  if (!checked.size) { toast("Nothing checked — tick some rows first."); return; }
  if (!confirm(
      `Delete ${checked.size} whole VOD folder(s) directly from disk, skipping ` +
      "LiveStreamDVR entirely?\n\nOnly do this if LiveStreamDVR itself can't " +
      "delete them (its server is unreachable or out of disk space, etc.) — " +
      "if LiveStreamDVR is still actively watching/recording any of these " +
      "folders, deleting them behind its back can crash it.")) return;
  await qapi("/bulk", { action: "delete_local_force", keys: [...checked], value: "" });
}
const _opWasActive = {};
function renderOps() {
  const chip = el("op-status");
  const q = activeQueue();
  el("btn-force-delete").classList.toggle(
    "hidden", !(q && q.settings && q.settings.lsdvr_delete));
  const op = (q && q.op) || {};
  if (op.active) {
    chip.textContent = `🗑 ${op.label}… ${op.done}/${op.total}`;
    chip.className = "chip chip-muted";
    chip.classList.remove("hidden");
  } else if (op.result) {
    const ok = op.ok_count ?? op.total ?? 0;
    const failed = (op.total || 0) > 0 && ok === 0;
    chip.textContent = (failed ? "❌ " : "✅ ") + op.result;
    chip.className = failed ? "chip chip-err" : "chip";
    chip.classList.remove("hidden");
    if (q && failed && _opWasActive[q.id]) {
      toast(`${op.result} — nothing was removed. Check the Log for why.`);
    }
  } else {
    chip.classList.add("hidden");
  }
  if (q) _opWasActive[q.id] = !!op.active;
}

/* ------------------------------------------------------------ editor */
function playlistChoices() {
  const q = activeQueue();
  return ["(default)", "(none)", ...(q ? q.playlists.map(p => p.title) : [])];
}
function fillSelect(sel, values, current) {
  sel.innerHTML = "";
  for (const v of values) {
    const o = document.createElement("option");
    o.textContent = v;
    sel.appendChild(o);
  }
  if (current && values.includes(current)) sel.value = current;
}
function openEditor(key) {
  const q = activeQueue();
  const v = q && q.vods.find(x => x.key === key);
  if (!v) return;
  editorKey = key;
  el("editor-title-label").textContent = v.stream_title.slice(0, 60);
  el("ed-title").value = v.meta.title || "";
  el("ed-tags").value = v.meta.tags || "";
  el("ed-privacy").value = v.meta.privacy || "private";
  fillSelect(el("ed-playlist"), playlistChoices(), v.meta.playlist_choice || "(default)");
  el("ed-desc").value = v.meta.description || "";
  updateTitleCount();
  el("editor").classList.remove("hidden");
}
function closeEditor() { editorKey = null; el("editor").classList.add("hidden"); }
function updateTitleCount() {
  el("title-count").textContent = `${el("ed-title").value.length}/100`;
}
el("ed-title").addEventListener("input", updateTitleCount);
async function saveEditor(andQueue = false) {
  if (!editorKey) return;
  await qapi(`/meta/${encodeURIComponent(editorKey)}`, {
    title: el("ed-title").value,
    tags: el("ed-tags").value,
    privacy: el("ed-privacy").value,
    playlist_choice: el("ed-playlist").value,
    description: el("ed-desc").value,
  }, "PATCH");
  if (andQueue) await qapi("/bulk", { action: "queue", keys: [editorKey], value: "" });
  toast(andQueue ? "Saved and queued." : "Saved.", true);
  if (andQueue) closeEditor();
}
async function resetEditor() {
  if (!editorKey) return;
  await qapi("/bulk", { action: "reset_meta", keys: [editorKey], value: "" });
  setTimeout(() => openEditor(editorKey), 300);
}

/* ------------------------------------------------------------- queue */
async function startUploads() {
  const q = activeQueue();
  if (!q) return;
  if (S.quota_cooldown || q.cooldown) {
    const reason = S.quota_cooldown
      ? `the shared API-quota cooldown until ${S.quota_cooldown} (${S.quota_cooldown_reason})`
      : `this queue's channel limit until ${q.cooldown} (${q.cooldown_reason})`;
    if (!confirm(`Upload cooldown is active — ${reason}. Uploads resume ` +
                 `automatically then.\n\nForce-start now anyway?`)) return;
    await qapi("/queue/start", { force: true });
    return;
  }
  await qapi("/queue/start", {});
}
function renderCooldown() {
  const q = activeQueue();
  const banner = el("cooldown-banner");
  if (q && q.cooldown) {
    el("cooldown-until").textContent = q.cooldown;
    el("cooldown-reason").textContent = q.cooldown_reason || "limit";
    banner.classList.remove("hidden");
  } else {
    banner.classList.add("hidden");
  }
  const quotaBanner = el("quota-cooldown-banner");
  if (S.quota_cooldown) {
    el("quota-cooldown-until").textContent = S.quota_cooldown;
    el("quota-cooldown-reason").textContent = S.quota_cooldown_reason || "limit";
    quotaBanner.classList.remove("hidden");
  } else {
    quotaBanner.classList.add("hidden");
  }
  const chip = el("quota-chip");
  if (S.quota_cooldown) {
    chip.textContent = "⏳ API quota cooldown until " + S.quota_cooldown;
    chip.classList.remove("hidden");
  } else {
    chip.classList.add("hidden");
  }
}
function renderQueue() {
  const box = el("queue-list");
  const q = activeQueue();
  box.innerHTML = "";
  if (!q || !q.queue.length) {
    box.innerHTML = `<div class="empty">Queue is empty — add videos from the Videos section.</div>`;
    el("queue-badge").classList.add("hidden");
    return;
  }
  q.queue.forEach((item, idx) => {
    const div = document.createElement("div");
    div.className = "q-item";
    div.innerHTML = `
      <div>${idx + 1}</div>
      <div style="min-width:0">
        <div class="q-title">${esc(item.title)}</div>
        <div class="q-detail">📁 ${esc(item.key)} · ${fmtSize(item.size)} · ${esc(item.privacy)} · ${esc(item.detail || "")}</div>
        ${item.local ? `<div class="q-detail ${LOCAL_LABELS[item.local.state]?.[1] || ""}"
          title="${esc(item.local.path || "")}">${localLabel(item.local)}</div>` : ""}
      </div>
      <span class="q-status ${item.status}">${item.status}${item.status === "uploading" ? " " + item.progress + "%" : ""}</span>
      <div class="q-arrows">
        ${item.video_id ? `<button class="btn btn-ghost sm" data-open title="Open on YouTube">🌐</button>` : ""}
        <button class="btn btn-ghost sm" data-d="-1">▲</button>
        <button class="btn btn-ghost sm" data-d="1">▼</button>
      </div>
      <button class="btn btn-ghost sm" data-rm>✕</button>
      <div class="q-progress"><div style="width:${item.progress || 0}%"></div></div>`;
    const openBtn = div.querySelector("[data-open]");
    if (openBtn) openBtn.addEventListener("click", () =>
      window.open("https://youtu.be/" + item.video_id));
    div.querySelectorAll("[data-d]").forEach(b => b.addEventListener("click", () =>
      qapi("/queue/move", { key: item.key, delta: parseInt(b.dataset.d) })));
    div.querySelector("[data-rm]").addEventListener("click", () =>
      qapi("/queue/remove", { keys: [item.key] }));
    box.appendChild(div);
  });
  const pending = q.queue.filter(i => i.status === "queued").length;
  const badge = el("queue-badge");
  badge.textContent = pending;
  badge.classList.toggle("hidden", pending === 0);
}

/* ----------------------------------------------------------- history */
const LOCAL_LABELS = {                // registry local.state -> [label, css]
  deleted: ["🗑 Local files deleted", "st-ok"],
  kept: ["📁 Kept", ""],
  failed: ["❌ Delete failed", "st-err"],
  missing: ["Already gone", ""],
};
const CHECK_LABELS = {                // server-side check -> [label, css]
  gone: ["🗑 Deleted ✓", "st-ok"],
  still_on_disk: ["⚠ Deleted, but still on disk", "st-err"],
  failed: ["❌ Delete failed", "st-err"],
  kept: ["📁 Kept", ""],
  missing: ["Already gone", ""],
  legacy: ["— not tracked", "muted"],
};
function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleString();
}
function localLabel(local) {
  const [label] = LOCAL_LABELS[local.state] || [local.state];
  return `${label} · ${esc(local.detail || "")} · ${fmtTime(local.at)}`;
}
let histRows = [], historyAfter = "keep";
async function loadHistory() {
  if (!activeQueueId) return;
  el("hist-summary").innerHTML = `<span class="chip chip-muted">checking disk…</span>`;
  try {
    const data = await qapi("/history", null, "GET");
    histRows = data.history || [];
    historyAfter = data.after_upload || "keep";
    renderHistory();
  } catch (e) { el("hist-summary").innerHTML = ""; }
}
function histPresent(r) {
  // whatever delete-after-upload targets is still there
  return historyAfter === "trash_video" ? r.video_exists : r.folder_exists;
}
function histProblem(r) {
  if (r.check === "still_on_disk" || r.check === "failed") return true;
  // deletion is on, upload went through, but the files never went away
  return historyAfter !== "keep" && r.verified && histPresent(r)
    && (r.check === "kept" || r.check === "legacy");
}
function renderHistory() {
  const q = el("hist-search").value.trim().toLowerCase();
  const filter = el("hist-filter").value;
  const gone = histRows.filter(r => r.check === "gone").length;
  const problems = histRows.filter(histProblem).length;
  const present = histRows.filter(r => r.folder_exists).length;
  el("hist-summary").innerHTML = `
    <span class="chip chip-muted">${histRows.length} uploaded</span>
    <span class="chip">🗑 ${gone} deleted &amp; confirmed gone</span>
    <span class="chip chip-muted">📁 ${present} folder(s) still on disk</span>
    <span class="chip ${problems ? "chip-warn" : "chip-muted"}">⚠ ${problems} need attention</span>
    <span class="chip chip-muted">after upload: ${esc({ keep: "keep files",
      trash_video: "delete video file", trash_folder: "delete whole folder" }[historyAfter]
      || historyAfter)}</span>`;
  const rows = histRows.filter(r => {
    if (q && !(r.key.toLowerCase().includes(q) || r.title.toLowerCase().includes(q))) return false;
    if (filter === "problems") return histProblem(r);
    if (filter === "gone") return r.check === "gone";
    if (filter === "present") return r.folder_exists;
    return true;
  });
  const tbody = el("history-table").querySelector("tbody");
  tbody.innerHTML = rows.length ? ""
    : `<tr><td colspan="6" class="empty">Nothing here.</td></tr>`;
  for (const r of rows) {
    const [label, cls] = CHECK_LABELS[r.check] || [r.check, ""];
    const local = r.local || {};
    const tr = document.createElement("tr");
    if (histProblem(r)) tr.classList.add("hist-problem");
    tr.innerHTML = `
      <td>${fmtTime(r.uploaded_at)}</td>
      <td class="mono" title="${esc(r.folder)}">${esc(r.key)}</td>
      <td>${esc(r.title)}</td>
      <td>${r.video_id ? `<a href="https://youtu.be/${esc(r.video_id)}" target="_blank"
        rel="noopener">${esc(r.video_id)}</a>` : "—"}
        ${r.failed ? `<div class="st-err">failed on YouTube</div>`
          : r.verified ? `<div class="st-ok">verified</div>` : `<div class="muted">unverified</div>`}</td>
      <td class="${cls}" title="${esc(local.path || "")}">${label}
        ${local.detail ? `<div class="muted">${esc(local.detail)}</div>` : ""}
        ${local.at ? `<div class="muted">${fmtTime(local.at)}</div>` : ""}</td>
      <td class="${histPresent(r) && historyAfter !== "keep" ? "st-err" : ""}">${
        r.folder_exists ? "📁 folder present" + (r.video_exists ? " · video file present" : "")
                        : "✓ folder gone"}</td>`;
    tbody.appendChild(tr);
  }
}

/* -------------------------------------------------------- automation */
function renderAutomationStatus() {
  const q = activeQueue();
  el("auto-status").textContent = q
    ? (q.settings.auto_scan ? q.auto_status : "off") : "";
}

/* --------------------------------------------------------- playlists */
function renderAccountSelectors() {
  if (!browseAccountId) {
    const aq = activeQueue();
    browseAccountId = (aq && aq.account_id) || (S.accounts[0] && S.accounts[0].id) || "";
  }
  for (const id of ["browse-account-playlists", "browse-account-myyt"]) {
    const sel = el(id);
    if (document.activeElement === sel) continue;
    sel.innerHTML = "";
    for (const a of (S.accounts || [])) {
      const o = document.createElement("option");
      o.value = a.id;
      o.textContent = a.title;
      sel.appendChild(o);
    }
    if (browseAccountId) sel.value = browseAccountId;
  }
}
function setBrowseAccount(id) {
  if (id === browseAccountId) return;
  browseAccountId = id;
  for (const selId of ["browse-account-playlists", "browse-account-myyt"]) {
    el(selId).value = id;
  }
  playlistsLoaded = false;
  ytVideos = [];
  loadPlaylists();
  loadYt();
}
let playlistsCache = [], playlistsLoaded = false;
async function loadPlaylists() {
  if (!browseAccountId) return;
  try {
    const data = await api(`/api/playlists?account_id=${encodeURIComponent(browseAccountId)}`,
                           null, "GET");
    playlistsCache = data.playlists || [];
    playlistsLoaded = true;
    renderPlaylists();
  } catch (e) { /* toast already shown */ }
}
function renderPlaylists() {
  const tbody = el("playlists-table").querySelector("tbody");
  tbody.innerHTML = "";
  for (const p of playlistsCache) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${esc(p.title)}</td><td>${p.count}</td>
      <td>${esc(p.privacy)}</td><td class="muted">${esc(p.id)}</td>`;
    tbody.appendChild(tr);
  }
  const q = activeQueue();
  fillSelect(el("bulk-playlist"), playlistChoices(), el("bulk-playlist").value);
  fillSelect(el("yt-bulk-playlist"), q ? q.playlists.map(p => p.title) : [],
             el("yt-bulk-playlist").value);
  fillSelect(el("yt-add-playlist"), q ? q.playlists.map(p => p.title) : [],
             el("yt-add-playlist").value);
}
async function createPlaylist() {
  const title = el("new-pl-title").value.trim();
  if (!title) return;
  el("new-pl-title").value = "";
  await api(`/api/playlists?account_id=${encodeURIComponent(browseAccountId)}`,
            { title, privacy: el("new-pl-privacy").value });
  loadPlaylists();
}

/* ------------------------------------------------------ video preview */
function openVideoModal(key) {
  if (!key || !activeQueueId) return;
  const q = activeQueue();
  const v = q && q.vods.find(x => x.key === key);
  if (!v) return;
  if ((v.problems || []).some(p => p.includes(".ts"))) {
    toast("Raw .ts captures may not play in the browser — use a local player.");
  }
  el("video-modal-title").textContent = v.stream_title || key;
  el("video-player").src =
    `/api/queues/${encodeURIComponent(activeQueueId)}/video/${encodeURIComponent(key)}/stream`;
  el("video-modal").classList.remove("hidden");
  el("video-player").play().catch(() => {});
}
function closeVideoModal() {
  const player = el("video-player");
  player.pause();
  player.removeAttribute("src");
  player.load();
  el("video-modal").classList.add("hidden");
}
el("video-modal").addEventListener("click", (e) => {
  if (e.target === el("video-modal")) closeVideoModal();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeVideoModal();
});

/* --------------------------------------------------------- my youtube */
let ytVideos = [];
let ytChecked = new Set();
let ytSort = { col: "", rev: false };
let ytEditorId = null;
let ytMemberships = [];

async function loadYt() {
  if (!browseAccountId) return;
  el("yt-count").textContent = "loading…";
  try {
    const data = await api(`/api/yt/videos?account_id=${encodeURIComponent(browseAccountId)}`,
                           null, "GET");
    ytVideos = data.videos || [];
    ytChecked = new Set([...ytChecked].filter(id => ytVideos.some(v => v.id === id)));
    el("yt-count").textContent = `${ytVideos.length} video(s)`;
    renderYt();
  } catch (e) { el("yt-count").textContent = ""; }
}
function sortYt(col) {
  ytSort = { col, rev: ytSort.col === col && !ytSort.rev };
  const key = (v) => col === "views" ? v.views
    : col === "duration" ? durSeconds(v.duration)
    : String(v[col] ?? "").toLowerCase();
  ytVideos.sort((a, b) =>
    (key(a) > key(b) ? 1 : key(a) < key(b) ? -1 : 0) * (ytSort.rev ? -1 : 1));
  renderYt();
}
function durSeconds(t) {
  return String(t || "").split(":").reduce((acc, p) => acc * 60 + (parseInt(p) || 0), 0);
}
function renderYt() {
  const tbody = el("yt-table").querySelector("tbody");
  tbody.innerHTML = "";
  for (const v of ytVideos) {
    const tr = document.createElement("tr");
    if (ytChecked.has(v.id)) tr.classList.add("checked");
    const stClass = ["failed", "rejected"].includes(v.upload_status) ? "st-err"
      : v.privacy === "public" ? "st-ok" : "";
    tr.innerHTML = `
      <td><input type="checkbox" ${ytChecked.has(v.id) ? "checked" : ""}></td>
      <td>${esc(v.published)}</td>
      <td>${esc(v.title)}</td>
      <td>${esc(v.duration)}</td>
      <td>${esc(v.privacy)}</td>
      <td>${v.views.toLocaleString()}</td>
      <td class="${stClass}">${esc(v.upload_status)}</td>`;
    tr.querySelector("input").addEventListener("click", (e) => {
      e.stopPropagation();
      if (e.target.checked) ytChecked.add(v.id); else ytChecked.delete(v.id);
      tr.classList.toggle("checked", e.target.checked);
    });
    tr.addEventListener("click", () => openYtEditor(v.id));
    tr.addEventListener("dblclick", () => window.open("https://youtu.be/" + v.id));
    tbody.appendChild(tr);
  }
  fillSelect(el("yt-bulk-playlist"), playlistsCache.map(p => p.title),
             el("yt-bulk-playlist").value);
  fillSelect(el("yt-add-playlist"), playlistsCache.map(p => p.title),
             el("yt-add-playlist").value);
}
function ytCheckAll(on) {
  ytChecked = on ? new Set(ytVideos.map(v => v.id)) : new Set();
  renderYt();
}
async function ytBulk(action) {
  if (!ytChecked.size) { toast("Nothing checked."); return; }
  let value = "";
  if (action === "playlist") {
    const pl = playlistsCache.find(p => p.title === el("yt-bulk-playlist").value);
    if (!pl) { toast("Pick a playlist first."); return; }
    value = pl.id;
  }
  if (action === "privacy") value = el("yt-bulk-privacy").value;
  if (action === "delete" && !confirm(
      `PERMANENTLY delete ${ytChecked.size} video(s) from YouTube?\n` +
      `This cannot be undone!`)) return;
  const res = await api("/api/yt/bulk",
    { account_id: browseAccountId, action, keys: [...ytChecked], value });
  toast(`Applied to ${res.ok}/${ytChecked.size} video(s).`, true);
  loadYt();
}
async function openYtEditor(id) {
  try {
    const v = await api(`/api/yt/video/${encodeURIComponent(id)}?account_id=${encodeURIComponent(browseAccountId)}`,
                        null, "GET");
    ytEditorId = id;
    el("yt-ed-title").value = v.title;
    el("yt-ed-tags").value = (v.tags || []).join(", ");
    el("yt-ed-privacy").value = v.privacy;
    const cat = el("yt-ed-category");
    if (![...cat.options].some(o => o.value === String(v.category_id))) {
      const o = document.createElement("option");
      o.value = String(v.category_id);
      o.textContent = "Category " + v.category_id;
      cat.appendChild(o);
    }
    cat.value = String(v.category_id);
    el("yt-ed-desc").value = v.description;
    el("yt-title-count").textContent = `${v.title.length}/100`;
    ytMemberships = [];
    el("yt-memberships").innerHTML = `<span class="muted">(press ⟳ Check)</span>`;
    el("yt-editor").classList.remove("hidden");
  } catch (e) { /* toast already shown */ }
}
function closeYtEditor() { ytEditorId = null; el("yt-editor").classList.add("hidden"); }
el("yt-ed-title").addEventListener("input", () =>
  el("yt-title-count").textContent = `${el("yt-ed-title").value.length}/100`);
async function saveYtEditor() {
  if (!ytEditorId) return;
  await api(`/api/yt/video/${encodeURIComponent(ytEditorId)}?account_id=${encodeURIComponent(browseAccountId)}`, {
    title: el("yt-ed-title").value,
    tags: el("yt-ed-tags").value,
    privacy: el("yt-ed-privacy").value,
    category_id: el("yt-ed-category").value,
    description: el("yt-ed-desc").value,
  }, "PATCH");
  toast("Saved to YouTube.", true);
  loadYt();
}
async function ytCheckMemberships() {
  if (!ytEditorId) return;
  el("yt-memberships").innerHTML = `<span class="muted">checking…</span>`;
  const data = await api(
    `/api/yt/video/${encodeURIComponent(ytEditorId)}/playlists?account_id=${encodeURIComponent(browseAccountId)}`,
    null, "GET");
  ytMemberships = data.playlists || [];
  const box = el("yt-memberships");
  box.innerHTML = ytMemberships.length ? ""
    : `<span class="muted">(not in any playlist)</span>`;
  for (const m of ytMemberships) {
    const row = document.createElement("div");
    row.className = "pl-row";
    row.innerHTML = `<span>${esc(m.title)}</span>
      <button class="btn btn-ghost sm" title="Remove from playlist">✕</button>`;
    row.querySelector("button").addEventListener("click", async () => {
      await api("/api/yt/playlist_item/remove",
                { account_id: browseAccountId, item_id: m.item_id });
      ytCheckMemberships();
    });
    box.appendChild(row);
  }
}
async function ytAddToPlaylist() {
  if (!ytEditorId) return;
  const pl = playlistsCache.find(p => p.title === el("yt-add-playlist").value);
  if (!pl) { toast("Pick a playlist first."); return; }
  await api(`/api/yt/video/${encodeURIComponent(ytEditorId)}/playlists?account_id=${encodeURIComponent(browseAccountId)}`,
            { playlist_id: pl.id });
  toast("Added to playlist.", true);
  ytCheckMemberships();
}

/* --------------------------------------------------------- secret file */
el("secret-file").addEventListener("change", () => {
  const file = el("secret-file").files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = async () => {
    try {
      const res = await api("/api/auth/secret", { content: reader.result });
      el("secret-status").textContent = "Imported client " +
        (res.client_id || "").slice(0, 18) + "…";
      toast("OAuth client imported.", true);
    } catch (e) { /* toast shown */ }
    el("secret-file").value = "";
  };
  reader.readAsText(file);
});

/* ------------------------------------------------------------- auth */
function renderAuth() {
  const a = S.auth;
  const chip = el("channel-chip");
  const status = el("auth-status");
  if (a.status === "signed_in") {
    chip.textContent = "▶ " + ((S.accounts || []).length) + " account(s) connected";
    chip.className = "chip";
    status.textContent = "Connected";
    status.className = "chip";
    el("device-code-box").classList.add("hidden");
  } else if (a.status === "pending_browser") {
    chip.textContent = "check your browser…";
    chip.className = "chip chip-muted";
    status.textContent = "Finish signing in in the browser window";
    status.className = "chip chip-muted";
    el("device-code-box").classList.add("hidden");
  } else if (a.status === "pending") {
    chip.textContent = "waiting for code…";
    chip.className = "chip chip-muted";
    status.textContent = "Waiting for you to enter the code";
    status.className = "chip chip-muted";
    el("device-code").textContent = a.user_code || "";
    el("device-url").href = a.verification_url || "https://www.google.com/device";
    el("device-code-box").classList.remove("hidden");
  } else {
    chip.textContent = "Not connected";
    chip.className = "chip chip-muted";
    status.textContent = a.detail ? "Error: " + a.detail : "Not connected";
    status.className = "chip chip-muted";
    el("device-code-box").classList.add("hidden");
  }
}
function renderAccountsTable() {
  const tbody = el("accounts-table").querySelector("tbody");
  tbody.innerHTML = "";
  for (const a of (S.accounts || [])) {
    const usedBy = queueOrder().map(id => S.queues[id])
      .filter(q => q.account_id === a.id).map(q => q.name);
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${esc(a.title)}</td>
      <td class="muted">${usedBy.length ? esc(usedBy.join(", ")) : "—"}</td>
      <td><button class="btn btn-ghost sm" data-rm>Remove</button></td>`;
    tr.querySelector("[data-rm]").addEventListener("click", async () => {
      if (!confirm(`Remove the saved sign-in for "${a.title}"? Queues using it ` +
                   `will show "not signed in" until you pick another account.`)) return;
      await api("/api/auth/remove", { id: a.id });
    });
    tbody.appendChild(tr);
  }
}
async function connectYouTube() {
  const id = el("client-id").value.trim(), secret = el("client-secret").value.trim();
  if (id || secret) {
    await api("/api/settings", { client_id: id, client_secret: secret });
  }
  await api("/api/auth/start");
}

/* ---------------------------------------------------------- settings */
function renderSettings() {
  if (["client-id", "client-secret", "set-lsdvr-url", "set-lsdvr-pass"]
      .includes(document.activeElement?.id)) return;
  el("set-lsdvr-url").value = S.cfg.lsdvr_url || "";
  el("set-lsdvr-pass").placeholder = S.has_lsdvr_password
    ? "saved — type to replace" : "panel password (empty if none)";
  if (!el("client-id").value && S.cfg.client_id) el("client-id").value = S.cfg.client_id;
  if (document.activeElement !== el("set-queue-layout")) {
    el("set-queue-layout").value = S.cfg.queue_ui_layout || "switcher";
  }
}
function saveSettings() {
  api("/api/settings", {
    lsdvr_url: el("set-lsdvr-url").value.trim(),
  });
}
async function saveLsdvrPassword() {
  const pass = el("set-lsdvr-pass").value;
  if (!pass) return;
  await api("/api/settings", { lsdvr_password: pass });
  el("set-lsdvr-pass").value = "";
  toast("LiveStreamDVR password saved", true);
}
async function testLsdvr() {
  const status = el("lsdvr-status");
  status.textContent = "testing…";
  try {
    const data = await api("/api/lsdvr/test", {
      url: el("set-lsdvr-url").value.trim(),
      password: el("set-lsdvr-pass").value,
    });
    status.textContent = "✓ " + data.message;
  } catch (e) {
    status.textContent = "✗ not connected";
  }
}

/* ---------------------------------------------------------- log bar */
function toggleLog() { el("logbar").classList.toggle("open"); }
async function pollEvents() {
  try {
    const resp = await fetch(`/api/events?since=${lastSeq}`);
    if (resp.status === 401) return;
    const data = await resp.json();
    if (data.events && data.events.length) {
      const box = el("log-lines");
      for (const e of data.events) {
        lastSeq = Math.max(lastSeq, e.seq);
        const line = document.createElement("div");
        line.textContent = `[${e.time}] ${e.text}`;
        box.appendChild(line);
        while (box.childElementCount > 400) box.removeChild(box.firstChild);
      }
      box.scrollTop = box.scrollHeight;
      el("log-last").textContent = data.events[data.events.length - 1].text.slice(0, 120);
    }
  } catch (e) { /* server briefly unreachable */ }
}

/* ------------------------------------------------------------ poller */
async function refresh() {
  let resp;
  try {
    resp = await fetch("/api/state");
    if (resp.status === 401) { showLogin(true); return; }
    S = await resp.json();
  } catch (e) { return; }
  el("version").textContent = "v" + S.version;
  renderAuth();
  renderAccountsTable();
  renderDesktop();
  renderSettings();
  renderQueuePicker();
  renderQueuesTable();
  renderAccountSelectors();
  if (!playlistsLoaded && browseAccountId) loadPlaylists();
  const activeView = document.querySelector(".nav-btn.active")?.dataset.view || "videos";
  el("no-queues-notice").classList.toggle(
    "hidden", !(QUEUE_SCOPED_VIEWS.has(activeView) && queueOrder().length === 0));
  renderScan();
  renderOps();
  renderCooldown();
  renderAutomationStatus();
  const q = activeQueue();
  const name = q ? "— " + q.name : "";
  el("videos-queue-name").textContent = name;
  el("queue-queue-name").textContent = name;
  el("auto-queue-name").textContent = name;
  const vodsJson = q ? JSON.stringify(q.vods) : "";
  if (vodsJson !== lastVodsJson) { lastVodsJson = vodsJson; renderVideos(); }
  const queueJson = q ? JSON.stringify(q.queue) : "";
  if (queueJson !== lastQueueJson) { lastQueueJson = queueJson; renderQueue(); }
  const plJson = JSON.stringify(q ? [q.playlists, q.settings.playlist_mode] : []);
  if (plJson !== lastPlaylistsJson) { lastPlaylistsJson = plJson; renderPlaylists(); }
}

refresh();
pollEvents();
setInterval(refresh, 2000);
setInterval(pollEvents, 2000);

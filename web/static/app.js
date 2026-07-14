"use strict";

let S = null;                 // last /api/state snapshot
let checked = new Set();      // checked vod keys
let lastSeq = 0;              // event log cursor
let editorKey = null;
let lastVodsJson = "", lastQueueJson = "", lastPlaylistsJson = "";

const el = (id) => document.getElementById(id);

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
  const opts = { method: method || (body !== null ? "POST" : "POST"),
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
document.querySelectorAll(".nav-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".nav-btn").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    document.querySelectorAll(".view").forEach(v => v.classList.add("hidden"));
    const view = el("view-" + btn.dataset.view);
    view.classList.remove("hidden", "anim");
    void view.offsetWidth;          // restart the entry animation
    view.classList.add("anim");
  });
});

/* ---------------------------------------------------------- desktop bits */
function renderDesktop() {
  const isDesktop = !!S.desktop;
  el("browser-signin").classList.toggle("hidden", !isDesktop);
  el("desktop-settings").classList.toggle("hidden", !isDesktop);
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
async function applyUpdate() {
  if (!confirm(`Download v${S.update.version} and restart now?`)) return;
  try {
    await api("/api/update/apply");
    toast("Updating — the app will restart itself…", true);
  } catch (e) { /* toast shown */ }
}

/* ------------------------------------------------------------ videos */
let vodsSort = { col: "", rev: false };
function sortVods(col) {
  vodsSort = { col, rev: vodsSort.col === col && !vodsSort.rev };
  lastVodsJson = "";      // force re-render
  renderVideos();
}
function vodsSorted() {
  if (!vodsSort.col) return S.vods;
  const c = vodsSort.col;
  const key = (v) => (c === "size" || c === "duration" || c === "chapters")
    ? (v[c] || 0) : String(v[c] ?? "").toLowerCase();
  return [...S.vods].sort((a, b) =>
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
  checked = on ? new Set(S.vods.map(v => v.key)) : new Set();
  renderVideos();
}
async function doScan() {
  await api("/api/scan", { folder: el("folder-input").value || null });
}
async function bulk(action, value = "", confirmFirst = false) {
  if (!checked.size) { toast("Nothing checked — tick some rows first."); return; }
  if (confirmFirst && !confirm(
      `${action === "delete_local" ? "PERMANENTLY delete local files of" : "Apply to"} ` +
      `${checked.size} checked video(s)?`)) return;
  await api("/api/bulk", { action, keys: [...checked], value });
}

/* ------------------------------------------------------------ editor */
function playlistChoices() {
  return ["(default)", "(none)", ...S.playlists.map(p => p.title)];
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
  const v = S.vods.find(x => x.key === key);
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
  await api(`/api/meta/${encodeURIComponent(editorKey)}`, {
    title: el("ed-title").value,
    tags: el("ed-tags").value,
    privacy: el("ed-privacy").value,
    playlist_choice: el("ed-playlist").value,
    description: el("ed-desc").value,
  }, "PATCH");
  if (andQueue) await api("/api/bulk", { action: "queue", keys: [editorKey], value: "" });
  toast(andQueue ? "Saved and queued." : "Saved.", true);
  if (andQueue) closeEditor();
}
async function resetEditor() {
  if (!editorKey) return;
  await api("/api/bulk", { action: "reset_meta", keys: [editorKey], value: "" });
  setTimeout(() => openEditor(editorKey), 300);
}

/* ------------------------------------------------------------- queue */
async function startUploads() {
  if (S && S.cooldown) {
    if (!confirm(`Upload cooldown is active until ${S.cooldown} ` +
                 `(${S.cooldown_reason}). Uploads resume automatically then.\n\n` +
                 `Force-start now anyway?`)) return;
    await api("/api/queue/start", { force: true });
    return;
  }
  await api("/api/queue/start", {});
}
function renderCooldown() {
  const banner = el("cooldown-banner");
  if (S.cooldown) {
    el("cooldown-until").textContent = S.cooldown;
    el("cooldown-reason").textContent = S.cooldown_reason || "limit";
    banner.classList.remove("hidden");
  } else {
    banner.classList.add("hidden");
  }
  el("uploads-24h").textContent = S.uploads_last_24h ?? 0;
}
function renderQueue() {
  const box = el("queue-list");
  box.innerHTML = "";
  if (!S.queue.length) {
    box.innerHTML = `<div class="empty">Queue is empty — add videos from the Videos section.</div>`;
  }
  S.queue.forEach((q, idx) => {
    const div = document.createElement("div");
    div.className = "q-item";
    div.innerHTML = `
      <div>${idx + 1}</div>
      <div style="min-width:0">
        <div class="q-title">${esc(q.title)}</div>
        <div class="q-detail">${fmtSize(q.size)} · ${esc(q.privacy)} · ${esc(q.detail || "")}</div>
      </div>
      <span class="q-status ${q.status}">${q.status}${q.status === "uploading" ? " " + q.progress + "%" : ""}</span>
      <div class="q-arrows">
        <button class="btn btn-ghost sm" data-d="-1">▲</button>
        <button class="btn btn-ghost sm" data-d="1">▼</button>
      </div>
      <button class="btn btn-ghost sm" data-rm>✕</button>
      <div class="q-progress"><div style="width:${q.progress || 0}%"></div></div>`;
    div.querySelectorAll("[data-d]").forEach(b => b.addEventListener("click", () =>
      api("/api/queue/move", { key: q.key, delta: parseInt(b.dataset.d) })));
    div.querySelector("[data-rm]").addEventListener("click", () =>
      api("/api/queue/remove", { keys: [q.key] }));
    box.appendChild(div);
  });
  const pending = S.queue.filter(q => q.status === "queued").length;
  const badge = el("queue-badge");
  badge.textContent = pending;
  badge.classList.toggle("hidden", pending === 0);
}

/* -------------------------------------------------------- automation */
function renderAutomation() {
  if (document.activeElement && document.activeElement.id === "auto-interval") return;
  el("auto-scan").checked = !!S.cfg.auto_scan;
  el("auto-interval").value = S.cfg.auto_scan_interval_min;
  el("auto-queue").checked = !!S.cfg.auto_queue;
  el("auto-start").checked = !!S.cfg.auto_start;
  el("auto-finalized").checked = !!S.cfg.auto_only_finalized;
  el("auto-status").textContent = S.cfg.auto_scan ? S.auto_status : "off";
}
function saveAutomation() {
  api("/api/settings", {
    auto_scan: el("auto-scan").checked,
    auto_scan_interval_min: parseInt(el("auto-interval").value) || 10,
    auto_queue: el("auto-queue").checked,
    auto_start: el("auto-start").checked,
    auto_only_finalized: el("auto-finalized").checked,
  });
}

/* --------------------------------------------------------- playlists */
function renderPlaylists() {
  const tbody = el("playlists-table").querySelector("tbody");
  tbody.innerHTML = "";
  for (const p of S.playlists) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${esc(p.title)}</td><td>${p.count}</td>
      <td>${esc(p.privacy)}</td><td class="muted">${esc(p.id)}</td>`;
    tbody.appendChild(tr);
  }
  fillSelect(el("bulk-playlist"), playlistChoices(), el("bulk-playlist").value);
  const fixedSel = el("pl-fixed");
  if (document.activeElement !== fixedSel) {
    fillSelect(fixedSel, S.playlists.map(p => p.title), S.cfg.playlist_fixed_title);
  }
  const radio = document.querySelector(`input[name="plmode"][value="${S.cfg.playlist_mode}"]`);
  if (radio && !radio.checked) radio.checked = true;
  if (document.activeElement !== el("pl-template")) {
    el("pl-template").value = S.cfg.playlist_template || "";
  }
}
function savePlaylistRule() {
  const mode = document.querySelector('input[name="plmode"]:checked')?.value || "none";
  const fixedTitle = el("pl-fixed").value;
  const fixed = S.playlists.find(p => p.title === fixedTitle);
  api("/api/settings", {
    playlist_mode: mode,
    playlist_fixed_title: fixedTitle,
    playlist_fixed_id: fixed ? fixed.id : S.cfg.playlist_fixed_id,
    playlist_template: el("pl-template").value,
  });
}
async function createPlaylist() {
  const title = el("new-pl-title").value.trim();
  if (!title) return;
  el("new-pl-title").value = "";
  await api("/api/playlists/create", { title, privacy: el("new-pl-privacy").value });
}

/* --------------------------------------------------------- my youtube */
let ytVideos = [];
let ytChecked = new Set();
let ytSort = { col: "", rev: false };
let ytEditorId = null;
let ytMemberships = [];

async function loadYt() {
  el("yt-count").textContent = "loading…";
  try {
    const data = await api("/api/yt/videos", null, "GET");
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
  fillSelect(el("yt-bulk-playlist"), S.playlists.map(p => p.title),
             el("yt-bulk-playlist").value);
  fillSelect(el("yt-add-playlist"), S.playlists.map(p => p.title),
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
    const pl = S.playlists.find(p => p.title === el("yt-bulk-playlist").value);
    if (!pl) { toast("Pick a playlist first."); return; }
    value = pl.id;
  }
  if (action === "privacy") value = el("yt-bulk-privacy").value;
  if (action === "delete" && !confirm(
      `PERMANENTLY delete ${ytChecked.size} video(s) from YouTube?\n` +
      `This cannot be undone!`)) return;
  const res = await api("/api/yt/bulk", { action, keys: [...ytChecked], value });
  toast(`Applied to ${res.ok}/${ytChecked.size} video(s).`, true);
  loadYt();
}
async function openYtEditor(id) {
  try {
    const v = await api(`/api/yt/video/${encodeURIComponent(id)}`, null, "GET");
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
  await api(`/api/yt/video/${encodeURIComponent(ytEditorId)}`, {
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
  const data = await api(`/api/yt/video/${encodeURIComponent(ytEditorId)}/playlists`,
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
      await api("/api/yt/playlist_item/remove", { item_id: m.item_id });
      ytCheckMemberships();
    });
    box.appendChild(row);
  }
}
async function ytAddToPlaylist() {
  if (!ytEditorId) return;
  const pl = S.playlists.find(p => p.title === el("yt-add-playlist").value);
  if (!pl) { toast("Pick a playlist first."); return; }
  await api(`/api/yt/video/${encodeURIComponent(ytEditorId)}/playlists`,
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
    chip.textContent = "▶ " + (a.channel || "connected");
    chip.className = "chip";
    status.textContent = "Connected" + (a.channel ? ": " + a.channel : "");
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
function renderAccounts() {
  const sel = el("account-select");
  if (document.activeElement === sel) return;
  sel.innerHTML = "";
  for (const a of (S.accounts || [])) {
    const o = document.createElement("option");
    o.value = a.id;
    o.textContent = a.title;
    sel.appendChild(o);
  }
  if (S.active_account) sel.value = S.active_account;
}
async function switchAccount() {
  await api("/api/auth/switch", { id: el("account-select").value });
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
  if (["set-template", "client-id", "client-secret", "folder-input", "set-daily",
       "set-desc-template", "set-speed", "set-cooldown-h", "set-extra-tags"]
      .includes(document.activeElement?.id)) return;
  el("set-daily").value = S.cfg.daily_upload_limit ?? 0;
  el("set-desc-template").value = S.cfg.description_template || "";
  el("set-speed").value = S.cfg.upload_speed_limit ?? 0;
  el("set-cooldown-h").value = S.cfg.cooldown_hours ?? 24.5;
  el("set-verify").checked = S.cfg.verify_uploads !== false;
  el("set-extra-tags").value = S.cfg.extra_tags || "";
  el("set-privacy").value = S.cfg.privacy;
  el("set-category").value = String(S.cfg.category_id || "20");
  el("set-template").value = S.cfg.title_template;
  el("set-notify").checked = !!S.cfg.notify_subscribers;
  el("set-kids").checked = !!S.cfg.made_for_kids;
  el("set-after").value = S.cfg.after_upload || "keep";
  if (document.activeElement !== el("folder-input")) {
    el("folder-input").value = S.cfg.vod_folder || "";
  }
  if (!el("client-id").value && S.cfg.client_id) el("client-id").value = S.cfg.client_id;
}
function saveSettings() {
  api("/api/settings", {
    privacy: el("set-privacy").value,
    category_id: el("set-category").value,
    title_template: el("set-template").value,
    notify_subscribers: el("set-notify").checked,
    made_for_kids: el("set-kids").checked,
    after_upload: el("set-after").value,
    daily_upload_limit: parseInt(el("set-daily").value) || 0,
    description_template: el("set-desc-template").value,
    upload_speed_limit: parseFloat(el("set-speed").value) || 0,
    cooldown_hours: parseFloat(el("set-cooldown-h").value) || 24.5,
    verify_uploads: el("set-verify").checked,
    extra_tags: el("set-extra-tags").value,
  });
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
  renderAccounts();
  renderDesktop();
  renderCooldown();
  renderAutomation();
  renderSettings();
  const vodsJson = JSON.stringify(S.vods);
  if (vodsJson !== lastVodsJson) { lastVodsJson = vodsJson; renderVideos(); }
  const queueJson = JSON.stringify(S.queue);
  if (queueJson !== lastQueueJson) { lastQueueJson = queueJson; renderQueue(); }
  const plJson = JSON.stringify([S.playlists, S.cfg.playlist_mode,
                                 S.cfg.playlist_fixed_title, S.cfg.playlist_template]);
  if (plJson !== lastPlaylistsJson) { lastPlaylistsJson = plJson; renderPlaylists(); }
}

refresh();
pollEvents();
setInterval(refresh, 2000);
setInterval(pollEvents, 2000);

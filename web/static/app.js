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
  if (!resp.ok) {
    toast(data.error || data.detail || `Request failed (${resp.status})`);
    throw new Error("api error");
  }
  refresh();
  return data;
}

/* --------------------------------------------------------------- nav */
document.querySelectorAll(".nav-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".nav-btn").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    document.querySelectorAll(".view").forEach(v => v.classList.add("hidden"));
    el("view-" + btn.dataset.view).classList.remove("hidden");
  });
});

/* ------------------------------------------------------------ videos */
function statusClass(st) {
  if (st.startsWith("uploaded")) return "st-ok";
  if (st.includes("failed") || st.includes("no video")) return "st-err";
  if (st === "uploading" || st === "verifying" || st === "queued") return "st-info";
  return "";
}
function renderVideos() {
  const tbody = el("videos-table").querySelector("tbody");
  tbody.innerHTML = "";
  for (const v of S.vods) {
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
async function connectYouTube() {
  const id = el("client-id").value.trim(), secret = el("client-secret").value.trim();
  if (id || secret) {
    await api("/api/settings", { client_id: id, client_secret: secret });
  }
  await api("/api/auth/start");
}

/* ---------------------------------------------------------- settings */
function renderSettings() {
  if (["set-template", "client-id", "client-secret", "folder-input", "set-daily"]
      .includes(document.activeElement?.id)) return;
  el("set-daily").value = S.cfg.daily_upload_limit ?? 0;
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
  });
}

/* ---------------------------------------------------------- log bar */
function toggleLog() { el("logbar").classList.toggle("open"); }
async function pollEvents() {
  try {
    const resp = await fetch(`/api/events?since=${lastSeq}`);
    const data = await resp.json();
    if (data.events.length) {
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
  try {
    const resp = await fetch("/api/state");
    S = await resp.json();
  } catch (e) { return; }
  el("version").textContent = "v" + S.version;
  renderAuth();
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

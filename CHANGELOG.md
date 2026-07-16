# Changelog

Release versions come from the `VERSION` file; each release's notes are the
matching section of this file. Bump `VERSION` and add a section here to cut
a new release.

## 2.5.0 — 2026-07-16

### ℹ️ About page in Studio / web
- The modern interface now has an About section: app info, author, GitHub
  and releases links, and (on desktop) a check-for-updates button.
- Full **release notes history** is shown right on the About page — the
  changelog ships inside the exe, the .app and the Docker image and is
  rendered in-app.
- Historical GitHub releases (the whole v1.0.x series) got retroactive
  release notes generated from their commit history, so the releases page
  now tells a coherent story.

## 2.4.0 — 2026-07-16

### 🎞 Video preview
- Desktop: "▶ Play video" opens the selected VOD in your default player
  (plus an explicit "📂 Open folder" button next to it).
- Web/Studio: "▶ Preview" in the metadata editor plays the video in an
  in-page pop-out player (seeking supported; Esc or backdrop click
  closes). Raw `.ts` captures get a heads-up that browsers may not play
  them.

### 🛠 Updater fix
- Fixed the "Failed to load Python DLL …\\_MEIxxxxxx\\python313.dll" error
  after auto-updating. The swap script now waits for **all** app processes
  (the onefile bootloader included), gives antivirus a moment to scan the
  new exe, and retries the launch up to three times if the first boot gets
  interrupted.

## 2.3.0 — 2026-07-16

### 🗑 Deletion overhaul
- Manual cleanup now removes the **whole VOD folder** — video, chapters,
  metadata, everything — not just the video file (desktop: Recycle Bin,
  server: permanent, both clearly labeled). The automatic after-upload
  setting still offers video-only or whole-folder.
- Deletion runs in the background with live progress ("Recycling 3/12: …")
  in the same status line/chip as scanning, and finishes with an explicit
  result ("Recycled 12 of 12 folder(s)").
- Fixed: deleted VODs stayed on the list until a manual rescan. Removed
  folders now disappear immediately (including the automatic
  delete-after-upload path) and a background rescan runs after bulk
  deletion to keep everything in sync.

## 2.2.0 — 2026-07-16

### 🔍 Scan feedback
- Scanning now shows live progress everywhere instead of silently freezing:
  the desktop scans in a background thread with a status line under the
  folder bar ("⏳ Scanning… 12/48: folder"), and the web/Studio UI shows a
  status chip next to the Scan button. Both end with a clear result —
  "✅ Found N VOD folder(s)", "⚠ No VOD folders found" (with a hint that
  you should pick the folder containing the per-stream subfolders), or the
  error that occurred. The Scan button is disabled while a scan runs, and
  automation waits for the background scan to finish before queueing.

## 2.1.0 — 2026-07-15

### 📱 Mobile
- The web/Docker interface is now fully responsive: the sidebar becomes a
  bottom navigation bar with icon tabs, tables scroll horizontally instead
  of crushing, queue items reflow, editors open full-screen, and the
  activity log docks above the navigation. Touch targets enlarged and
  double-tap zoom disabled on controls.

### 🐳 Docker
- The web version now shows the real release version (read from the
  `VERSION` file) instead of `0.0.0-dev` on rsync-deployed servers.

## 2.0.0 — 2026-07-15

The "Studio" milestone — a new interface, a much faster uploader, and a
grown-up release process. (Versions 1.0.x were incremental development
builds.)

### 🚀 Upload speed
- Fixed the long-standing ~11 MB/s ceiling. Root cause: the HTTP stack
  streamed file bodies in 16 KB blocks, each a full Python/TLS round trip
  (benchmark: 12.3 MB/s vs 40+ MB/s for the same connection). Uploads now
  send 1 MB blocks — **3–4× faster** on fast connections, with the same
  resumable safety.

### 🖥 Studio interface (desktop)
- The desktop app now opens the modern web interface in a native window
  (WebView2/WebKit): sidebar navigation, cards, gradients, animated views
  and buttons. The classic Tkinter window is still available
  (Settings → Interface, or `--classic`), and is used automatically when
  WebView2 is missing.
- Desktop-only powers inside Studio: browser OAuth sign-in (works with a
  normal "Desktop app" client), an update banner wired to the built-in
  self-updater, and the interface switcher.
- Classic window improvements: scrollable Settings, a Modern/Classic
  typography-and-spacing switch, Midnight/dark/light themes, tidier
  grouped toolbars with icons, and table sorting in My YouTube.

### 🌐 Web / Docker version
- In-page login screen (session cookie) replaces the Basic-auth popup;
  password comes from `WEB_PASSWORD` in `.env`.
- Full **My YouTube** manager: sortable channel video list, bulk
  add-to-playlist / privacy / delete, and a per-video editor (title, tags,
  privacy, category, description, playlist membership) saved to YouTube.
- `client_secret.json` can be uploaded from the browser; a clear message
  explains that device sign-in needs a "TVs and Limited Input devices"
  client, not a Desktop one.
- "Open on YouTube" buttons on finished queue items (desktop queue too,
  plus double-click).

### 🔁 Reliability
- `uploadLimitExceeded` (HTTP 400) is recognized correctly: the queue
  pauses with a configurable cooldown (default 24.5 h) and resumes
  automatically; a daily upload limit can stop before YouTube errors.
- Retry-failed button, post-upload verification, playlist auto-add after
  verification, automation (folder watching, auto-queue, auto-start).

### 📦 Platform & updates
- Self-updater with release notes and progress: downloads the new build,
  swaps the whole file (exe on Windows, .app via dmg on macOS) and
  restarts.
- macOS bundle switched to onedir with a proper `.icns` — single Dock
  icon, correct branding.
- First-run Terms of Use dialog (stored in `.accepted`), MIT license,
  app icon, splash screen, About tab.
- Hardened Tailscale deploy workflow with a hardcoded target path and
  multiple guards after the rsync incident.

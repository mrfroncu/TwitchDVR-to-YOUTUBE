# Changelog

Release versions come from the `VERSION` file; each release's notes are the
matching section of this file. Bump `VERSION` and add a section here to cut
a new release.

## 2.6.3 — 2026-09-20

### 📊 Total size in the Videos summary
- The summary line under the filters now also sums the Size column — for
  what's currently shown, and for whatever you have checked.

### ⚙ Settings tab reorganized, and fixed to actually save
- **Every setting now saves the instant you change it** — no more separate
  "Save settings" button to remember. This also fixes a real bug: the new
  "delete permanently if the Recycle Bin fails" checkbox looked checked in
  the UI but silently never took effect unless you happened to click Save
  afterward, since it had no auto-save wired up. It (and everything else on
  this tab) now applies immediately.
- The title & description upload templates — previously the single biggest
  block on the page — moved into their own "✏ Edit title & description
  templates…" popup, so the main Settings page is no longer dominated by a
  multi-line text box.
- Sections got icons (🎨 Appearance, 🔑 YouTube account, ⬆ Uploads,
  ⚙ Behavior) and tighter grouping to make the page easier to scan.

## 2.6.2 — 2026-09-18

### 🖱️ Right-click menu on the Videos tab
- Right-click (or secondary-click on macOS) a video for a quick menu: play,
  open folder, add to queue, reset metadata, and — once it's been uploaded —
  open on YouTube, verify, reset upload state, and recycle local files.

### 🐛 "Recycle local files" failing silently
- If moving a VOD folder to the Recycle Bin failed (common on some network
  drives — macOS's Send2Trash doesn't always support them), the app quietly
  logged one easy-to-miss line, showed a green "✅ Recycled 0 of N" status
  anyway, and then just rescanned the folder — which looked exactly like
  "nothing happened, it just searched again." It now shows a clear ❌ status,
  pops up a warning pointing at the log, and skips the pointless rescan when
  nothing was actually removed.
- New opt-in Settings toggle: "If the Recycle Bin isn't available (e.g. some
  network drives), delete permanently instead of doing nothing." Off by
  default — recycling still just fails safely unless you turn this on. The
  confirmation dialog reminds you it's enabled every time you use Recycle.

## 2.6.1 — 2026-09-18

### 🐛 Fixes for the checkboxes, shortcuts and notifications from 2.6.0
- **Checkboxes were hard to see** — swapped the thin ☑/☐ glyphs for bold ✅/⬜
  ones and widened the checkbox column.
- **Click-and-drag checkbox selection didn't work** — the drag required the
  cursor to stay inside the narrow checkbox column the whole time, which any
  normal mouse/trackpad movement broke immediately. Dragging now only cares
  about where the drag started.
- **macOS shortcuts** — Ctrl+A/Ctrl+F now also work as Cmd+A/Cmd+F, and
  removing a queue item now also works on the key macOS actually labels
  "delete" (Tk calls it BackSpace).
- **Date filters** — added a 📅 button next to the From/To fields that opens
  a small calendar popup, instead of typing YYYY-MM-DD by hand.
- **Misleading "queue finished" notification** — it now reports actual
  done/failed counts instead of a blanket "all processed" message, and
  uploads pausing on a quota/daily limit or failing to start now also send
  a notification (previously only a full successful finish did).

## 2.6.0 — 2026-09-18

### 🗑️ Studio removed — classic is now the only desktop interface
- The "Studio" desktop mode (the modern web UI shown in a native pywebview
  window) has been removed. Classic Tkinter is now the only desktop
  interface, so every new feature lands in one place instead of two.
- If your Settings had Interface set to "studio", the app now simply opens
  classic — the old setting is ignored.
- The headless Docker/browser deployment (`docker compose up -d`, port 4091)
  is completely unaffected — it's a separate, still fully supported way to
  run the app on a server without a screen.

### ℹ️ Release notes now show up in the desktop app too
- The About tab renders this very changelog in a scrollable panel, so you
  can see what changed without leaving the app.

### 🔎 Filters and a live summary on the Videos tab
- New filter bar: search by streamer/title, filter by upload status, by
  game, or by a date range — all combinable.
- A summary line under the filters shows "N shown of M total" plus a
  breakdown of ready/queued/uploaded/verified/failed/not-finalized counts.
- Checking a row, then filtering it out of view, no longer drops it from
  bulk actions — it stays checked in the background.

### 💾 The upload queue survives a restart
- Queue order, each item's status, and any metadata you edited before
  queueing are now saved to disk (`queue_state.json`) and restored on the
  next launch. Queue up 300 videos, close the app or reboot mid-batch, and
  it picks back up in the same place. A file that was mid-upload when the
  app closed restarts from the beginning next time — same as a network-drop
  retry already did within a session.

### 🖱️ Click-and-drag checkboxes, plus keyboard shortcuts
- Check or uncheck a whole range of rows by clicking and dragging across
  the checkbox column, on the Videos, Queue and My YouTube tabs.
- New shortcuts: Space toggles the focused row, Ctrl+A checks everything in
  the current list, Delete removes the selected item from the queue, and
  Ctrl+F jumps to the Videos search box.

### 🔍 UI scale for small or high-DPI screens
- Settings → Appearance has a new 75% / 100% / 125% / 150% scale, applied
  instantly without restarting the app.

### ⚡ My YouTube and Playlists load automatically
- Both tabs now fetch their data the first time you visit them in a
  session — no more clicking "Load videos" / "Refresh playlists" first.

### 📊 Quota panel and optional notifications
- Queue & Progress now shows today's YouTube API quota usage and a rough
  ETA for when the current queue will finish, given the ~6 uploads/day cap.
- Optional OS notifications (native on macOS, via a lightweight helper on
  Windows) when the queue finishes or an individual upload fails.

## 2.5.1 — 2026-07-16

### 🛠 Updater — real fix for the python313.dll error
- Root cause found: the PyInstaller bootloader passes `_PYI_*`/`_MEIPASS2`
  environment variables to child processes. The update helper inherited
  them from the closing app, so the freshly started exe believed it was a
  child process and tried to load Python DLLs from the old, already
  deleted `_MEIxxxxxx` directory. The helper now launches with a scrubbed
  environment and additionally clears those variables before restarting
  the app (Windows and macOS).
- Note: the fix takes effect when updating **from** this version onward —
  the update that installs 2.5.1 still runs the old helper once.

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

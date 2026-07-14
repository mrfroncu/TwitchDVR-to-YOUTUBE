# Changelog

Release versions come from the `VERSION` file; each release's notes are the
matching section of this file. Bump `VERSION` and add a section here to cut
a new release.

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

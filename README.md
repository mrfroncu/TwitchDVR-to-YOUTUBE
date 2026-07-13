# TwitchDVR to YouTube

A small Windows desktop app that uploads [LiveStreamDVR](https://github.com/MrBrax/LiveStreamDVR)
Twitch VOD recordings to YouTube — sequentially, with all the stream metadata carried over:

- **Title** from the Twitch stream/VOD title (templated, e.g. `{title} | {streamer} VOD {date}`)
- **Description** with the stream title, streamer link, date, game list and the original Twitch VOD ID
- **YouTube chapters** generated from Twitch title/game changes (rendered from timestamps in the description)
- **Tags** from the game names + streamer name
- **Recording date** set to the real stream date, **category** set to Gaming
- Editable per-video metadata, upload queue with reordering, progress/speed/ETA, resumable
  chunked uploads with automatic retry, and duplicate-upload protection (already-uploaded
  folders are remembered and skipped)
- **Post-upload verification**: after each upload the app asks the YouTube API whether the
  video actually exists and wasn't rejected/failed (shown as *verified* in the list; there is
  also a bulk "Verify on YouTube" action)
- **Checkboxes & bulk actions** in both lists: check rows (or click the ☐ column header for
  all) and bulk add-to-queue, reset metadata, set privacy, verify, remove from queue
- **Optional local cleanup**: after a *verified* upload, move the video file — or the whole
  VOD folder — to the Recycle Bin, automatically (Settings → "After verified upload") or
  manually via the bulk "🗑 Recycle local files" button. Files are never touched unless
  YouTube confirmed the video exists, and they go to the Recycle Bin, not permanent deletion.
- Modern Fluent-inspired UI with dark and light mode (Settings → Appearance), animated
  upload progress, and a theme-matched title bar — drawn natively, so it stays responsive
- **Automation tab**: watch the VOD folder in the background — rescan on an interval,
  auto-queue new ready VODs with generated metadata, and auto-start uploads
- **Playlists tab**: browse/create channel playlists and set a default rule for uploads —
  a fixed playlist, or auto-created by name template (e.g. `{streamer} VODs {year}`);
  per-video override in the editor and as a bulk action. Videos are added right after
  upload verification.

> **Updating from an older version?** The playlist features need an extra Google permission,
> so the app will ask you to **sign in again once**.

## Requirements

- Windows or macOS. Grab a build from the [Releases page](../../releases)
  (no Python needed):
  - `TwitchDVR-to-YouTube.exe` — Windows, single file, just run it
  - `TwitchDVR-to-YouTube-macos-arm64.dmg` — macOS on Apple Silicon
    (Intel Macs: run from source)

  …or run from source on any OS (including Linux) with Python 3.10+
  (Tkinter is included in the default python.org installer).
- A Google account with a YouTube channel

> **Windows:** SmartScreen may warn about the unsigned exe on first run —
> click **More info → Run anyway**.
>
> **macOS:** open the dmg, drag the app to Applications. Because the app is
> not notarized, the first launch will be blocked: right-click the app →
> **Open**, or go to **System Settings → Privacy & Security → Open Anyway**.
> Alternatively run `xattr -cr "/Applications/TwitchDVR-to-YouTube.app"` once.

## Setup

### 1. Install dependencies

```
pip install -r requirements.txt
```

### 2. Create your own Google API credentials (one-time, ~5 minutes)

The app talks to the official YouTube Data API v3 and needs an OAuth client that belongs to *you*:

1. Go to <https://console.cloud.google.com/> and create a project (any name).
2. **APIs & Services → Library** → search **YouTube Data API v3** → **Enable**.
3. **APIs & Services → OAuth consent screen** → External → fill in the app name + your email,
   add yourself as a **test user**.
4. **APIs & Services → Credentials → Create credentials → OAuth client ID** →
   application type **Desktop app** → Create → **Download JSON**.
5. Keep the downloaded `client_secret_….json` somewhere safe.

### 3. Run and sign in

```
python run.py
```

- **Settings** tab → browse to your `client_secret_….json` → **Sign in with Google…**
- Your browser opens; the app listens on `http://127.0.0.1:<port>` for the redirect.
  The Google account chooser in the browser is where you pick the account **and the YouTube
  channel** (brand/secondary channels show up in that list). To switch channels later:
  Sign out → Sign in again.

### 4. Upload

1. **Videos** tab → pick the folder that contains your VOD subfolders → **Scan**.
2. Select a video to review/edit the generated title, description (chapters), tags and privacy.
3. **Add selected to queue** (or **Add ALL new to queue**).
4. **Queue & Progress** tab → **Start uploads**. Uploads run one at a time; you can pause after
   the current file, cancel the current file, reorder or remove pending items.

## Expected folder layout

One subfolder per stream, as produced by LiveStreamDVR:

```
vods/
├─ streamer_2024-10-15T17_03_35Z_43013488392/
│  ├─ streamer_…_43013488392.json                ← metadata (required)
│  ├─ streamer_…_43013488392.mp4  (or .ts)       ← video
│  ├─ streamer_…_43013488392-ffmpeg-chapters.txt ← optional, used for display name
│  └─ …
```

Folders with only a `.ts` capture or `is_finalized: false` are still listed (flagged in the
Status column) and can be uploaded — YouTube accepts MPEG-TS.

## Important YouTube API limitations

- **Quota:** each upload costs **1600 units**; the default daily quota is **10 000 units**,
  i.e. **~6 uploads per day**. The queue detects quota exhaustion and stops; the quota resets
  at **midnight Pacific time**. You can request a quota increase in the Google Cloud console.
- **Unverified apps upload as private-locked:** until your OAuth app passes Google's
  verification/audit, videos uploaded through the API are **locked to Private** regardless of
  the privacy setting. For personal archiving this is usually fine (you can watch them while
  signed in); to publish publicly, either complete the
  [API audit](https://support.google.com/youtube/contact/yt_api_form) or manually flip
  visibility in YouTube Studio after upload.
- While the consent screen is in *Testing* mode, refresh tokens expire after ~7 days —
  you'll just be asked to sign in again.
- **Videos longer than 15 minutes** require a verified YouTube account
  (<https://www.youtube.com/verify>) — otherwise YouTube rejects the video *after* the
  transfer finishes. The app's post-upload verification catches this and marks the video
  as *failed*, so it can be re-queued once your account is verified. If a video is stuck
  showing "uploaded" from an older failed attempt, check it and use **Reset upload state**.

## Upload speed

Uploads stream the whole file as **one continuous request** on a Google resumable-upload
session — the same approach browsers use — instead of the Google Python client's chunked
loop, which is known to cap out around 10 MB/s
([google-api-python-client#625](https://github.com/googleapis/google-api-python-client/issues/625),
[#793](https://github.com/googleapis/google-api-python-client/issues/793)).
If the connection drops, the app asks the session how many bytes were committed and
resumes from there, so nothing restarts from zero. There is nothing to configure.

## Building the exe / releases

Every push to `main` triggers the [Build & Release workflow](.github/workflows/release.yml):
it builds the Windows exe and the macOS `.app`/`.dmg` (Apple Silicon, plus Intel as a
best-effort job) with PyInstaller and publishes a GitHub release tagged `v1.0.<build number>`
with all binaries attached and auto-generated notes.

To build locally (on the OS you're building for):

```
pip install pyinstaller
pyinstaller TwitchDVR-to-YouTube.spec --noconfirm
```

Output: `dist\TwitchDVR-to-YouTube.exe` on Windows, `dist/TwitchDVR-to-YouTube.app` on macOS.

## Where the app stores its data

Settings (`config.json`), the Google token (`token.json`), and the record of what was already
uploaded (`uploads.json`) live in:

- Windows: `%APPDATA%\TwitchDVR-to-YouTube\`
- macOS: `~/Library/Application Support/TwitchDVR-to-YouTube/`
- Linux: `~/.config/TwitchDVR-to-YouTube/`

Delete `uploads.json` if you ever want to re-upload something the app considers done.

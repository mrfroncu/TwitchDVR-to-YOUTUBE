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

## Requirements

- Windows, Python 3.10+ (Tkinter included, which is the default python.org installer)
- A Google account with a YouTube channel

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

## Where the app stores its data

`%APPDATA%\TwitchDVR-to-YouTube\` — settings (`config.json`), the Google token (`token.json`),
and the record of what was already uploaded (`uploads.json`). Delete `uploads.json` if you ever
want to re-upload something the app considers done.

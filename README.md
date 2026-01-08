# Newsreader Pipeline

Local-first, personal-only reading pipeline that captures article lists and content in the browser, then builds a single EPUB issue per Book per day.

## Requirements

- OrbStack (Docker-compatible)
- Chrome or Firefox (for extension)

## Setup (OrbStack)

1. From the repo root, start the stack:

   ```bash
   docker-compose up --build
   ```

2. The API/UI will be available at: `http://localhost:8000`.

Data persists in a named Docker volume at `/data` inside the container.

## Load the Chrome Extension

1. Open `chrome://extensions`.
2. Enable **Developer mode**.
3. Click **Load unpacked** and select the `extension/` folder.

## Load the Firefox Extension (temporary add-on)

1. Open `about:debugging#/runtime/this-firefox`.
2. Click **Load Temporary Add-on**.
3. Select `extension_firefox/manifest.json`.
4. Click the toolbar icon to open the **Newsreader Control Panel** window (it stays open for debugging).

Firefox MV3 note:
- The default manifest is MV2 for compatibility.
- If you want MV3, enable `extensions.manifestV3.enabled` and `extensions.backgroundServiceWorker.enabled` in `about:config`, then load `extension_firefox/manifest_mv3.json`.

## Create a Book

- Visit `http://localhost:8000` and create a Book.
- Note the Book ID from the URL (e.g., `/books/1` -> Book ID `1`).

## Capture a Reading List (Update Book)

1. Navigate to a page that contains links you want captured.
2. Open the extension control panel (toolbar icon).
3. Enter Host URL (default `http://localhost:8000`) and Book ID.
   - If the host runs on another machine, set Host URL to `http://<host-ip>:8000`.
4. Click **Update Book** to send a snapshot list to the host.
   - Optional: enable **Bulk capture snapshot items** to auto-open and ingest each URL.
   - Firefox-only: enable **Use iPhone-style mobile view for WSJ capture** to force a mobile UA for WSJ list extraction and bulk capture.

## Capture an Article (Send Article)

1. Open the article you want to capture.
2. Click **Send Article** in the extension control panel.
   - Readability-based extraction is used; if it fails, the full HTML is sent.
   - Images are inlined as data URLs when possible to preserve charts behind logins.
   - Byline, published time, section, and reading time are shown when available.
   - Firefox-only: the mobile UA toggle captures a mobile-rendered copy of WSJ in a background tab.

## Build Today's Issue

- From the extension popup, click **Build Today's Issue**, or
- From the host UI, click **Build Today's Issue** on the Book page.

The issue EPUB is generated per Book per calendar day and updated with new chapters when new articles arrive (deduped by URL + content hash).

## Download Issue EPUB

- From the Book page, click the **Download EPUB** link.
- Direct download endpoint: `http://localhost:8000/download/{issue_id}.epub`

## Send to Kindle (optional)

The API container includes the `kindle-send` CLI for emailing EPUBs to your Kindle.

1. Initialize the configuration (stores it at `/data/KindleConfig.json`):

   ```bash
   docker compose exec api kindle-send --config /data/KindleConfig.json send https://example.com
   ```

2. Use the **Send to Kindle** button on the Book page, or:

   ```bash
   curl -X POST http://localhost:8000/api/issues/{issue_id}/send
   ```

Optional environment variables:

- `KINDLE_SEND_CONFIG` (default `/data/KindleConfig.json`)
- `KINDLE_SEND_TIMEOUT` (seconds, default `120`)
- `KINDLE_SEND_ARGS` (extra flags passed to `kindle-send`)

## API Quick Checks

```bash
curl http://localhost:8000/api/books
```

## Reading Time + Scene Breaks

- Reading time defaults to 230 WPM. Override with `READING_WPM` in the API container environment.
- Scene breaks are normalized to a visible `* * *` marker when an article contains empty paragraphs, `<hr>`, or `***`.

## Image Compression (optional)

Embedded images are resized and compressed to keep EPUB sizes reasonable.

- `IMAGE_MAX_DIM` (default `1400`): max width/height in pixels
- `IMAGE_JPEG_QUALITY` (default `82`): JPEG re-encode quality (50-95)

## Crosspoint-reader Notes

- Copy the downloaded EPUB to the device storage/SD card.
- If using a catalog flow, you can expose the `/download/*.epub` links in your preferred file browser or OPDS client.

## Repository Layout

```
/extension         Chrome MV3 extension
/extension_firefox Firefox MV2 extension (MV3 optional via manifest_mv3.json)
/services/api      FastAPI application + UI
/services/renderer EPUB build helper
/docker-compose.yml
```

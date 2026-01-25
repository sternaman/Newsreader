# CrossPoint Reader Capability Map (Xteink X4 / ESP32-C3)

This report is derived only from the code in this repo.

## Build and Flash

- Toolchain: PlatformIO with `espressif32@6.12.0`, board `esp32-c3-devkitm-1`, Arduino framework, 16MB flash, partitions from `partitions.csv`.
- Commands:
  - `git submodule update --init --recursive`
  - `pio run`
  - `pio run -t upload`
  - `pio device monitor -b 115200`
- Pre-build step: `scripts/build_html.py` runs via `extra_scripts` in `platformio.ini`.

Evidence:
```ini
; platformio.ini
platform = espressif32 @ 6.12.0
board = esp32-c3-devkitm-1
framework = arduino
board_build.flash_size = 16MB
board_build.partitions = partitions.csv
extra_scripts =
  pre:scripts/build_html.py
```

## Capability Map

| Component | Status | Evidence (paths/functions) |
| --- | --- | --- |
| SD card storage via SdFat | Present | `open-x4-sdk/libs/hardware/SDCardManager/include/SDCardManager.h` (`SdFat sd;`) |
| Internal flash FS (SPIFFS) | Not found in code | `partitions.csv` defines `spiffs`, but no SPIFFS usage in `src/` |
| Cache dir `/.crosspoint` | Present | `src/CrossPointSettings.cpp`, `src/CrossPointState.cpp`, `lib/Epub/Epub.cpp` |
| Local file browser (SD) | Present | `src/activities/home/MyLibraryActivity.cpp` (`loadFiles`) |
| Wi-Fi config UI | Present | `src/activities/network/WifiSelectionActivity.cpp` |
| Wi-Fi AP + captive portal | Present | `src/activities/network/CrossPointWebServerActivity.cpp` (AP + DNS) |
| HTTP/HTTPS client | Present | `src/network/HttpDownloader.cpp`, `lib/KOReaderSync/KOReaderSyncClient.cpp` |
| TLS validation | Partial (insecure) | `WiFiClientSecure::setInsecure()` in `HttpDownloader`, `OtaUpdater`, `KOReaderSyncClient` |
| Web file transfer (HTTP + WebSocket) | Present | `src/network/CrossPointWebServer.cpp` |
| OTA firmware update | Present | `src/network/OtaUpdater.cpp` |
| DNS / mDNS | Present | `src/activities/network/CrossPointWebServerActivity.cpp` (DNSServer + ESPmDNS) |
| NTP time sync | Present (KOReader sync only) | `src/activities/reader/KOReaderSyncActivity.cpp` |
| Calibre Wireless (UDP discovery + TCP transfer) | Present | `src/activities/network/CalibreWirelessActivity.cpp` |
| OPDS browser + download | Present | `src/activities/browser/OpdsBookBrowserActivity.cpp` |
| SSH / Telnet | Not found | no matches in repo |
| Resume downloads | Not found | `HttpDownloader::downloadToFile` truncates existing files |
| Free space API | Not found (hardcoded) | `CalibreWirelessActivity::handleFreeSpace` TODO |
| Formats: EPUB, XTC/XTCH, TXT | Present | `lib/Epub`, `lib/Xtc`, `lib/Txt` |
| In-EPUB images | Partial (alt text only) | `lib/Epub/Epub/parsers/ChapterHtmlSlimParser.cpp` |

## Storage Stack

- Physical storage: SD card only, using SdFat. No SPIFFS/LittleFS usage found.
- Mount point: paths are rooted at `/` (SD root). The cache and settings live under `/.crosspoint`.
- SD init happens early in `src/main.cpp` and is required for normal boot.
- No free space API implemented; Calibre reports hardcoded value.

Evidence (SdFat usage):
```cpp
// open-x4-sdk/libs/hardware/SDCardManager/include/SDCardManager.h
#include <SdFat.h>
class SDCardManager {
  SdFat sd;
};
```

Cache layout (constructed in code):
- `/.crosspoint/settings.bin` - settings (`src/CrossPointSettings.cpp`)
- `/.crosspoint/state.bin` - last open book and sleep state (`src/CrossPointState.cpp`)
- `/.crosspoint/recent.bin` - recent books (`src/RecentBooksStore.cpp`)
- `/.crosspoint/wifi.bin` - Wi-Fi creds, XOR-obfuscated (`src/WifiCredentialStore.cpp`)
- `/.crosspoint/koreader.bin` - KOReader sync creds (`lib/KOReaderSync/KOReaderCredentialStore.cpp`)
- `/.crosspoint/epub_<hash>/` - EPUB cache (book.bin, sections, cover/thumbnail)
  - `book.bin` (metadata, spine/TOC LUTs) - `lib/Epub/Epub/BookMetadataCache.cpp`
  - `sections/<spine>.bin` (page layout) - `lib/Epub/Epub/Section.cpp`
- `/.crosspoint/xtc_<hash>/` - XTC cache (progress, cover/thumbnail)
- `/.crosspoint/txt_<hash>/` - TXT cache (index.bin, progress.bin)

Evidence (Wi-Fi file path):
```cpp
// src/WifiCredentialStore.cpp
constexpr char WIFI_FILE[] = "/.crosspoint/wifi.bin";
```

Content discovery:
- `src/activities/home/MyLibraryActivity.cpp` lists only `.epub`, `.xtc`, `.xtch`, `.txt` in the current folder.
- No repo evidence of an automatic full-library scan; navigation is directory-by-directory.

## Networking Stack

- Wi-Fi STA + AP modes with on-device selection UI and saved credentials on SD.
- HTTP/HTTPS via `HTTPClient` + `WiFiClient` / `WiFiClientSecure`.
- TLS is used but certificate validation is disabled (`setInsecure`).
- mDNS (`ESPmDNS`) and captive DNS server (`DNSServer`) are used in AP mode for file transfer.
- Web server provides file listing, upload (multipart), and WebSocket binary upload.
- Calibre Wireless uses UDP broadcast discovery and TCP protocol.
- OPDS browser downloads via HTTP.
- KOReader sync uses HTTPS + NTP time sync.

Evidence (HTTP + insecure TLS):
```cpp
// src/network/HttpDownloader.cpp
if (UrlUtils::isHttpsUrl(url)) {
  auto* secureClient = new WiFiClientSecure();
  secureClient->setInsecure();
  client.reset(secureClient);
}
HTTPClient http;
```

Evidence (NTP):
```cpp
// src/activities/reader/KOReaderSyncActivity.cpp
esp_sntp_setservername(0, "pool.ntp.org");
esp_sntp_init();
```

## Format Support

### XTC / XTCH (pre-rendered)
- `.xtc` (1-bit XTG) and `.xtch` (2-bit XTH) containers; pages are pre-rendered bitmaps.
- `lib/Xtc/XtcParser.cpp` reads headers, page table, and raw page data from SD.
- `src/activities/reader/XtcReaderActivity.cpp` blits pixels; status bar is already in page data.

Evidence (pre-rendered page load):
```cpp
// lib/Xtc/Xtc/XtcParser.cpp
size_t XtcParser::loadPage(uint32_t pageIndex, uint8_t* buffer, size_t bufferSize)
```

### EPUB (EPUB 2/3)
- EPUB is treated as a ZIP file (`lib/ZipFile/ZipFile.cpp`).
- `container.xml` -> `content.opf` -> spine/TOC cache (`lib/Epub/Epub.cpp`, `lib/Epub/Epub/BookMetadataCache.cpp`).
- Chapter HTML parsing uses Expat (`lib/Epub/Epub/parsers/ChapterHtmlSlimParser.cpp`).
- Chapter pages are cached to SD in `sections/*.bin` and rendered on-device from Page objects.
- Image tags are not rendered; alt text is inserted instead.

Evidence (image tags -> alt text):
```cpp
// lib/Epub/Epub/parsers/ChapterHtmlSlimParser.cpp
if (matches(name, IMAGE_TAGS, NUM_IMAGE_TAGS)) {
  // TODO: Start processing image tags
  // ... use alt text
}
```

### TXT
- Plain text reader with on-device pagination; page index is cached to `index.bin`.
- File content is read in chunks from SD (`Txt::readContent`).
- Cover support: BMP and JPG/JPEG next to the file; PNG is not supported.

Evidence (TXT cover formats):
```cpp
// lib/Txt/Txt.cpp
// PNG files are not supported (would need a PNG decoder)
```

## Render Pipeline (call graph)

EPUB page turn:
```
MappedInputManager -> EpubReaderActivity::loop
  -> updateRequired = true
EpubReaderActivity::displayTaskLoop
  -> renderScreen()
     -> Section::loadPageFromSectionFile()
        -> SdMan.openFileForRead("SCT", ...)
        -> Page::deserialize()
     -> Page::render() -> PageLine::render() -> TextBlock::render()
     -> GfxRenderer::drawText() -> renderer.displayBuffer()
```
Paths: `src/activities/reader/EpubReaderActivity.cpp`, `lib/Epub/Epub/Section.cpp`, `lib/Epub/Epub/Page.cpp`.

XTC/XTCH page turn:
```
MappedInputManager -> XtcReaderActivity::loop
  -> updateRequired = true
XtcReaderActivity::displayTaskLoop
  -> renderScreen()
     -> renderPage()
        -> Xtc::loadPage() -> XtcParser::loadPage()
           -> SD read into bitmap buffer
        -> GfxRenderer::drawPixel() -> renderer.displayBuffer()
```
Paths: `src/activities/reader/XtcReaderActivity.cpp`, `lib/Xtc/Xtc.cpp`, `lib/Xtc/Xtc/XtcParser.cpp`.

TXT page turn:
```
MappedInputManager -> TxtReaderActivity::loop
  -> updateRequired = true
TxtReaderActivity::displayTaskLoop
  -> renderScreen()
     -> loadPageAtOffset() -> Txt::readContent() (SD read)
     -> renderPage() -> GfxRenderer::drawText() -> renderer.displayBuffer()
```
Paths: `src/activities/reader/TxtReaderActivity.cpp`, `lib/Txt/Txt.cpp`.

## Performance and Blocking I/O (where chapter loads block)

- EPUB metadata cache build on first open:
  - `Epub::load` -> `parseContentOpf` -> `BookMetadataCache::buildBookBin` -> `ZipFile` reads and SD writes.
  - Paths: `lib/Epub/Epub.cpp`, `lib/Epub/Epub/BookMetadataCache.cpp`, `lib/ZipFile/ZipFile.cpp`.
- EPUB chapter cache build when `sections/<spine>.bin` is missing:
  - `EpubReaderActivity::renderScreen` -> `Section::createSectionFile`
  - `Epub::readItemContentsToStream` (ZIP inflate) -> temp HTML on SD
  - `ChapterHtmlSlimParser::parseAndBuildPages` -> `Page::serialize` to SD
  - This happens inside the render task; UI shows "Indexing..." and blocks until complete.
- TXT first open or settings change:
  - `TxtReaderActivity::initializeReader` -> `buildPageIndex` scans the entire file and word-wraps using font metrics.
- XTC page loads:
  - Each page turn calls `XtcParser::loadPage` (full page read), then pixel conversion in `renderPage`.

## Implementation Plan

### 1) Offline page prefetch / ring buffer (small change)

Goal: remove SD read latency on page turns, especially for XTC/XTCH (server-preprocessed news pages).

Recommended scope (minimal): XTC/XTCH reader only.

Plan:
1. Add a small cache in `src/activities/reader/XtcReaderActivity.cpp`:
   - Struct `PageCacheSlot { uint32_t page; uint8_t* buf; size_t size; uint16_t w,h; uint8_t bitDepth; }`.
   - Two slots (current + next) to cap RAM usage (~48KB per 1-bit page, ~96KB per 2-bit page at 480x800).
2. On `renderPage()`:
   - If current page is cached, render from the cached buffer.
   - Else, load into current slot via `Xtc::loadPage` (or `loadPageStreaming` into the slot buffer) and render.
3. After rendering, prefetch `currentPage + 1` into the next slot (if in bounds). If user goes backward, prefetch `currentPage - 1` instead.
4. Invalidate cache on large jumps (e.g., skip 10 pages) or when `currentPage` changes by more than 1.
5. Keep everything on the render task thread to avoid SD contention and locking complexity.

Optional extension (if memory allows):
- EPUB prefetch for the next page within the current section:
  - Add `std::unique_ptr<Page> nextPageCache` and `int nextPageIndex` in `EpubReaderActivity`.
  - After rendering page N, call `Section::loadPageFromSectionFile()` for page N+1 and store.
  - On page turn, use cached page if index matches, then prefetch the next.
  - Clear cache whenever `section` changes or a jump occurs.

### 2) Optional Wi-Fi sync client for .xtc/.xtch bundles (larger change)

Goal: pull preprocessed news bundles over Wi-Fi using HTTP GET, with resumable downloads.

Plan (minimal, maintainable):
1. Add settings fields for sync base URL and enable flag in `src/CrossPointSettings.h/.cpp`.
2. Add `NewsSyncActivity` (new) in `src/activities/network/`:
   - Reuse `WifiSelectionActivity` to connect.
   - Show progress and errors on screen.
3. Add `NewsSyncClient` (new) in `src/network/`:
   - Fetch a manifest with `HttpDownloader::fetchUrl`.
   - Parse line-by-line to minimize RAM.

Minimal manifest format (line-based):
```
# crosspoint-sync-v1
base_url=/news
file news/2026-01-24-01.xtch 123456 sha256=deadbeef...
file news/2026-01-24-02.xtc  789012 sha256=...
```
4. For each entry:
   - If file exists and size matches, skip.
   - Else download to `/<path>.part`, then rename on success.
5. Add resume support to `HttpDownloader`:
   - If `.part` exists, get its size and send `Range: bytes=<size>-`.
   - Open file in append mode (new `SDCardManager::openFileForAppend` or direct `sd.open` with `O_APPEND`).
   - Accept `206 Partial Content`; if server returns `200`, restart from 0.
6. Handle intermittent Wi-Fi:
   - Retry on failure with backoff, leaving `.part` intact for future resume.
   - Abort cleanly on user cancel.
7. Integrity check (optional but recommended):
   - Use `miniz` CRC32 on the downloaded file if the manifest provides a CRC.
   - Otherwise rely on size + HTTP completion.

## Branch Plan

- `feature/prefetch-pages`
  - Add XTC page cache/ring buffer in `src/activities/reader/XtcReaderActivity.cpp`.
  - Optional EPUB prefetch in `src/activities/reader/EpubReaderActivity.cpp`.

- `feature/http-sync`
  - Add settings fields in `src/CrossPointSettings.h` and `src/CrossPointSettings.cpp`.
  - Add `src/network/NewsSyncClient.*` and `src/activities/network/NewsSyncActivity.*`.
  - Extend `src/network/HttpDownloader.*` for Range/resume support.

```

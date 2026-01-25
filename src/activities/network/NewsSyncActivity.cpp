#include "NewsSyncActivity.h"

#include <Epub.h>
#include <GfxRenderer.h>
#include <HardwareSerial.h>
#include <OpdsParser.h>
#include <OpdsStream.h>
#include <SDCardManager.h>
#include <Xtc.h>
#include <WiFi.h>
#include <algorithm>

#include "CrossPointSettings.h"
#include "MappedInputManager.h"
#include "ScreenComponents.h"
#include "activities/network/WifiSelectionActivity.h"
#include "fontIds.h"
#include "network/HttpDownloader.h"
#include "util/StringUtils.h"
#include "util/UrlUtils.h"

namespace {
constexpr const char* kNewsDir = "/News";
}  // namespace

void NewsSyncActivity::taskTrampoline(void* param) {
  auto* self = static_cast<NewsSyncActivity*>(param);
  self->displayTaskLoop();
}

void NewsSyncActivity::onEnter() {
  ActivityWithSubactivity::onEnter();

  renderingMutex = xSemaphoreCreateMutex();
  state = SyncState::CHECK_WIFI;
  statusMessage = "Checking WiFi...";
  errorMessage.clear();
  downloadProgress = 0;
  downloadTotal = 0;
  entries.clear();
  selectorIndex = 0;
  updateRequired = true;

  xTaskCreate(&NewsSyncActivity::taskTrampoline, "NewsSyncTask",
              4096,               // Stack size
              this,               // Parameters
              1,                  // Priority
              &displayTaskHandle  // Task handle
  );

  checkAndConnectWifi();
}

void NewsSyncActivity::onExit() {
  ActivityWithSubactivity::onExit();

  // Turn off WiFi when exiting
  WiFi.disconnect(false);
  delay(100);
  WiFi.mode(WIFI_OFF);
  delay(100);

  xSemaphoreTake(renderingMutex, portMAX_DELAY);
  if (displayTaskHandle) {
    vTaskDelete(displayTaskHandle);
    displayTaskHandle = nullptr;
  }
  vSemaphoreDelete(renderingMutex);
  renderingMutex = nullptr;
}

void NewsSyncActivity::checkAndConnectWifi() {
  // Already connected? Verify connection is valid by checking IP
  if (WiFi.status() == WL_CONNECTED && WiFi.localIP() != IPAddress(0, 0, 0, 0)) {
    startSync();
    return;
  }

  // Not connected - launch WiFi selection screen
  state = SyncState::WIFI_SELECTION;
  updateRequired = true;
  enterNewActivity(new WifiSelectionActivity(renderer, mappedInput,
                                             [this](const bool connected) { onWifiSelectionComplete(connected); }));
}

void NewsSyncActivity::onWifiSelectionComplete(const bool connected) {
  exitActivity();

  if (connected) {
    startSync();
  } else {
    setError("WiFi connection failed");
  }
}

void NewsSyncActivity::startSync() {
  const char* serverUrl = SETTINGS.opdsServerUrl;
  if (strlen(serverUrl) == 0) {
    setError("Calibre Web URL not set");
    return;
  }

  const char* feedPath = SETTINGS.opdsNewsPath;
  if (strlen(feedPath) == 0) {
    setError("News Feed Path not set");
    return;
  }

  state = SyncState::FETCHING_FEED;
  statusMessage = "Fetching feed...";
  updateRequired = true;

  const std::string feedUrl = UrlUtils::buildUrl(serverUrl, feedPath);
  Serial.printf("[%lu] [NEWS] Fetching: %s\n", millis(), feedUrl.c_str());

  OpdsParser parser;
  {
    OpdsParserStream stream{parser};
    if (!HttpDownloader::fetchUrl(feedUrl, stream)) {
      setError("Failed to fetch feed");
      return;
    }
  }

  if (!parser) {
    setError("Failed to parse feed");
    return;
  }

  const auto books = parser.getBooks();
  if (books.empty()) {
    setError("No books in feed");
    return;
  }

  entries = books;
  selectorIndex = 0;
  state = SyncState::SELECT_SOURCE;
  statusMessage = "Select source";
  updateRequired = true;
}

void NewsSyncActivity::downloadEntry(const OpdsEntry& entry) {
  const std::string downloadHref = !entry.hrefXtc.empty() ? entry.hrefXtc : entry.href;
  if (downloadHref.empty()) {
    setError("No download link");
    return;
  }
  std::string baseName = entry.title;
  if (!entry.author.empty()) {
    baseName += " - " + entry.author;
  }
  std::string safeName = StringUtils::sanitizeFilename(baseName);
  if (safeName.empty()) {
    safeName = "news";
  }

  // Ensure target directory exists
  SdMan.mkdir(kNewsDir);

  std::string extension = ".epub";
  if (StringUtils::checkFileExtension(downloadHref, ".xtch")) {
    extension = ".xtch";
  } else if (StringUtils::checkFileExtension(downloadHref, ".xtc")) {
    extension = ".xtc";
  }

  const std::string destPath = std::string(kNewsDir) + "/" + safeName + extension;
  if (SdMan.exists(destPath.c_str())) {
    state = SyncState::COMPLETE;
    statusMessage = "Already downloaded";
    updateRequired = true;
    return;
  }

  state = SyncState::DOWNLOADING;
  statusMessage = entry.title.empty() ? "Downloading..." : entry.title;
  downloadProgress = 0;
  downloadTotal = 0;
  updateRequired = true;

  const std::string downloadUrl = UrlUtils::buildUrl(serverUrl, downloadHref);
  Serial.printf("[%lu] [NEWS] Downloading: %s -> %s\n", millis(), downloadUrl.c_str(), destPath.c_str());

  const auto result =
      HttpDownloader::downloadToFile(downloadUrl, destPath, [this](const size_t downloaded, const size_t total) {
        downloadProgress = downloaded;
        downloadTotal = total;
        updateRequired = true;
      });

  if (result == HttpDownloader::OK) {
    // Clear any stale cache if a file with same name existed previously
    if (extension == ".xtch" || extension == ".xtc") {
      Xtc xtc(destPath, "/.crosspoint");
      xtc.clearCache();
    } else {
      Epub epub(destPath, "/.crosspoint");
      epub.clearCache();
    }
    state = SyncState::COMPLETE;
    statusMessage = "Download complete";
    updateRequired = true;
  } else {
    setError("Download failed");
  }
}

void NewsSyncActivity::setError(const std::string& message) {
  state = SyncState::ERROR;
  errorMessage = message;
  updateRequired = true;
}

void NewsSyncActivity::loop() {
  if (subActivity) {
    subActivity->loop();
    return;
  }

  if (state == SyncState::CHECK_WIFI) {
    if (mappedInput.wasPressed(MappedInputManager::Button::Back)) {
      onGoHome();
    }
    return;
  }

  if (state == SyncState::ERROR || state == SyncState::COMPLETE) {
    if (mappedInput.wasPressed(MappedInputManager::Button::Back)) {
      onGoHome();
    } else if (mappedInput.wasPressed(MappedInputManager::Button::Confirm)) {
      if (!entries.empty()) {
        state = SyncState::SELECT_SOURCE;
        updateRequired = true;
      } else {
        onGoHome();
      }
    }
    return;
  }

  if (state == SyncState::SELECT_SOURCE) {
    const bool prevPressed = mappedInput.wasPressed(MappedInputManager::Button::Up) ||
                             mappedInput.wasPressed(MappedInputManager::Button::Left);
    const bool nextPressed = mappedInput.wasPressed(MappedInputManager::Button::Down) ||
                             mappedInput.wasPressed(MappedInputManager::Button::Right);
    if (mappedInput.wasPressed(MappedInputManager::Button::Back)) {
      onGoHome();
      return;
    }
    if (mappedInput.wasPressed(MappedInputManager::Button::Confirm)) {
      if (!entries.empty()) {
        downloadEntry(entries[selectorIndex]);
      }
      return;
    }
    if (prevPressed && !entries.empty()) {
      selectorIndex = (selectorIndex + static_cast<int>(entries.size()) - 1) % static_cast<int>(entries.size());
      updateRequired = true;
    } else if (nextPressed && !entries.empty()) {
      selectorIndex = (selectorIndex + 1) % static_cast<int>(entries.size());
      updateRequired = true;
    }
    return;
  }
}

void NewsSyncActivity::displayTaskLoop() {
  while (true) {
    if (updateRequired) {
      updateRequired = false;
      xSemaphoreTake(renderingMutex, portMAX_DELAY);
      render();
      xSemaphoreGive(renderingMutex);
    }
    vTaskDelay(10 / portTICK_PERIOD_MS);
  }
}

void NewsSyncActivity::render() const {
  renderer.clearScreen();

  const auto pageWidth = renderer.getScreenWidth();
  const auto pageHeight = renderer.getScreenHeight();

  renderer.drawCenteredText(UI_12_FONT_ID, 15, "News Sync", true, EpdFontFamily::BOLD);

  if (state == SyncState::FETCHING_FEED || state == SyncState::CHECK_WIFI) {
    renderer.drawCenteredText(UI_10_FONT_ID, pageHeight / 2, statusMessage.c_str());
  } else if (state == SyncState::SELECT_SOURCE) {
    const int margin = 20;
    const int tileWidth = pageWidth - margin * 2;
    constexpr int tileHeight = 52;
    constexpr int tileSpacing = 8;
    const int listTop = 60;
    const int bottomReserve = 50;
    int availableHeight = pageHeight - listTop - bottomReserve;
    int visibleCount = availableHeight / (tileHeight + tileSpacing);
    if (visibleCount < 1) {
      visibleCount = 1;
    }
    int startIndex = selectorIndex - visibleCount / 2;
    if (startIndex < 0) {
      startIndex = 0;
    }
    if (startIndex + visibleCount > static_cast<int>(entries.size())) {
      startIndex = std::max(0, static_cast<int>(entries.size()) - visibleCount);
    }

    if (entries.empty()) {
      renderer.drawCenteredText(UI_10_FONT_ID, pageHeight / 2, "No sources found");
    } else {
      for (int i = 0; i < visibleCount; ++i) {
        const int entryIndex = startIndex + i;
        if (entryIndex >= static_cast<int>(entries.size())) {
          break;
        }
        const auto& entry = entries[entryIndex];
        const int tileX = margin;
        const int tileY = listTop + i * (tileHeight + tileSpacing);
        const bool selected = entryIndex == selectorIndex;
        if (selected) {
          renderer.fillRect(tileX, tileY, tileWidth, tileHeight);
        } else {
          renderer.drawRect(tileX, tileY, tileWidth, tileHeight);
        }

        const int textX = tileX + 10;
        const int titleY = tileY + 8;
        const int authorY = titleY + renderer.getLineHeight(UI_10_FONT_ID) + 4;
        auto title = renderer.truncatedText(UI_10_FONT_ID, entry.title.c_str(), tileWidth - 20);
        renderer.drawText(UI_10_FONT_ID, textX, titleY, title.c_str(), !selected);
        if (!entry.author.empty()) {
          auto author = renderer.truncatedText(SMALL_FONT_ID, entry.author.c_str(), tileWidth - 20);
          renderer.drawText(SMALL_FONT_ID, textX, authorY, author.c_str(), !selected);
        }
      }
    }
  } else if (state == SyncState::DOWNLOADING) {
    renderer.drawCenteredText(UI_10_FONT_ID, pageHeight / 2 - 40, "Downloading...");
    renderer.drawCenteredText(UI_10_FONT_ID, pageHeight / 2 - 10, statusMessage.c_str());
    if (downloadTotal > 0) {
      const int barWidth = pageWidth - 100;
      constexpr int barHeight = 20;
      constexpr int barX = 50;
      const int barY = pageHeight / 2 + 20;
      ScreenComponents::drawProgressBar(renderer, barX, barY, barWidth, barHeight, downloadProgress, downloadTotal);
    }
  } else if (state == SyncState::COMPLETE) {
    renderer.drawCenteredText(UI_10_FONT_ID, pageHeight / 2 - 20, "Sync complete", true, EpdFontFamily::BOLD);
    renderer.drawCenteredText(UI_10_FONT_ID, pageHeight / 2 + 10, statusMessage.c_str());
  } else if (state == SyncState::ERROR) {
    renderer.drawCenteredText(UI_10_FONT_ID, pageHeight / 2 - 20, "Sync failed", true, EpdFontFamily::BOLD);
    renderer.drawCenteredText(UI_10_FONT_ID, pageHeight / 2 + 10, errorMessage.c_str());
  } else if (state == SyncState::WIFI_SELECTION) {
    renderer.drawCenteredText(UI_10_FONT_ID, pageHeight / 2, "Connecting...");
  }

  if (state == SyncState::SELECT_SOURCE) {
    const auto labels = mappedInput.mapLabels("Back", "Download", "", "");
    renderer.drawButtonHints(UI_10_FONT_ID, labels.btn1, labels.btn2, labels.btn3, labels.btn4);
  } else if (state == SyncState::ERROR || state == SyncState::COMPLETE || state == SyncState::CHECK_WIFI) {
    const auto labels = mappedInput.mapLabels("Back", "Select", "", "");
    renderer.drawButtonHints(UI_10_FONT_ID, labels.btn1, labels.btn2, labels.btn3, labels.btn4);
  }

  renderer.displayBuffer();
}

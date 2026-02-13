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
#include <cctype>

#include "CrossPointSettings.h"
#include "MappedInputManager.h"
#include "WifiCredentialStore.h"
#include "activities/network/WifiSelectionActivity.h"
#include "components/UITheme.h"
#include "fontIds.h"
#include "network/HttpDownloader.h"
#include "util/StringUtils.h"
#include "util/UrlUtils.h"

namespace {
constexpr const char* kNewsDir = "/News";
constexpr unsigned long kWifiConnectTimeoutMs = 12000;
constexpr unsigned long kWifiPollIntervalMs = 200;

std::string trimWhitespace(const std::string& input) {
  size_t start = 0;
  while (start < input.size() && std::isspace(static_cast<unsigned char>(input[start])) != 0) {
    ++start;
  }
  size_t end = input.size();
  while (end > start && std::isspace(static_cast<unsigned char>(input[end - 1])) != 0) {
    --end;
  }
  return input.substr(start, end - start);
}

std::string toLowerCopy(std::string input) {
  for (char& c : input) {
    c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
  }
  return input;
}

bool fetchOpdsFeed(const std::string& url, OpdsParser& parser) {
  OpdsParserStream stream{parser};
  if (!HttpDownloader::fetchUrl(url, stream)) {
    return false;
  }
  return static_cast<bool>(parser);
}

std::string resolveCategoryFeedUrl(const std::string& serverUrl, const std::string& categoryName) {
  const std::string rootUrl = UrlUtils::buildUrl(serverUrl, "/opds");
  OpdsParser parser;
  if (!fetchOpdsFeed(rootUrl, parser)) {
    return {};
  }

  const std::string targetTitle = toLowerCopy("By " + categoryName);
  for (const auto& entry : parser.getEntries()) {
    if (entry.type != OpdsEntryType::NAVIGATION || entry.href.empty()) {
      continue;
    }
    if (toLowerCopy(entry.title) == targetTitle) {
      return UrlUtils::buildUrl(serverUrl, entry.href);
    }
  }
  return {};
}

std::string resolveFeedUrl(const std::string& serverUrl, const std::string& feedPath) {
  const std::string trimmed = trimWhitespace(feedPath);
  if (trimmed.empty()) {
    return {};
  }

  if (trimmed.find('/') != std::string::npos || trimmed.find("opds") != std::string::npos ||
      trimmed.find('?') != std::string::npos) {
    return UrlUtils::buildUrl(serverUrl, trimmed);
  }

  const std::string categoryFeed = resolveCategoryFeedUrl(serverUrl, trimmed);
  if (!categoryFeed.empty()) {
    return categoryFeed;
  }

  return UrlUtils::buildUrl(serverUrl, trimmed);
}

std::vector<OpdsEntry> resolveNavigationBooks(const std::string& serverUrl, const std::vector<OpdsEntry>& navEntries) {
  std::vector<OpdsEntry> resolved;
  for (const auto& entry : navEntries) {
    if (entry.type != OpdsEntryType::NAVIGATION || entry.href.empty()) {
      continue;
    }
    OpdsParser parser;
    const std::string navUrl = UrlUtils::buildUrl(serverUrl, entry.href);
    if (!fetchOpdsFeed(navUrl, parser)) {
      continue;
    }
    auto books = parser.getBooks();
    if (!books.empty()) {
      resolved.push_back(books.front());
    }
  }
  return resolved;
}
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
  lastDownloadedPath.clear();
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

  if (autoConnectSavedWifiOnly) {
    state = SyncState::CHECK_WIFI;
    statusMessage = "Connecting WiFi...";
    updateRequired = true;
    if (tryConnectSavedWifi()) {
      startSync();
    } else {
      setError("No saved WiFi connection");
    }
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

bool NewsSyncActivity::tryConnectSavedWifi() {
  WIFI_STORE.loadFromFile();
  const auto& credentials = WIFI_STORE.getCredentials();

  if (!credentials.empty()) {
    for (const auto& cred : credentials) {
      if (cred.ssid.empty()) {
        continue;
      }
      statusMessage = "Connecting: " + cred.ssid;
      updateRequired = true;
      if (connectToSavedNetwork(cred.ssid, cred.password)) {
        Serial.printf("[%lu] [NEWS] Connected to saved WiFi: %s\n", millis(), cred.ssid.c_str());
        return true;
      }
    }
  }

  // Fallback: try last credentials remembered by the WiFi stack/NVS.
  statusMessage = "Connecting: last network";
  updateRequired = true;
  WiFi.mode(WIFI_STA);
  WiFi.begin();
  const unsigned long start = millis();
  while (millis() - start < kWifiConnectTimeoutMs) {
    if (WiFi.status() == WL_CONNECTED && WiFi.localIP() != IPAddress(0, 0, 0, 0)) {
      Serial.printf("[%lu] [NEWS] Connected via remembered WiFi credentials\n", millis());
      return true;
    }
    delay(kWifiPollIntervalMs);
  }

  WiFi.disconnect();
  delay(50);
  return false;
}

bool NewsSyncActivity::connectToSavedNetwork(const std::string& ssid, const std::string& password) {
  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  delay(100);

  if (password.empty()) {
    WiFi.begin(ssid.c_str());
  } else {
    WiFi.begin(ssid.c_str(), password.c_str());
  }

  const unsigned long start = millis();
  while (millis() - start < kWifiConnectTimeoutMs) {
    if (WiFi.status() == WL_CONNECTED && WiFi.localIP() != IPAddress(0, 0, 0, 0)) {
      return true;
    }
    delay(kWifiPollIntervalMs);
  }

  WiFi.disconnect();
  delay(50);
  return false;
}

void NewsSyncActivity::startSync() {
  const char* serverUrl = SETTINGS.opdsServerUrl;
  if (strlen(serverUrl) == 0) {
    setError("Calibre Web URL not set");
    return;
  }

  const std::string defaultFeedPath = SETTINGS.opdsNewsPath;
  const std::string sourceFeedPath = forcedFeedPath.empty() ? SETTINGS.opdsNewsPath : forcedFeedPath;
  if (sourceFeedPath.empty()) {
    setError("News Feed Path not set");
    return;
  }

  state = SyncState::FETCHING_FEED;
  statusMessage = "Fetching feed...";
  updateRequired = true;

  const auto fetchBooksFromPath = [serverUrl](const std::string& feedPath,
                                              std::vector<OpdsEntry>& outBooks) -> bool {
    const std::string feedUrl = resolveFeedUrl(serverUrl, feedPath);
    if (feedUrl.empty()) {
      return false;
    }
    Serial.printf("[%lu] [NEWS] Fetching: %s\n", millis(), feedUrl.c_str());

    OpdsParser parser;
    if (!fetchOpdsFeed(feedUrl, parser)) {
      return false;
    }

    outBooks = parser.getBooks();
    if (outBooks.empty()) {
      std::vector<OpdsEntry> navEntries;
      for (const auto& entry : parser.getEntries()) {
        if (entry.type == OpdsEntryType::NAVIGATION) {
          navEntries.push_back(entry);
        }
      }
      if (navEntries.empty()) {
        return true;
      }
      outBooks = resolveNavigationBooks(serverUrl, navEntries);
    }
    return true;
  };

  std::string activeFeedPath = sourceFeedPath;
  if (quickSourceMode) {
    const bool sourcePathLooksLikeUrlOrPath = sourceFeedPath.find('/') != std::string::npos ||
                                              sourceFeedPath.find("opds") != std::string::npos ||
                                              sourceFeedPath.find('?') != std::string::npos;
    if (!sourcePathLooksLikeUrlOrPath && !defaultFeedPath.empty()) {
      // For source labels like "Bloomberg", use the shared news feed and select by title.
      activeFeedPath = defaultFeedPath;
    }
  }

  std::vector<OpdsEntry> books;
  if (!fetchBooksFromPath(activeFeedPath, books)) {
    setError("Failed to fetch feed");
    return;
  }
  if (books.empty()) {
    setError("No books in feed");
    return;
  }

  if (quickSourceMode) {
    const std::string target = toLowerCopy(!forcedSourceLabel.empty() ? forcedSourceLabel : forcedFeedPath);
    bool sourceMatched = target.empty();
    if (!target.empty()) {
      const auto it = std::find_if(books.begin(), books.end(), [&target](const OpdsEntry& entry) {
        const std::string title = toLowerCopy(entry.title);
        const std::string author = toLowerCopy(entry.author);
        return title.find(target) != std::string::npos || author.find(target) != std::string::npos;
      });
      if (it != books.end()) {
        books = {*it};
        sourceMatched = true;
      } else if (activeFeedPath != sourceFeedPath) {
        // Fallback: source path may actually be an explicit feed URL/path.
        std::vector<OpdsEntry> sourceBooks;
        if (fetchBooksFromPath(sourceFeedPath, sourceBooks) && !sourceBooks.empty()) {
          books = {sourceBooks.front()};
          sourceMatched = true;
        }
      }
    }
    if (!sourceMatched) {
      setError("Source not found in feed");
      return;
    }
  }

  entries = std::move(books);
  selectorIndex = 0;
  if (quickSourceMode) {
    if (entries.empty()) {
      setError("No books in feed");
      return;
    }
    statusMessage = forcedSourceLabel.empty() ? "Syncing source..." : "Syncing " + forcedSourceLabel + "...";
    updateRequired = true;
    const bool success = downloadEntry(entries.front());
    if (success) {
      state = SyncState::COMPLETE;
      statusMessage = "Sync complete";
      updateRequired = true;
    }
    autoExitPending = true;
  } else if (autoMode) {
    statusMessage = "Auto syncing...";
    updateRequired = true;
    for (const auto& entry : entries) {
      if (!downloadEntry(entry)) {
        break;
      }
    }
    if (state != SyncState::ERROR) {
      state = SyncState::COMPLETE;
      statusMessage = "Sync complete";
      updateRequired = true;
    }
    autoExitPending = true;
  } else {
    state = SyncState::SELECT_SOURCE;
    statusMessage = "Select source";
    updateRequired = true;
  }
}

bool NewsSyncActivity::downloadEntry(const OpdsEntry& entry) {
  const char* serverUrl = SETTINGS.opdsServerUrl;
  if (strlen(serverUrl) == 0) {
    setError("Calibre Web URL not set");
    return false;
  }

  const std::string downloadHref = !entry.hrefXtc.empty() ? entry.hrefXtc : entry.href;
  if (downloadHref.empty()) {
    setError("No download link");
    return false;
  }
  // Use a stable filename per source so daily bundles overwrite cleanly.
  std::string baseName = entry.title;
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
  // Overwrite existing bundle so the latest download replaces prior days.
  if (SdMan.exists(destPath.c_str())) {
    SdMan.remove(destPath.c_str());
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
    lastDownloadedPath = destPath;
    state = SyncState::COMPLETE;
    statusMessage = "Download complete";
    updateRequired = true;
    return true;
  } else {
    setError("Download failed");
    return false;
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

  if (autoExitPending && (state == SyncState::COMPLETE || state == SyncState::ERROR)) {
    autoExitPending = false;
    if (quickSourceMode) {
      if (state == SyncState::COMPLETE && onOpenDownloadedBook && !lastDownloadedPath.empty()) {
        onOpenDownloadedBook(lastDownloadedPath, openDownloadedBookWithChapterSelection);
      } else {
        onGoHome();
      }
      return;
    }
    if (autoMode) {
      onGoHome();
      return;
    }
  }

  if (state == SyncState::CHECK_WIFI) {
    if (mappedInput.wasPressed(MappedInputManager::Button::Back)) {
      onGoHome();
    }
    return;
  }

  if (state == SyncState::ERROR || state == SyncState::COMPLETE) {
    if (quickSourceMode) {
      return;
    }
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
      GUI.drawProgressBar(renderer, Rect{barX, barY, barWidth, barHeight}, downloadProgress, downloadTotal);
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
    GUI.drawButtonHints(renderer, labels.btn1, labels.btn2, labels.btn3, labels.btn4);
  } else if (state == SyncState::ERROR || state == SyncState::COMPLETE || state == SyncState::CHECK_WIFI) {
    const auto labels = mappedInput.mapLabels("Back", "Select", "", "");
    GUI.drawButtonHints(renderer, labels.btn1, labels.btn2, labels.btn3, labels.btn4);
  }

  renderer.displayBuffer();
}

#pragma once
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>
#include <freertos/task.h>

#include <functional>
#include <string>
#include <vector>

#include <OpdsParser.h>

#include "activities/ActivityWithSubactivity.h"

/**
 * News sync activity to fetch a list of entries from a configured OPDS feed.
 */
class NewsSyncActivity final : public ActivityWithSubactivity {
 public:
  explicit NewsSyncActivity(GfxRenderer& renderer, MappedInputManager& mappedInput,
                            const std::function<void()>& onGoHome, bool autoMode = false)
      : ActivityWithSubactivity("NewsSync", renderer, mappedInput),
        onGoHome(onGoHome),
        autoMode(autoMode) {}

  explicit NewsSyncActivity(
      GfxRenderer& renderer, MappedInputManager& mappedInput, const std::function<void()>& onGoHome,
      const std::string& sourceLabel, const std::string& feedPath,
      const std::function<void(const std::string& path, bool openChapterSelection)>& onOpenDownloadedBook,
      bool openChapterSelection = true)
      : ActivityWithSubactivity("NewsSync", renderer, mappedInput),
        onGoHome(onGoHome),
        quickSourceMode(true),
        autoConnectSavedWifiOnly(true),
        autoExitPending(true),
        forcedSourceLabel(sourceLabel),
        forcedFeedPath(feedPath),
        onOpenDownloadedBook(onOpenDownloadedBook),
        openDownloadedBookWithChapterSelection(openChapterSelection) {}

  void onEnter() override;
  void onExit() override;
  void loop() override;

 private:
  enum class SyncState { CHECK_WIFI, WIFI_SELECTION, FETCHING_FEED, SELECT_SOURCE, DOWNLOADING, COMPLETE, ERROR };

  TaskHandle_t displayTaskHandle = nullptr;
  SemaphoreHandle_t renderingMutex = nullptr;
  bool updateRequired = false;

  SyncState state = SyncState::CHECK_WIFI;
  std::string statusMessage;
  std::string errorMessage;
  size_t downloadProgress = 0;
  size_t downloadTotal = 0;
  std::vector<OpdsEntry> entries;
  int selectorIndex = 0;

  const std::function<void()> onGoHome;
  bool autoMode = false;
  bool quickSourceMode = false;
  bool autoConnectSavedWifiOnly = false;
  bool autoExitPending = false;
  std::string forcedSourceLabel;
  std::string forcedFeedPath;
  std::string lastDownloadedPath;
  std::function<void(const std::string& path, bool openChapterSelection)> onOpenDownloadedBook = nullptr;
  bool openDownloadedBookWithChapterSelection = false;

  static void taskTrampoline(void* param);
  [[noreturn]] void displayTaskLoop();
  void render() const;

  void checkAndConnectWifi();
  void onWifiSelectionComplete(bool connected);
  bool tryConnectSavedWifi();
  bool connectToSavedNetwork(const std::string& ssid, const std::string& password);
  void startSync();
  bool downloadEntry(const OpdsEntry& entry);
  void setError(const std::string& message);
};

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
                            const std::function<void()>& onGoHome)
      : ActivityWithSubactivity("NewsSync", renderer, mappedInput), onGoHome(onGoHome) {}

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

  static void taskTrampoline(void* param);
  [[noreturn]] void displayTaskLoop();
  void render() const;

  void checkAndConnectWifi();
  void onWifiSelectionComplete(bool connected);
  void startSync();
  void downloadEntry(const OpdsEntry& entry);
  void setError(const std::string& message);
};

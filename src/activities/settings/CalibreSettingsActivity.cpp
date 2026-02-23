#include "CalibreSettingsActivity.h"

#include <GfxRenderer.h>
#include <I18n.h>

#include <cstring>

#include "CrossPointSettings.h"
#include "MappedInputManager.h"
#include "activities/util/KeyboardEntryActivity.h"
#include "components/UITheme.h"
#include "fontIds.h"

namespace {
constexpr int MENU_ITEMS = 8;
const char* menuNames[MENU_ITEMS] = {"Calibre Web URL", "News Feed Path", "Bloomberg Path", "Businessweek Path",
                                     "WSJ Path", "NYT Path", "Username", "Password"};
}  // namespace

void CalibreSettingsActivity::onEnter() {
  ActivityWithSubactivity::onEnter();

  selectedIndex = 0;
  requestUpdate();
}

void CalibreSettingsActivity::onExit() { ActivityWithSubactivity::onExit(); }

void CalibreSettingsActivity::loop() {
  if (subActivity) {
    subActivity->loop();
    return;
  }

  if (mappedInput.wasPressed(MappedInputManager::Button::Back)) {
    onBack();
    return;
  }

  if (mappedInput.wasPressed(MappedInputManager::Button::Confirm)) {
    handleSelection();
    return;
  }

  buttonNavigator.onNext([this] {
    selectedIndex = (selectedIndex + 1) % MENU_ITEMS;
    requestUpdate();
  });

  buttonNavigator.onPrevious([this] {
    selectedIndex = (selectedIndex + MENU_ITEMS - 1) % MENU_ITEMS;
    requestUpdate();
  });
}

void CalibreSettingsActivity::handleSelection() {
  if (selectedIndex == 0) {
    // OPDS Server URL
    exitActivity();
    enterNewActivity(new KeyboardEntryActivity(
        renderer, mappedInput, menuNames[0], SETTINGS.opdsServerUrl,
        127,    // maxLength
        false,  // not password
        [this](const std::string& url) {
          strncpy(SETTINGS.opdsServerUrl, url.c_str(), sizeof(SETTINGS.opdsServerUrl) - 1);
          SETTINGS.opdsServerUrl[sizeof(SETTINGS.opdsServerUrl) - 1] = '\0';
          SETTINGS.saveToFile();
          exitActivity();
          requestUpdate();
        },
        [this]() {
          exitActivity();
          requestUpdate();
        }));
    return;
  }

  if (selectedIndex == 1) {
    // Shared news feed path
    exitActivity();
    enterNewActivity(new KeyboardEntryActivity(
        renderer, mappedInput, menuNames[1], SETTINGS.opdsNewsPath,
        127,    // maxLength
        false,  // not password
        [this](const std::string& path) {
          strncpy(SETTINGS.opdsNewsPath, path.c_str(), sizeof(SETTINGS.opdsNewsPath) - 1);
          SETTINGS.opdsNewsPath[sizeof(SETTINGS.opdsNewsPath) - 1] = '\0';
          SETTINGS.saveToFile();
          exitActivity();
          requestUpdate();
        },
        [this]() {
          exitActivity();
          requestUpdate();
        }));
    return;
  }

  if (selectedIndex >= 2 && selectedIndex <= 5) {
    char* target = nullptr;
    if (selectedIndex == 2) {
      target = SETTINGS.opdsNewsBloombergPath;
    } else if (selectedIndex == 3) {
      target = SETTINGS.opdsNewsBusinessweekPath;
    } else if (selectedIndex == 4) {
      target = SETTINGS.opdsNewsWsjPath;
    } else {
      target = SETTINGS.opdsNewsNytPath;
    }

    exitActivity();
    enterNewActivity(new KeyboardEntryActivity(
        renderer, mappedInput, menuNames[selectedIndex], target,
        127,    // maxLength
        false,  // not password
        [this, target](const std::string& path) {
          strncpy(target, path.c_str(), 127);
          target[127] = '\0';
          SETTINGS.saveToFile();
          exitActivity();
          requestUpdate();
        },
        [this]() {
          exitActivity();
          requestUpdate();
        }));
    return;
  }

  if (selectedIndex == 6) {
    // Username
    exitActivity();
    enterNewActivity(new KeyboardEntryActivity(
        renderer, mappedInput, menuNames[6], SETTINGS.opdsUsername,
        63,     // maxLength
        false,  // not password
        [this](const std::string& username) {
          strncpy(SETTINGS.opdsUsername, username.c_str(), sizeof(SETTINGS.opdsUsername) - 1);
          SETTINGS.opdsUsername[sizeof(SETTINGS.opdsUsername) - 1] = '\0';
          SETTINGS.saveToFile();
          exitActivity();
          requestUpdate();
        },
        [this]() {
          exitActivity();
          requestUpdate();
        }));
    return;
  }

  if (selectedIndex == 7) {
    // Password
    exitActivity();
    enterNewActivity(new KeyboardEntryActivity(
        renderer, mappedInput, menuNames[7], SETTINGS.opdsPassword,
        63,    // maxLength
        true,  // password mode
        [this](const std::string& password) {
          strncpy(SETTINGS.opdsPassword, password.c_str(), sizeof(SETTINGS.opdsPassword) - 1);
          SETTINGS.opdsPassword[sizeof(SETTINGS.opdsPassword) - 1] = '\0';
          SETTINGS.saveToFile();
          exitActivity();
          requestUpdate();
        },
        [this]() {
          exitActivity();
          requestUpdate();
        }));
  }
}

void CalibreSettingsActivity::render(Activity::RenderLock&&) {
  renderer.clearScreen();

  auto metrics = UITheme::getInstance().getMetrics();
  const auto pageWidth = renderer.getScreenWidth();
  const auto pageHeight = renderer.getScreenHeight();
  GUI.drawHeader(renderer, Rect{0, metrics.topPadding, pageWidth, metrics.headerHeight}, tr(STR_OPDS_BROWSER));
  GUI.drawSubHeader(renderer, Rect{0, metrics.topPadding + metrics.headerHeight, pageWidth, metrics.tabBarHeight},
                    "Use /opds for Calibre server");

  const int contentTop = metrics.topPadding + metrics.headerHeight + metrics.verticalSpacing + metrics.tabBarHeight;
  const int contentHeight = pageHeight - contentTop - metrics.buttonHintsHeight - metrics.verticalSpacing * 2;

  GUI.drawList(
      renderer, Rect{0, contentTop, pageWidth, contentHeight}, MENU_ITEMS, static_cast<int>(selectedIndex),
      [](int index) { return std::string(menuNames[index]); }, nullptr, nullptr,
      [](int index) {
        if (index == 0) {
          return (strlen(SETTINGS.opdsServerUrl) > 0) ? std::string(SETTINGS.opdsServerUrl)
                                                      : std::string(tr(STR_NOT_SET));
        }
        if (index == 1) {
          return (strlen(SETTINGS.opdsNewsPath) > 0) ? std::string(SETTINGS.opdsNewsPath) : std::string(tr(STR_NOT_SET));
        }
        if (index == 2) {
          return (strlen(SETTINGS.opdsNewsBloombergPath) > 0) ? std::string(SETTINGS.opdsNewsBloombergPath)
                                                               : std::string(tr(STR_NOT_SET));
        }
        if (index == 3) {
          return (strlen(SETTINGS.opdsNewsBusinessweekPath) > 0) ? std::string(SETTINGS.opdsNewsBusinessweekPath)
                                                                  : std::string(tr(STR_NOT_SET));
        }
        if (index == 4) {
          return (strlen(SETTINGS.opdsNewsWsjPath) > 0) ? std::string(SETTINGS.opdsNewsWsjPath)
                                                         : std::string(tr(STR_NOT_SET));
        }
        if (index == 5) {
          return (strlen(SETTINGS.opdsNewsNytPath) > 0) ? std::string(SETTINGS.opdsNewsNytPath)
                                                         : std::string(tr(STR_NOT_SET));
        }
        if (index == 6) {
          return (strlen(SETTINGS.opdsUsername) > 0) ? std::string(SETTINGS.opdsUsername)
                                                     : std::string(tr(STR_NOT_SET));
        }
        return (strlen(SETTINGS.opdsPassword) > 0) ? std::string("******") : std::string(tr(STR_NOT_SET));
      },
      true);

  const auto labels = mappedInput.mapLabels(tr(STR_BACK), tr(STR_SELECT), tr(STR_DIR_UP), tr(STR_DIR_DOWN));
  GUI.drawButtonHints(renderer, labels.btn1, labels.btn2, labels.btn3, labels.btn4);

  renderer.displayBuffer();
}

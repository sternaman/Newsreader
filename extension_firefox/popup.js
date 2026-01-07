const statusEl = document.getElementById("status");
const hostInput = document.getElementById("host");
const bookInput = document.getElementById("bookId");
const bulkCheckbox = document.getElementById("bulkCapture");
const mobileCheckbox = document.getElementById("useMobileUA");
const logsEl = document.getElementById("logs");
const refreshLogsButton = document.getElementById("refreshLogs");
const clearLogsButton = document.getElementById("clearLogs");

const MAX_LOGS = 200;

const setStatus = (msg) => {
  statusEl.textContent = msg;
  void writeLog("status", msg);
};

const logEntryToLine = (entry) => {
  const ts = entry?.ts || new Date().toISOString();
  const level = entry?.level || "info";
  const message = entry?.message || "";
  let extra = "";
  if (entry?.data) {
    try {
      extra = ` ${JSON.stringify(entry.data)}`;
    } catch (error) {
      extra = " [data]";
    }
  }
  return `[${ts}] ${level.toUpperCase()}: ${message}${extra}`;
};

const appendLocalLog = async (entry) => {
  const stored = await browser.storage.local.get("logs");
  const logs = Array.isArray(stored.logs) ? stored.logs : [];
  logs.push(entry);
  const trimmed = logs.slice(-MAX_LOGS);
  await browser.storage.local.set({ logs: trimmed });
};

const writeLog = async (level, message, data) => {
  const entry = { ts: new Date().toISOString(), level, message, data };
  try {
    await browser.runtime.sendMessage({ type: "log", entry });
    return;
  } catch (error) {
    await appendLocalLog(entry);
  }
};

const loadLogs = async () => {
  let logs = [];
  try {
    const response = await browser.runtime.sendMessage({ type: "getLogs" });
    if (Array.isArray(response?.logs)) {
      logs = response.logs;
    }
  } catch (error) {
    const stored = await browser.storage.local.get("logs");
    logs = Array.isArray(stored.logs) ? stored.logs : [];
  }
  if (logsEl) {
    logsEl.value = logs.map(logEntryToLine).join("\n");
    logsEl.scrollTop = logsEl.scrollHeight;
  }
};

const clearLogs = async () => {
  try {
    await browser.runtime.sendMessage({ type: "clearLogs" });
  } catch (error) {
    await browser.storage.local.set({ logs: [] });
  }
  if (logsEl) {
    logsEl.value = "";
  }
};

const postJson = async (url, payload) => {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(`${response.status} ${text}`);
  }
  return response.json();
};

const sendBeaconJson = (url, payload) => {
  if (typeof navigator === "undefined" || typeof navigator.sendBeacon !== "function") {
    return false;
  }
  try {
    const blob = new Blob([JSON.stringify(payload)], { type: "application/json" });
    return navigator.sendBeacon(url, blob);
  } catch (error) {
    return false;
  }
};

const getActiveTab = async () => {
  const tabs = await browser.tabs.query({ active: true, currentWindow: true });
  return tabs[0] || null;
};

const fallbackUpdateBook = async (config) => {
  void writeLog("info", "Update book (fallback)", { bookId: config.bookId });
  const tab = await getActiveTab();
  if (!tab) {
    throw new Error("No active tab");
  }
  const response = await browser.tabs.sendMessage(tab.id, { action: "extractList" });
  const items = response?.items || [];
  await postJson(`${config.host}/api/books/${config.bookId}/snapshot`, { items });
  return `Snapshot saved (${items.length} items).`;
};

const fallbackSendArticle = async (config) => {
  void writeLog("info", "Send article (fallback)", { bookId: config.bookId });
  const tab = await getActiveTab();
  if (!tab) {
    throw new Error("No active tab");
  }
  const response = await browser.tabs.sendMessage(tab.id, { action: "captureArticle" });
  const article = response?.article;
  if (!article || !article.content_html) {
    throw new Error("Article extraction failed");
  }
  let sourceDomain = null;
  try {
    sourceDomain = new URL(tab.url).hostname;
  } catch (error) {
    sourceDomain = null;
  }
  await postJson(`${config.host}/api/books/${config.bookId}/articles/ingest`, {
    url: tab.url,
    title: article.title || tab.title || tab.url,
    byline: article.byline,
    excerpt: article.excerpt,
    content_html: article.content_html,
    source_domain: sourceDomain,
    published_at_raw: article.published_at_raw || null,
    text_content: article.text_content || null,
    section: article.section || null
  });
  return "Article sent.";
};

const fallbackBuildIssue = async (config) => {
  void writeLog("info", "Build issue (fallback)", { bookId: config.bookId });
  const url = `${config.host}/api/books/${config.bookId}/issue/build`;
  if (sendBeaconJson(url, {})) {
    return "Issue build triggered.";
  }
  await postJson(url, {});
  return "Issue build triggered.";
};

const callBackground = async (action, config, shouldBulk) => {
  void writeLog("info", "Dispatch action", { action, bookId: config.bookId });
  const response = await browser.runtime.sendMessage({
    action,
    config,
    bulkCapture: shouldBulk
  });
  if (response?.error) {
    throw new Error(response.error);
  }
  return response?.status || "Done.";
};

const normalizeHost = (value) => {
  const trimmed = value.trim();
  if (!trimmed) {
    return "";
  }
  if (!/^https?:\/\//i.test(trimmed)) {
    return `http://${trimmed.replace(/^\/+/, "")}`.replace(/\/+$/, "");
  }
  return trimmed.replace(/\/+$/, "");
};

const readConfig = async () => {
  try {
    return await browser.storage.sync.get(["host", "bookId", "bulkCapture", "useMobileUA"]);
  } catch (error) {
    return await browser.storage.local.get(["host", "bookId", "bulkCapture", "useMobileUA"]);
  }
};

const writeConfig = async (config) => {
  try {
    await browser.storage.sync.set(config);
  } catch (error) {
    await browser.storage.local.set(config);
  }
};

const loadConfig = async () => {
  try {
    const config = await readConfig();
    hostInput.value = config.host || "http://localhost:8000";
    bookInput.value = config.bookId || "";
    bulkCheckbox.checked = Boolean(config.bulkCapture);
    mobileCheckbox.checked = Boolean(config.useMobileUA);
  } catch (error) {
    setStatus(error.message || String(error));
  }
};

const saveConfig = async () => {
  try {
    await writeConfig({
      host: normalizeHost(hostInput.value),
      bookId: bookInput.value.trim(),
      bulkCapture: bulkCheckbox.checked,
      useMobileUA: mobileCheckbox.checked
    });
    setStatus("Settings saved.");
  } catch (error) {
    setStatus(error.message || String(error));
  }
};

const sendAction = async (action) => {
  let config;
  try {
    const stored = await readConfig();
    config = {
      host: normalizeHost(hostInput.value) || stored.host || "http://localhost:8000",
      bookId: bookInput.value.trim() || stored.bookId || "",
      bulkCapture: bulkCheckbox.checked,
      useMobileUA: mobileCheckbox.checked
    };
    await writeConfig(config);
  } catch (error) {
    setStatus(error.message || String(error));
    return;
  }
  void writeLog("info", "Action requested", {
    action,
    bookId: config.bookId,
    bulkCapture: bulkCheckbox.checked,
    useMobileUA: config.useMobileUA
  });
  if (!config.bookId) {
    setStatus("Set a Book ID first.");
    return;
  }
  if (action === "buildIssue") {
    try {
      const status = await fallbackBuildIssue(config);
      setStatus(status);
    } catch (error) {
      setStatus(error.message || String(error));
    }
    return;
  }

  const needsBackground =
    (action === "updateBook" && (bulkCheckbox.checked || config.useMobileUA)) ||
    (action === "sendArticle" && config.useMobileUA);

  if (!needsBackground) {
    try {
      let status;
      if (action === "updateBook") {
        status = await fallbackUpdateBook(config);
      } else if (action === "sendArticle") {
        status = await fallbackSendArticle(config);
      } else {
        status = await fallbackBuildIssue(config);
      }
      setStatus(status);
    } catch (error) {
      setStatus(error.message || String(error));
    }
    return;
  }

  try {
    const status = await callBackground(action, config, bulkCheckbox.checked);
    setStatus(status);
  } catch (error) {
    const message = error?.message || String(error);
    const backgroundMissing =
      /Receiving end does not exist/i.test(message) ||
      /Could not establish connection/i.test(message);
    if (backgroundMissing) {
      try {
        if (action === "updateBook") {
          let status = await fallbackUpdateBook(config);
          if (bulkCheckbox.checked) {
            status = `${status} Bulk capture skipped (background not ready).`;
          }
          if (config.useMobileUA) {
            status = `${status} Mobile UA capture skipped (background not ready).`;
          }
          setStatus(status);
          return;
        }
        if (action === "sendArticle") {
          let status = await fallbackSendArticle(config);
          if (config.useMobileUA) {
            status = `${status} Mobile UA capture skipped (background not ready).`;
          }
          setStatus(status);
          return;
        }
      } catch (fallbackError) {
        setStatus(fallbackError.message || String(fallbackError));
        return;
      }
      setStatus("Background not ready. Reload the add-on and try again.");
      return;
    }
    setStatus(message);
  }
};

document.getElementById("saveConfig").addEventListener("click", saveConfig);
document.getElementById("updateBook").addEventListener("click", () => sendAction("updateBook"));
document.getElementById("sendArticle").addEventListener("click", () => sendAction("sendArticle"));
document.getElementById("buildIssue").addEventListener("click", () => sendAction("buildIssue"));
if (refreshLogsButton) {
  refreshLogsButton.addEventListener("click", loadLogs);
}
if (clearLogsButton) {
  clearLogsButton.addEventListener("click", clearLogs);
}

loadConfig();
void loadLogs();

const statusEl = document.getElementById("status");
const hostInput = document.getElementById("host");
const bookInput = document.getElementById("bookId");
const bulkCheckbox = document.getElementById("bulkCapture");
const mobileCheckbox = document.getElementById("useMobileUA");
const frontPageCheckbox = document.getElementById("frontPageOnly");
const logsEl = document.getElementById("logs");
const refreshLogsButton = document.getElementById("refreshLogs");
const clearLogsButton = document.getElementById("clearLogs");
const previewListEl = document.getElementById("previewList");
const previewCountEl = document.getElementById("previewCount");
const saveSnapshotButton = document.getElementById("saveSnapshot");
const bulkCaptureSelectedButton = document.getElementById("bulkCaptureSelected");
const selectAllButton = document.getElementById("selectAll");
const selectNoneButton = document.getElementById("selectNone");

const MAX_LOGS = 200;
const MIN_TITLE_LENGTH = 8;

let previewSourceItems = [];
let previewItems = [];

const setStatus = (msg) => {
  statusEl.textContent = msg;
  void writeLog("status", msg);
};

const setPreviewStatus = (msg) => {
  previewCountEl.textContent = msg;
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

const fallbackPreviewList = async () => {
  const tab = await getActiveTab();
  if (!tab) {
    throw new Error("No active tab");
  }
  const response = await browser.tabs.sendMessage(tab.id, { action: "extractList" });
  return response?.items || [];
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

const callBackground = async (payload) => {
  const response = await browser.runtime.sendMessage(payload);
  if (response?.error) {
    throw new Error(response.error);
  }
  return response;
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

const buildConfigKey = (host, bookId) => `${host}::${bookId}`;

const readConfig = async () => {
  const keys = ["host", "bookId", "bulkCapture", "useMobileUA", "frontPageOnly", "configByBook"];
  try {
    return await browser.storage.sync.get(keys);
  } catch (error) {
    return await browser.storage.local.get(keys);
  }
};

const writeConfig = async (config) => {
  try {
    await browser.storage.sync.set(config);
  } catch (error) {
    await browser.storage.local.set(config);
  }
};

const applyStoredSettings = (stored, hostValue, bookValue) => {
  const configByBook = stored.configByBook || {};
  const key = buildConfigKey(hostValue, bookValue);
  const perBook = configByBook[key] || {};
  const fallback = {
    bulkCapture: stored.bulkCapture,
    useMobileUA: stored.useMobileUA,
    frontPageOnly: stored.frontPageOnly
  };
  const settings = { ...fallback, ...perBook };
  bulkCheckbox.checked = Boolean(settings.bulkCapture);
  mobileCheckbox.checked = Boolean(settings.useMobileUA);
  frontPageCheckbox.checked = Boolean(settings.frontPageOnly);
};

const loadPerBookSettings = async () => {
  const stored = await readConfig();
  const hostValue = normalizeHost(hostInput.value) || stored.host || "http://localhost:8000";
  const bookValue = bookInput.value.trim();
  applyStoredSettings(stored, hostValue, bookValue);
  resetPreview("No preview loaded.");
};

const loadConfig = async () => {
  try {
    const stored = await readConfig();
    const hostValue = normalizeHost(stored.host || hostInput.value || "http://localhost:8000");
    const bookValue = stored.bookId || "";
    hostInput.value = hostValue || "http://localhost:8000";
    bookInput.value = bookValue;
    applyStoredSettings(stored, hostValue, bookValue);
  } catch (error) {
    setStatus(error.message || String(error));
  }
};

const persistConfig = async (config, stored) => {
  const configByBook = stored.configByBook || {};
  if (config.host && config.bookId) {
    configByBook[buildConfigKey(config.host, config.bookId)] = {
      bulkCapture: config.bulkCapture,
      useMobileUA: config.useMobileUA,
      frontPageOnly: config.frontPageOnly
    };
  }
  await writeConfig({
    host: config.host,
    bookId: config.bookId,
    bulkCapture: config.bulkCapture,
    useMobileUA: config.useMobileUA,
    frontPageOnly: config.frontPageOnly,
    configByBook
  });
};

const buildConfigFromInputs = async () => {
  const stored = await readConfig();
  const hostValue = normalizeHost(hostInput.value) || stored.host || "http://localhost:8000";
  const bookValue = bookInput.value.trim() || stored.bookId || "";
  return {
    config: {
      host: hostValue,
      bookId: bookValue,
      bulkCapture: bulkCheckbox.checked,
      useMobileUA: mobileCheckbox.checked,
      frontPageOnly: frontPageCheckbox.checked
    },
    stored
  };
};

const normalizeItems = (items) => {
  if (!Array.isArray(items)) {
    return [];
  }
  return items
    .filter((item) => item && item.url)
    .map((item) => ({
      title: (item.title || item.url || "").trim(),
      url: item.url.trim(),
      ts: item.ts || null
    }))
    .filter((item) => item.url && item.title.length >= MIN_TITLE_LENGTH);
};

const normalizeUrlKey = (value) => {
  try {
    const url = new URL(value);
    return `${url.origin}${url.pathname}`.toLowerCase();
  } catch (error) {
    return value.toLowerCase();
  }
};

const dedupeItems = (items) => {
  const seen = new Set();
  const deduped = [];
  for (const item of items) {
    const key = normalizeUrlKey(item.url);
    if (seen.has(key)) {
      continue;
    }
    seen.add(key);
    deduped.push(item);
  }
  return deduped;
};

const isWsjFrontPageUrl = (url) => {
  if (!url.hostname.endsWith("wsj.com")) {
    return false;
  }
  const path = url.pathname.replace(/\/+$/, "");
  const last = path.split("/").filter(Boolean).pop() || "";
  if (!/-[0-9a-f]{8}$/i.test(last)) {
    return false;
  }
  if (/\/(video|podcasts|livecoverage|live-coverage|journalcollection|buy-side)/i.test(path)) {
    return false;
  }
  const mod = url.searchParams.get("mod");
  if (mod && !/(hp|mhp)/i.test(mod)) {
    return false;
  }
  return true;
};

const isBloombergFrontPageUrl = (url) => {
  if (!/bloomberg\.com$|businessweek\.com$/i.test(url.hostname)) {
    return false;
  }
  const path = url.pathname.toLowerCase();
  if (/\/(video|podcast)/.test(path)) {
    return false;
  }
  if (/\/news\/articles\/\d{4}-\d{2}-\d{2}\//.test(path)) {
    return true;
  }
  if (/\/(features|graphics|articles)\//.test(path)) {
    return true;
  }
  if (/\/news\/articles\//.test(path)) {
    return true;
  }
  return false;
};

const isGenericFrontPageUrl = (url) => {
  const segments = url.pathname.split("/").filter(Boolean);
  if (segments.length < 2) {
    return false;
  }
  const last = segments[segments.length - 1] || "";
  if (last.length < 12) {
    return false;
  }
  if (!/[a-z]/i.test(last)) {
    return false;
  }
  return true;
};

const applyFrontPageFilter = (items) => {
  if (!frontPageCheckbox.checked) {
    return items;
  }
  return items.filter((item) => {
    try {
      const url = new URL(item.url);
      if (isWsjFrontPageUrl(url)) {
        return true;
      }
      if (isBloombergFrontPageUrl(url)) {
        return true;
      }
      return isGenericFrontPageUrl(url);
    } catch (error) {
      return false;
    }
  });
};

const buildPreviewItems = (items) =>
  items.map((item, index) => ({
    id: String(index),
    title: item.title || item.url,
    url: item.url,
    ts: item.ts || null,
    selected: true
  }));

const updatePreviewControls = () => {
  const total = previewItems.length;
  const selected = previewItems.filter((item) => item.selected).length;
  if (!total) {
    setPreviewStatus("No preview loaded.");
  } else {
    setPreviewStatus(`${selected}/${total} items selected`);
  }
  saveSnapshotButton.disabled = selected === 0;
  bulkCaptureSelectedButton.disabled = selected === 0 || !bulkCheckbox.checked;
};

const renderPreview = () => {
  previewListEl.innerHTML = "";
  if (!previewItems.length) {
    updatePreviewControls();
    return;
  }
  for (const item of previewItems) {
    const row = document.createElement("div");
    row.className = "preview-item";
    row.dataset.id = item.id;

    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.className = "preview-select";
    checkbox.checked = item.selected;

    const fields = document.createElement("div");
    fields.className = "preview-fields";

    const titleInput = document.createElement("input");
    titleInput.type = "text";
    titleInput.className = "preview-title";
    titleInput.value = item.title;

    const urlInput = document.createElement("input");
    urlInput.type = "text";
    urlInput.className = "preview-url";
    urlInput.value = item.url;

    fields.appendChild(titleInput);
    fields.appendChild(urlInput);

    row.appendChild(checkbox);
    row.appendChild(fields);
    previewListEl.appendChild(row);
  }
  updatePreviewControls();
};

const resetPreview = (message) => {
  previewSourceItems = [];
  previewItems = [];
  previewListEl.innerHTML = "";
  setPreviewStatus(message);
  updatePreviewControls();
};

const applyFiltersAndRender = () => {
  const filtered = applyFrontPageFilter(dedupeItems(previewSourceItems));
  previewItems = buildPreviewItems(filtered);
  renderPreview();
};

const getItemById = (id) => previewItems.find((item) => item.id === id);

const getSelectedPreviewItems = () =>
  previewItems.filter((item) => item.selected && item.url);

const setAllSelections = (value) => {
  previewItems.forEach((item) => {
    item.selected = value;
  });
  previewListEl.querySelectorAll(".preview-select").forEach((el) => {
    el.checked = value;
  });
  updatePreviewControls();
};

const previewUpdateBook = async () => {
  const { config, stored } = await buildConfigFromInputs();
  if (!config.bookId) {
    setStatus("Set a Book ID first.");
    return;
  }
  await persistConfig(config, stored);
  setStatus("Loading preview...");
  try {
    const response = await callBackground({ action: "previewList", config });
    const items = normalizeItems(response?.items || []);
    previewSourceItems = items;
    applyFiltersAndRender();
    setStatus(`Preview loaded (${previewItems.length} items).`);
  } catch (error) {
    const message = error?.message || String(error);
    const backgroundMissing =
      /Receiving end does not exist/i.test(message) ||
      /Could not establish connection/i.test(message);
    if (backgroundMissing) {
      try {
        const items = normalizeItems(await fallbackPreviewList());
        previewSourceItems = items;
        applyFiltersAndRender();
        setStatus("Preview loaded (background fallback). Mobile UA not applied.");
        return;
      } catch (fallbackError) {
        setStatus(fallbackError.message || String(fallbackError));
        return;
      }
    }
    setStatus(message);
  }
};

const saveSnapshotInternal = async (config, items) => {
  if (!items.length) {
    setStatus("Select at least one item.");
    return false;
  }
  try {
    const response = await callBackground({ action: "saveSnapshot", config, items });
    setStatus(response?.status || "Snapshot saved.");
    return true;
  } catch (error) {
    try {
      await postJson(`${config.host}/api/books/${config.bookId}/snapshot`, { items });
      setStatus(`Snapshot saved (${items.length} items).`);
      return true;
    } catch (fallbackError) {
      setStatus(fallbackError.message || String(fallbackError));
      return false;
    }
  }
};

const saveSnapshot = async () => {
  const { config, stored } = await buildConfigFromInputs();
  if (!config.bookId) {
    setStatus("Set a Book ID first.");
    return;
  }
  await persistConfig(config, stored);
  const selected = getSelectedPreviewItems();
  void saveSnapshotInternal(config, selected);
};

const bulkCaptureSelected = async () => {
  const { config, stored } = await buildConfigFromInputs();
  if (!config.bookId) {
    setStatus("Set a Book ID first.");
    return;
  }
  await persistConfig(config, stored);
  const selected = getSelectedPreviewItems();
  const saved = await saveSnapshotInternal(config, selected);
  if (!saved) {
    return;
  }
  try {
    const response = await callBackground({
      action: "bulkCaptureItems",
      config,
      items: selected,
      buildIssue: bulkCheckbox.checked
    });
    setStatus(response?.status || "Bulk capture finished.");
  } catch (error) {
    setStatus(error.message || String(error));
  }
};

const sendAction = async (action) => {
  let config;
  let stored;
  try {
    const result = await buildConfigFromInputs();
    config = result.config;
    stored = result.stored;
    await persistConfig(config, stored);
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

  const needsBackground = action === "sendArticle" && config.useMobileUA;
  if (!needsBackground) {
    try {
      const status = await fallbackSendArticle(config);
      setStatus(status);
    } catch (error) {
      setStatus(error.message || String(error));
    }
    return;
  }

  try {
    const response = await callBackground({ action, config, bulkCapture: bulkCheckbox.checked });
    setStatus(response?.status || "Done.");
  } catch (error) {
    const message = error?.message || String(error);
    const backgroundMissing =
      /Receiving end does not exist/i.test(message) ||
      /Could not establish connection/i.test(message);
    if (backgroundMissing) {
      try {
        const status = await fallbackSendArticle(config);
        setStatus(`${status} Mobile UA capture skipped (background not ready).`);
        return;
      } catch (fallbackError) {
        setStatus(fallbackError.message || String(fallbackError));
        return;
      }
    }
    setStatus(message);
  }
};

previewListEl.addEventListener("change", (event) => {
  const target = event.target;
  if (!(target instanceof HTMLInputElement)) {
    return;
  }
  if (!target.classList.contains("preview-select")) {
    return;
  }
  const row = target.closest(".preview-item");
  const id = row?.dataset?.id;
  if (!id) {
    return;
  }
  const item = getItemById(id);
  if (!item) {
    return;
  }
  item.selected = target.checked;
  updatePreviewControls();
});

previewListEl.addEventListener("input", (event) => {
  const target = event.target;
  if (!(target instanceof HTMLInputElement)) {
    return;
  }
  const row = target.closest(".preview-item");
  const id = row?.dataset?.id;
  if (!id) {
    return;
  }
  const item = getItemById(id);
  if (!item) {
    return;
  }
  if (target.classList.contains("preview-title")) {
    item.title = target.value;
  } else if (target.classList.contains("preview-url")) {
    item.url = target.value;
  }
});

bulkCheckbox.addEventListener("change", updatePreviewControls);
frontPageCheckbox.addEventListener("change", () => {
  if (!previewSourceItems.length) {
    return;
  }
  applyFiltersAndRender();
});

hostInput.addEventListener("change", loadPerBookSettings);
bookInput.addEventListener("change", loadPerBookSettings);

selectAllButton.addEventListener("click", () => setAllSelections(true));
selectNoneButton.addEventListener("click", () => setAllSelections(false));

saveSnapshotButton.addEventListener("click", saveSnapshot);
bulkCaptureSelectedButton.addEventListener("click", bulkCaptureSelected);

document.getElementById("saveConfig").addEventListener("click", async () => {
  try {
    const result = await buildConfigFromInputs();
    await persistConfig(result.config, result.stored);
    setStatus("Settings saved.");
  } catch (error) {
    setStatus(error.message || String(error));
  }
});

document.getElementById("updateBook").addEventListener("click", previewUpdateBook);
document.getElementById("sendArticle").addEventListener("click", () => sendAction("sendArticle"));
document.getElementById("buildIssue").addEventListener("click", () => sendAction("buildIssue"));

if (refreshLogsButton) {
  refreshLogsButton.addEventListener("click", loadLogs);
}
if (clearLogsButton) {
  clearLogsButton.addEventListener("click", clearLogs);
}

resetPreview("No preview loaded.");
loadConfig();
void loadLogs();

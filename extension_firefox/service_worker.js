const DEFAULT_MAX_ITEMS = 20;
const DEFAULT_THROTTLE_MS = 1500;
const IMAGE_INLINE_MAX_COUNT = 0;
const IMAGE_INLINE_MAX_BYTES = 0;
const INLINE_LEAD_MAX = 6;
const CHART_HINT_RE = /(chart|graph|infographic|data|plot|table|map)/i;
const IMAGE_INLINE_TIMEOUT_MS = 15000;
const MOBILE_UA =
  "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1";
const WSJ_URLS = ["*://*.wsj.com/*"];
const LOG_KEY = "logs";
const LOG_MAX = 200;
const PANEL_URL = browser.runtime.getURL("panel.html");
const PANEL_WIDTH = 460;
const PANEL_HEIGHT = 720;
const RETRY_DELAY_MS = 400;
const RETRY_ATTEMPTS = 5;
let lastContentTabId = null;
let lastContentTabUrl = null;
const injectedTabs = new Set();
const ARTICLE_WAIT_OPTIONS = {
  timeoutMs: 12000,
  intervalMs: 400,
  minTextLength: 400
};

const appendLog = async (entry) => {
  const stored = await browser.storage.local.get(LOG_KEY);
  const logs = Array.isArray(stored[LOG_KEY]) ? stored[LOG_KEY] : [];
  logs.push(entry);
  const trimmed = logs.slice(-LOG_MAX);
  await browser.storage.local.set({ [LOG_KEY]: trimmed });
};

const writeLog = async (level, message, data) => {
  const entry = { ts: new Date().toISOString(), level, message, data };
  await appendLog(entry);
};

const isUserContentUrl = (value) => {
  if (!value) {
    return false;
  }
  try {
    const url = new URL(value);
    return url.protocol === "http:" || url.protocol === "https:";
  } catch (error) {
    return false;
  }
};

const recordContentTab = (tab) => {
  if (!tab || !isUserContentUrl(tab.url)) {
    return;
  }
  lastContentTabId = tab.id;
  lastContentTabUrl = tab.url;
};

const getPreferredContentTab = async () => {
  const activeTabs = await browser.tabs.query({ active: true, currentWindow: true });
  const active = activeTabs[0] || null;
  if (active && isUserContentUrl(active.url)) {
    return active;
  }
  if (lastContentTabId) {
    try {
      const lastTab = await browser.tabs.get(lastContentTabId);
      if (isUserContentUrl(lastTab.url)) {
        return lastTab;
      }
    } catch (error) {
      // Ignore missing tab.
    }
  }
  const allTabs = await browser.tabs.query({ currentWindow: true });
  const candidates = allTabs.filter((tab) => isUserContentUrl(tab.url));
  if (!candidates.length) {
    return null;
  }
  candidates.sort((a, b) => (b.lastAccessed || 0) - (a.lastAccessed || 0));
  return candidates[0];
};

const shouldRetryError = (error) => {
  const message = error?.message || String(error);
  return /Receiving end does not exist|Could not establish connection/i.test(message);
};

const ensureContentScripts = async (tabId) => {
  if (!browser.tabs?.executeScript) {
    return;
  }
  try {
    await browser.tabs.executeScript(tabId, { file: "readability.js" });
  } catch (error) {
    // Ignore duplicate injections or restricted pages.
  }
  try {
    await browser.tabs.executeScript(tabId, { file: "content_script.js" });
  } catch (error) {
    // Ignore duplicate injections or restricted pages.
  }
};

const ensureContentScriptsIfNeeded = async (tabId, logOnInject) => {
  if (injectedTabs.has(tabId)) {
    return;
  }
  await ensureContentScripts(tabId);
  injectedTabs.add(tabId);
  if (logOnInject) {
    await writeLog("info", "Injected content scripts", { tabId });
  }
};

const focusOrOpenPanel = async () => {
  const windows = await browser.windows.getAll({ populate: true });
  for (const win of windows) {
    for (const tab of win.tabs || []) {
      if (tab.url && tab.url.startsWith(PANEL_URL)) {
        await browser.windows.update(win.id, { focused: true });
        if (tab.id) {
          await browser.tabs.update(tab.id, { active: true });
        }
        return;
      }
    }
  }
  await browser.windows.create({
    url: PANEL_URL,
    type: "popup",
    width: PANEL_WIDTH,
    height: PANEL_HEIGHT
  });
};

browser.tabs.onActivated.addListener((info) => {
  browser.tabs
    .get(info.tabId)
    .then((tab) => recordContentTab(tab))
    .catch(() => {});
});

browser.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.status === "complete") {
    recordContentTab(tab);
  }
});

const isWsjUrl = (value) => {
  if (!value) {
    return false;
  }
  try {
    const url = new URL(value);
    return url.hostname === "wsj.com" || url.hostname.endsWith(".wsj.com");
  } catch (error) {
    return false;
  }
};

const buildHeaders = () => ({
  "Content-Type": "application/json"
});

const postJson = async (url, payload) => {
  const response = await fetch(url, {
    method: "POST",
    headers: buildHeaders(),
    body: JSON.stringify(payload)
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(`${response.status} ${text}`);
  }
  return response.json();
};

const isWsjHost = (hostname) => hostname === "wsj.com" || hostname.endsWith(".wsj.com");
const isBloombergHost = (hostname) =>
  hostname === "bloomberg.com" ||
  hostname.endsWith(".bloomberg.com") ||
  hostname === "businessweek.com" ||
  hostname.endsWith(".businessweek.com");

const blobToDataUrl = (blob) =>
  new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onloadend = () => resolve(reader.result);
    reader.onerror = () => reject(new Error("Failed to read image"));
    reader.readAsDataURL(blob);
  });

const getInlinePolicy = (baseUrl) => {
  try {
    const hostname = new URL(baseUrl).hostname;
    if (isWsjHost(hostname) || isBloombergHost(hostname)) {
      return "lead+charts";
    }
  } catch (error) {
    // ignore
  }
  return "all";
};

const isPlaceholderImage = (src) => {
  if (!src) {
    return true;
  }
  const lowered = src.toLowerCase();
  if (lowered.startsWith("data:") && lowered.length < 200) {
    return true;
  }
  if (lowered.startsWith("blob:")) {
    return true;
  }
  return /pixel|spacer|transparent|1x1/.test(lowered);
};

const isChartImage = (img) => {
  const haystack = [
    img.getAttribute("alt"),
    img.getAttribute("src"),
    img.getAttribute("data-src"),
    img.className,
    img.id
  ]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
  return CHART_HINT_RE.test(haystack);
};

const selectImagesForInlining = (doc, policy) => {
  const images = Array.from(doc.querySelectorAll("img"));
  if (policy === "all") {
    return images;
  }
  const usable = images.filter((img) => !isPlaceholderImage(img.getAttribute("src") || img.src || ""));
  let lead =
    doc.querySelector("figure img") ||
    doc.querySelector("picture img") ||
    usable[0] ||
    images[0] ||
    null;
  const selected = [];
  if (lead && !isPlaceholderImage(lead.getAttribute("src") || lead.src || "")) {
    selected.push(lead);
  }
  for (const img of usable) {
    if (selected.includes(img)) {
      continue;
    }
    if (isChartImage(img)) {
      selected.push(img);
    }
    if (selected.length >= INLINE_LEAD_MAX) {
      break;
    }
  }
  return selected;
};

const inlineImages = async (html, baseUrl) => {
  if (typeof DOMParser === "undefined") {
    return html;
  }
  const doc = new DOMParser().parseFromString(html, "text/html");
  const policy = getInlinePolicy(baseUrl);
  const images = Array.from(doc.querySelectorAll("img"));
  const selected = selectImagesForInlining(doc, policy);
  const selectedSet = new Set(selected);
  if (policy !== "all") {
    images.forEach((img) => {
      if (!selectedSet.has(img)) {
        img.remove();
      }
    });
  }
  let count = 0;
  const maxCount = policy === "lead+charts" ? INLINE_LEAD_MAX : IMAGE_INLINE_MAX_COUNT;
  for (const img of selected) {
    if (maxCount > 0 && count >= maxCount) {
      break;
    }
    const src = img.getAttribute("src");
    if (!src || src.startsWith("data:")) {
      continue;
    }
    let resolved;
    try {
      resolved = new URL(src, baseUrl).toString();
    } catch (error) {
      continue;
    }
    try {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), IMAGE_INLINE_TIMEOUT_MS);
      const response = await fetch(resolved, { credentials: "include", signal: controller.signal });
      clearTimeout(timeout);
      if (!response.ok) {
        continue;
      }
      const blob = await response.blob();
      if (IMAGE_INLINE_MAX_BYTES > 0 && blob.size > IMAGE_INLINE_MAX_BYTES) {
        continue;
      }
      const dataUrl = await blobToDataUrl(blob);
      img.setAttribute("src", dataUrl);
      img.removeAttribute("srcset");
      count += 1;
    } catch (error) {
      continue;
    }
  }
  return doc.body ? doc.body.innerHTML : html;
};

const withMobileUAForTab = (tabId, enabled) => {
  if (!enabled || !browser.webRequest || !browser.webRequest.onBeforeSendHeaders) {
    return () => {};
  }
  const listener = (details) => {
    if (details.tabId !== tabId) {
      return {};
    }
    const headers = details.requestHeaders || [];
    const existing = headers.find((h) => h.name.toLowerCase() === "user-agent");
    if (existing) {
      existing.value = MOBILE_UA;
    } else {
      headers.push({ name: "User-Agent", value: MOBILE_UA });
    }
    return { requestHeaders: headers };
  };
  browser.webRequest.onBeforeSendHeaders.addListener(
    listener,
    { urls: WSJ_URLS, types: ["main_frame"], tabId },
    ["blocking", "requestHeaders"]
  );
  return () => {
    if (browser.webRequest.onBeforeSendHeaders.hasListener(listener)) {
      browser.webRequest.onBeforeSendHeaders.removeListener(listener);
    }
  };
};

const openCaptureTab = async (url, useMobileUA) => {
  if (useMobileUA) {
    const tab = await browser.tabs.create({ url: "about:blank", active: false });
    const removeListener = withMobileUAForTab(tab.id, true);
    await browser.tabs.update(tab.id, { url });
    return { tab, removeListener };
  }
  const tab = await browser.tabs.create({ url, active: false });
  return { tab, removeListener: () => {} };
};

const sendMessageWithRetry = async (tabId, message) => {
  let lastError = null;
  await ensureContentScriptsIfNeeded(tabId, false);
  for (let attempt = 0; attempt < RETRY_ATTEMPTS; attempt += 1) {
    try {
      return await browser.tabs.sendMessage(tabId, message);
    } catch (error) {
      lastError = error;
      if (!shouldRetryError(error)) {
        throw error;
      }
      if (attempt === 0) {
        await ensureContentScriptsIfNeeded(tabId, true);
      }
      await sleep(RETRY_DELAY_MS);
    }
  }
  throw lastError || new Error("Content script not reachable");
};

const extractListFromTab = async (tabId, options = {}) => {
  const action = options.waitFor ? "extractListWait" : "extractList";
  const response = await sendMessageWithRetry(tabId, { action, options });
  return response?.items || [];
};

const extractListForUpdate = async (tab, config) => {
  const waitOptions = {
    waitFor: Boolean(config.useMobileUA),
    timeoutMs: 10000,
    intervalMs: 400
  };
  if (!config.useMobileUA || !isWsjUrl(tab?.url)) {
    await ensureContentScriptsIfNeeded(tab.id, false);
    return extractListFromTab(tab.id, waitOptions);
  }
  let tempTab = null;
  let removeListener = () => {};
  try {
    const opened = await openCaptureTab(tab.url, true);
    tempTab = opened.tab;
    removeListener = opened.removeListener;
    await waitForTabLoad(tempTab.id);
    await ensureContentScriptsIfNeeded(tempTab.id, false);
    return await extractListFromTab(tempTab.id, waitOptions);
  } finally {
    removeListener();
    if (tempTab?.id) {
      await browser.tabs.remove(tempTab.id);
    }
  }
};

const captureArticleFromTab = async (tabId, options = {}) => {
  const response = await sendMessageWithRetry(tabId, {
    action: "captureArticleWait",
    options
  });
  return response?.article;
};

const waitForTabLoad = (tabId) => {
  return new Promise((resolve, reject) => {
    const listener = (updatedTabId, info) => {
      if (updatedTabId === tabId && info.status === "complete") {
        browser.tabs.onUpdated.removeListener(listener);
        resolve();
      }
    };
    const timeout = setTimeout(() => {
      browser.tabs.onUpdated.removeListener(listener);
      reject(new Error("Timeout waiting for load"));
    }, 20000);
    browser.tabs.onUpdated.addListener(listener);
  });
};

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const bulkCapture = async (items, config) => {
  const { host, bookId } = config;
  const results = [];
  const limited = items.slice(0, DEFAULT_MAX_ITEMS);
  for (const item of limited) {
    let tab = null;
    let removeListener = () => {};
    try {
      const opened = await openCaptureTab(item.url, config.useMobileUA);
      tab = opened.tab;
      removeListener = opened.removeListener;
      await waitForTabLoad(tab.id);
      await ensureContentScriptsIfNeeded(tab.id, false);
      const article = await captureArticleFromTab(tab.id, ARTICLE_WAIT_OPTIONS);
      if (!article || !article.content_html) {
        throw new Error("Article extraction failed");
      }
      await writeLog("info", "Article payload sizes", {
        url: item.url,
        htmlLength: article.content_html.length,
        textLength: (article.text_content || "").length
      });
      const contentHtml = await inlineImages(article.content_html, item.url);
      await postJson(`${host}/api/books/${bookId}/articles/ingest`, {
        url: item.url,
        title: article.title || item.title,
        byline: article.byline,
        excerpt: article.excerpt,
        content_html: contentHtml,
        source_domain: new URL(item.url).hostname,
        published_at_raw: article.published_at_raw || item.ts || null,
        text_content: article.text_content || null,
        section: article.section || null
      });
      results.push({ url: item.url, status: "ok" });
    } catch (error) {
      results.push({ url: item.url, status: "error", error: error.message });
    } finally {
      removeListener();
      if (tab?.id) {
        await browser.tabs.remove(tab.id);
      }
    }
    await sleep(DEFAULT_THROTTLE_MS);
  }
  return results;
};

const handleAction = async (action, config, shouldBulk) => {
  if (!action) {
    return {};
  }
  if (!config || !config.host || !config.bookId) {
    return { error: "Missing host or Book ID" };
  }

  try {
    await writeLog("info", "Handle action", {
      action,
      bookId: config.bookId,
      bulkCapture: Boolean(shouldBulk),
      useMobileUA: Boolean(config.useMobileUA)
    });
    const tab = await getPreferredContentTab();
    if (!tab) {
      return { error: "No active content tab. Open a site page and try again." };
    }
    await writeLog("info", "Using content tab", { tabId: tab.id, url: tab.url });

    if (action === "updateBook") {
      await writeLog("info", "Extracting list from tab", {
        tabId: tab.id,
        url: tab.url,
        useMobileUA: Boolean(config.useMobileUA)
      });
      const items = await extractListForUpdate(tab, config);
      await writeLog("info", "List extracted", { count: items.length, url: tab.url });
      await postJson(`${config.host}/api/books/${config.bookId}/snapshot`, {
        items
      });
      if (shouldBulk) {
        const results = await bulkCapture(items, config);
        const okCount = results.filter((result) => result.status === "ok").length;
          if (okCount > 0) {
            try {
              await postJson(`${config.host}/api/books/${config.bookId}/issue/build`, {});
              await writeLog("info", "Issue built after bulk capture", {
                count: results.length,
                okCount
              });
              return {
                status: `Snapshot saved. Bulk captured ${results.length} items (${okCount} ok). Issue built.`
              };
            } catch (error) {
              await writeLog("error", "Issue build failed after bulk capture", { error: error.message });
              return {
                status: `Snapshot saved. Bulk captured ${results.length} items (${okCount} ok). Issue build failed: ${error.message}`
              };
            }
          }
        return {
          status: `Snapshot saved. Bulk captured ${results.length} items (0 ok). Issue not built.`
        };
      }
      return { status: `Snapshot saved (${items.length} items).` };
    }

    if (action === "sendArticle") {
      if (config.useMobileUA) {
        const results = await bulkCapture(
          [{ url: tab.url, title: tab.title || tab.url }],
          config
        );
        const first = results[0];
        if (!first || first.status !== "ok") {
          return { error: first?.error || "Article extraction failed" };
        }
        return { status: "Article sent." };
      }
      const article = await captureArticleFromTab(tab.id, ARTICLE_WAIT_OPTIONS);
      if (!article || !article.content_html) {
        return { error: "Article extraction failed" };
      }
      await writeLog("info", "Article payload sizes", {
        url: tab.url,
        htmlLength: article.content_html.length,
        textLength: (article.text_content || "").length
      });
      const contentHtml = await inlineImages(article.content_html, tab.url);
      await postJson(`${config.host}/api/books/${config.bookId}/articles/ingest`, {
        url: tab.url,
        title: article.title,
        byline: article.byline,
        excerpt: article.excerpt,
        content_html: contentHtml,
        source_domain: new URL(tab.url).hostname,
        published_at_raw: article.published_at_raw || null,
        text_content: article.text_content || null,
        section: article.section || null
      });
      await writeLog("info", "Article sent", { url: tab.url });
      return { status: "Article sent." };
    }

    if (action === "buildIssue") {
      await postJson(`${config.host}/api/books/${config.bookId}/issue/build`, {});
      await writeLog("info", "Issue build triggered", { bookId: config.bookId });
      return { status: "Issue build triggered." };
    }

    return { error: "Unknown action" };
  } catch (error) {
    await writeLog("error", "Action failed", { action, error: error.message || String(error) });
    return { error: error.message || String(error) };
  }
};

browser.runtime.onMessage.addListener((message) => {
  if (message?.type === "log" && message.entry) {
    return appendLog(message.entry).then(() => ({ status: "ok" }));
  }
  if (message?.type === "getLogs") {
    return browser.storage.local.get(LOG_KEY).then((stored) => ({
      logs: Array.isArray(stored[LOG_KEY]) ? stored[LOG_KEY] : []
    }));
  }
  if (message?.type === "clearLogs") {
    return browser.storage.local.set({ [LOG_KEY]: [] }).then(() => ({ status: "ok" }));
  }
  const { action, config, bulkCapture: shouldBulk } = message || {};
  return handleAction(action, config, shouldBulk);
});

const actionApi = browser.action || browser.browserAction;
if (actionApi?.onClicked) {
  actionApi.onClicked.addListener(() => {
    void focusOrOpenPanel();
  });
}

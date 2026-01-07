const DEFAULT_MAX_ITEMS = 20;
const DEFAULT_THROTTLE_MS = 1500;
const IMAGE_INLINE_MAX_COUNT = 15;
const IMAGE_INLINE_MAX_BYTES = 2 * 1024 * 1024;
const IMAGE_INLINE_TIMEOUT_MS = 15000;
const MOBILE_UA =
  "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1";
const WSJ_URLS = ["*://*.wsj.com/*"];

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

const buildHeaders = (token) => ({
  "Content-Type": "application/json",
  "X-API-Token": token
});

const postJson = async (url, token, payload) => {
  const response = await fetch(url, {
    method: "POST",
    headers: buildHeaders(token),
    body: JSON.stringify(payload)
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(`${response.status} ${text}`);
  }
  return response.json();
};

const blobToDataUrl = (blob) =>
  new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onloadend = () => resolve(reader.result);
    reader.onerror = () => reject(new Error("Failed to read image"));
    reader.readAsDataURL(blob);
  });

const inlineImages = async (html, baseUrl) => {
  if (typeof DOMParser === "undefined") {
    return html;
  }
  const doc = new DOMParser().parseFromString(html, "text/html");
  const images = Array.from(doc.querySelectorAll("img"));
  let count = 0;
  for (const img of images) {
    if (count >= IMAGE_INLINE_MAX_COUNT) {
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
      if (blob.size > IMAGE_INLINE_MAX_BYTES) {
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

const extractListFromTab = async (tabId) => {
  const response = await browser.tabs.sendMessage(tabId, { action: "extractList" });
  return response?.items || [];
};

const extractListForUpdate = async (tab, config) => {
  if (!config.useMobileUA || !isWsjUrl(tab?.url)) {
    return extractListFromTab(tab.id);
  }
  let tempTab = null;
  let removeListener = () => {};
  try {
    const opened = await openCaptureTab(tab.url, true);
    tempTab = opened.tab;
    removeListener = opened.removeListener;
    await waitForTabLoad(tempTab.id);
    return await extractListFromTab(tempTab.id);
  } finally {
    removeListener();
    if (tempTab?.id) {
      await browser.tabs.remove(tempTab.id);
    }
  }
};

const captureArticleFromTab = async (tabId) => {
  const response = await browser.tabs.sendMessage(tabId, { action: "captureArticle" });
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
  const { host, token, bookId } = config;
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
      const article = await captureArticleFromTab(tab.id);
      if (!article || !article.content_html) {
        throw new Error("Article extraction failed");
      }
      const contentHtml = await inlineImages(article.content_html, item.url);
      await postJson(`${host}/api/books/${bookId}/articles/ingest`, token, {
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

browser.runtime.onMessage.addListener(async (message) => {
  const { action, config, bulkCapture: shouldBulk } = message;
  if (!action) {
    return {};
  }

  try {
    const tabs = await browser.tabs.query({ active: true, currentWindow: true });
    const tab = tabs[0];
    if (!tab) {
      return { error: "No active tab" };
    }

    if (action === "updateBook") {
      const items = await extractListForUpdate(tab, config);
      await postJson(`${config.host}/api/books/${config.bookId}/snapshot`, config.token, {
        items
      });
      if (shouldBulk) {
        const results = await bulkCapture(items, config);
        const okCount = results.filter((result) => result.status === "ok").length;
        if (okCount > 0) {
          try {
            await postJson(`${config.host}/api/books/${config.bookId}/issue/build`, config.token, {});
            return {
              status: `Snapshot saved. Bulk captured ${results.length} items (${okCount} ok). Issue built.`
            };
          } catch (error) {
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
      const article = await captureArticleFromTab(tab.id);
      if (!article || !article.content_html) {
        return { error: "Article extraction failed" };
      }
      const contentHtml = await inlineImages(article.content_html, tab.url);
      await postJson(`${config.host}/api/books/${config.bookId}/articles/ingest`, config.token, {
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
      return { status: "Article sent." };
    }

    if (action === "buildIssue") {
      await postJson(`${config.host}/api/books/${config.bookId}/issue/build`, config.token, {});
      return { status: "Issue build triggered." };
    }

    return { error: "Unknown action" };
  } catch (error) {
    return { error: error.message || String(error) };
  }
});

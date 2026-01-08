const DEFAULT_MAX_ITEMS = 20;
const DEFAULT_THROTTLE_MS = 1500;
const IMAGE_INLINE_MAX_COUNT = 15;
const IMAGE_INLINE_MAX_BYTES = 2 * 1024 * 1024;
const IMAGE_INLINE_TIMEOUT_MS = 15000;
const ARTICLE_WAIT_OPTIONS = {
  timeoutMs: 12000,
  intervalMs: 400,
  minTextLength: 400
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

const extractListFromTab = async (tabId) => {
  return new Promise((resolve, reject) => {
    chrome.tabs.sendMessage(tabId, { action: "extractList" }, (response) => {
      if (chrome.runtime.lastError) {
        reject(new Error(chrome.runtime.lastError.message));
      } else {
        resolve(response?.items || []);
      }
    });
  });
};

const captureArticleFromTab = async (tabId) => {
  return new Promise((resolve, reject) => {
    chrome.tabs.sendMessage(tabId, { action: "captureArticleWait", options: ARTICLE_WAIT_OPTIONS }, (response) => {
      if (chrome.runtime.lastError) {
        reject(new Error(chrome.runtime.lastError.message));
      } else {
        resolve(response?.article);
      }
    });
  });
};

const waitForTabLoad = (tabId) => {
  return new Promise((resolve, reject) => {
    const listener = (updatedTabId, info) => {
      if (updatedTabId === tabId && info.status === "complete") {
        chrome.tabs.onUpdated.removeListener(listener);
        resolve();
      }
    };
    const timeout = setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(listener);
      reject(new Error("Timeout waiting for load"));
    }, 20000);
    chrome.tabs.onUpdated.addListener(listener);
  });
};

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const bulkCapture = async (items, config) => {
  const { host, bookId } = config;
  const results = [];
  const limited = items.slice(0, DEFAULT_MAX_ITEMS);
  for (const item of limited) {
    let tab = null;
    try {
      tab = await chrome.tabs.create({ url: item.url, active: false });
      await waitForTabLoad(tab.id);
      const article = await captureArticleFromTab(tab.id);
      if (!article || !article.content_html) {
        throw new Error("Article extraction failed");
      }
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
      if (tab?.id) {
        await chrome.tabs.remove(tab.id);
      }
    }
    await sleep(DEFAULT_THROTTLE_MS);
  }
  return results;
};

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  const { action, config, bulkCapture: shouldBulk } = message;
  if (!action) {
    return;
  }

  chrome.tabs.query({ active: true, currentWindow: true }, async (tabs) => {
    try {
      const tab = tabs[0];
      if (!tab) {
        sendResponse({ error: "No active tab" });
        return;
      }

      if (action === "updateBook") {
        const items = await extractListFromTab(tab.id);
        await postJson(`${config.host}/api/books/${config.bookId}/snapshot`, {
          items
        });
        if (shouldBulk) {
          const results = await bulkCapture(items, config);
          const okCount = results.filter((result) => result.status === "ok").length;
          if (okCount > 0) {
            try {
              await postJson(`${config.host}/api/books/${config.bookId}/issue/build`, {});
              sendResponse({
                status: `Snapshot saved. Bulk captured ${results.length} items (${okCount} ok). Issue built.`
              });
            } catch (error) {
              sendResponse({
                status: `Snapshot saved. Bulk captured ${results.length} items (${okCount} ok). Issue build failed: ${error.message}`
              });
            }
            return;
          }
          sendResponse({
            status: `Snapshot saved. Bulk captured ${results.length} items (0 ok). Issue not built.`
          });
          return;
        }
        sendResponse({ status: `Snapshot saved (${items.length} items).` });
        return;
      }

      if (action === "sendArticle") {
        const article = await captureArticleFromTab(tab.id);
        if (!article || !article.content_html) {
          sendResponse({ error: "Article extraction failed" });
          return;
        }
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
        sendResponse({ status: "Article sent." });
        return;
      }

      if (action === "buildIssue") {
        await postJson(`${config.host}/api/books/${config.bookId}/issue/build`, {});
        sendResponse({ status: "Issue build triggered." });
        return;
      }

      sendResponse({ error: "Unknown action" });
    } catch (error) {
      sendResponse({ error: error.message });
    }
  });

  return true;
});

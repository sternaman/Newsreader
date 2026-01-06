const DEFAULT_MAX_ITEMS = 20;
const DEFAULT_THROTTLE_MS = 1500;

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
    chrome.tabs.sendMessage(tabId, { action: "captureArticle" }, (response) => {
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
  const { host, token, bookId } = config;
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
      await postJson(`${host}/api/books/${bookId}/articles/ingest`, token, {
        url: item.url,
        title: article.title || item.title,
        byline: article.byline,
        excerpt: article.excerpt,
        content_html: article.content_html,
        source_domain: new URL(item.url).hostname,
        published_at_raw: item.ts || null
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
        await postJson(`${config.host}/api/books/${config.bookId}/snapshot`, config.token, {
          items
        });
        if (shouldBulk) {
          const results = await bulkCapture(items, config);
          sendResponse({ status: `Snapshot saved. Bulk captured ${results.length} items.` });
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
        await postJson(`${config.host}/api/books/${config.bookId}/articles/ingest`, config.token, {
          url: tab.url,
          title: article.title,
          byline: article.byline,
          excerpt: article.excerpt,
          content_html: article.content_html,
          source_domain: new URL(tab.url).hostname
        });
        sendResponse({ status: "Article sent." });
        return;
      }

      if (action === "buildIssue") {
        await postJson(`${config.host}/api/books/${config.bookId}/issue/build`, config.token, {});
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

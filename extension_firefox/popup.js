const statusEl = document.getElementById("status");
const hostInput = document.getElementById("host");
const bookInput = document.getElementById("bookId");
const bulkCheckbox = document.getElementById("bulkCapture");
const mobileCheckbox = document.getElementById("useMobileUA");

const setStatus = (msg) => {
  statusEl.textContent = msg;
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

const getActiveTab = async () => {
  const tabs = await browser.tabs.query({ active: true, currentWindow: true });
  return tabs[0] || null;
};

const fallbackUpdateBook = async (config) => {
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
  await postJson(`${config.host}/api/books/${config.bookId}/issue/build`, {});
  return "Issue build triggered.";
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
  if (action === "updateBook" && !bulkCheckbox.checked && !config.useMobileUA) {
    try {
      const status = await fallbackUpdateBook(config);
      setStatus(status);
    } catch (error) {
      setStatus(error.message || String(error));
    }
    return;
  }
  if (action === "sendArticle" && !config.useMobileUA) {
    try {
      const status = await fallbackSendArticle(config);
      setStatus(status);
    } catch (error) {
      setStatus(error.message || String(error));
    }
    return;
  }
  try {
    const response = await browser.runtime.sendMessage({
      action,
      config,
      bulkCapture: bulkCheckbox.checked
    });
    if (response?.error) {
      setStatus(response.error);
    } else {
      setStatus(response?.status || "Done.");
    }
  } catch (error) {
    const message = error?.message || String(error);
    if (/Receiving end does not exist/i.test(message)) {
      try {
        let status;
        if (action === "buildIssue") {
          status = await fallbackBuildIssue(config);
        } else if (action === "updateBook") {
          status = await fallbackUpdateBook(config);
          if (bulkCheckbox.checked) {
            status = `${status} Bulk capture requires the background script. Reload the add-on.`;
          }
        } else if (action === "sendArticle") {
          if (config.useMobileUA) {
            throw new Error("Mobile capture needs the background script. Reload the add-on or disable the mobile toggle.");
          }
          status = await fallbackSendArticle(config);
        } else {
          throw new Error(message);
        }
        setStatus(status);
        return;
      } catch (fallbackError) {
        setStatus(fallbackError.message || String(fallbackError));
        return;
      }
    }
    setStatus(message);
  }
};

document.getElementById("saveConfig").addEventListener("click", saveConfig);
document.getElementById("updateBook").addEventListener("click", () => sendAction("updateBook"));
document.getElementById("sendArticle").addEventListener("click", () => sendAction("sendArticle"));
document.getElementById("buildIssue").addEventListener("click", () => sendAction("buildIssue"));

loadConfig();

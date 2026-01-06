const statusEl = document.getElementById("status");
const hostInput = document.getElementById("host");
const tokenInput = document.getElementById("token");
const bookInput = document.getElementById("bookId");
const bulkCheckbox = document.getElementById("bulkCapture");

const setStatus = (msg) => {
  statusEl.textContent = msg;
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
    return await browser.storage.sync.get(["host", "token", "bookId", "bulkCapture"]);
  } catch (error) {
    return await browser.storage.local.get(["host", "token", "bookId", "bulkCapture"]);
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
    tokenInput.value = config.token || "changeme";
    bookInput.value = config.bookId || "";
    bulkCheckbox.checked = Boolean(config.bulkCapture);
  } catch (error) {
    setStatus(error.message || String(error));
  }
};

const saveConfig = async () => {
  try {
    await writeConfig({
      host: normalizeHost(hostInput.value),
      token: tokenInput.value.trim(),
      bookId: bookInput.value.trim(),
      bulkCapture: bulkCheckbox.checked
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
      token: tokenInput.value.trim() || stored.token || "changeme",
      bookId: bookInput.value.trim() || stored.bookId || "",
      bulkCapture: bulkCheckbox.checked
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
    setStatus(error.message || String(error));
  }
};

document.getElementById("saveConfig").addEventListener("click", saveConfig);
document.getElementById("updateBook").addEventListener("click", () => sendAction("updateBook"));
document.getElementById("sendArticle").addEventListener("click", () => sendAction("sendArticle"));
document.getElementById("buildIssue").addEventListener("click", () => sendAction("buildIssue"));

loadConfig();

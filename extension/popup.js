const statusEl = document.getElementById("status");
const hostInput = document.getElementById("host");
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

const loadConfig = async () => {
  const config = await chrome.storage.sync.get(["host", "bookId", "bulkCapture"]);
  hostInput.value = config.host || "http://localhost:8000";
  bookInput.value = config.bookId || "";
  bulkCheckbox.checked = Boolean(config.bulkCapture);
};

const saveConfig = async () => {
  await chrome.storage.sync.set({
    host: normalizeHost(hostInput.value),
    bookId: bookInput.value.trim(),
    bulkCapture: bulkCheckbox.checked
  });
  setStatus("Settings saved.");
};

const sendAction = async (action) => {
  const stored = await chrome.storage.sync.get(["host", "bookId"]);
  const config = {
    host: normalizeHost(hostInput.value) || stored.host || "http://localhost:8000",
    bookId: bookInput.value.trim() || stored.bookId || "",
    bulkCapture: bulkCheckbox.checked
  };
  await chrome.storage.sync.set(config);
  if (!config.bookId) {
    setStatus("Set a Book ID first.");
    return;
  }
  chrome.runtime.sendMessage({ action, config, bulkCapture: bulkCheckbox.checked }, (response) => {
    if (chrome.runtime.lastError) {
      setStatus(chrome.runtime.lastError.message);
      return;
    }
    if (response?.error) {
      setStatus(response.error);
    } else {
      setStatus(response?.status || "Done.");
    }
  });
};

document.getElementById("saveConfig").addEventListener("click", saveConfig);
document.getElementById("updateBook").addEventListener("click", () => sendAction("updateBook"));
document.getElementById("sendArticle").addEventListener("click", () => sendAction("sendArticle"));
document.getElementById("buildIssue").addEventListener("click", () => sendAction("buildIssue"));

loadConfig();

const statusEl = document.getElementById("status");
const hostInput = document.getElementById("host");
const tokenInput = document.getElementById("token");
const bookInput = document.getElementById("bookId");
const bulkCheckbox = document.getElementById("bulkCapture");

const setStatus = (msg) => {
  statusEl.textContent = msg;
};

const loadConfig = async () => {
  const config = await browser.storage.sync.get(["host", "token", "bookId", "bulkCapture"]);
  hostInput.value = config.host || "http://localhost:8000";
  tokenInput.value = config.token || "changeme";
  bookInput.value = config.bookId || "";
  bulkCheckbox.checked = Boolean(config.bulkCapture);
};

const saveConfig = async () => {
  await browser.storage.sync.set({
    host: hostInput.value.trim(),
    token: tokenInput.value.trim(),
    bookId: bookInput.value.trim(),
    bulkCapture: bulkCheckbox.checked
  });
  setStatus("Settings saved.");
};

const sendAction = async (action) => {
  const config = await browser.storage.sync.get(["host", "token", "bookId"]);
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

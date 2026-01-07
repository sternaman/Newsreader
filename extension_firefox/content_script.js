const normalizeText = (value) => value.replace(/\s+/g, " ").trim();
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const MIN_ARTICLE_TEXT_LENGTH = 200;

const logEvent = (level, message, data) => {
  try {
    browser.runtime.sendMessage({
      type: "log",
      entry: { ts: new Date().toISOString(), level, message, data }
    });
  } catch (error) {
    // Ignore logging failures in content scripts.
  }
};

const isWsjHost = (hostname) => hostname === "wsj.com" || hostname.endsWith(".wsj.com");

const isLikelyWsjArticleUrl = (url) => {
  if (!isWsjHost(url.hostname)) {
    return false;
  }
  const path = url.pathname.replace(/\/+$/, "");
  if (!path || path === "/") {
    return false;
  }
  if (path.startsWith("/video") || path.startsWith("/podcasts") || path.startsWith("/livecoverage")) {
    return false;
  }
  const mod = url.searchParams.get("mod");
  if (mod && mod.startsWith("nav")) {
    return false;
  }
  if (path.includes("/articles/")) {
    return true;
  }
  const last = path.split("/").filter(Boolean).pop() || "";
  if (last.length < 12 || last.split("-").length < 3) {
    return false;
  }
  if (/news|markets|opinion|personal-finance|real-estate|lifestyle|business|world|economy|tech|arts|sports|science|us/i.test(last)) {
    return false;
  }
  return true;
};

const extractWsjFrontPageItems = () => {
  const main = document.querySelector("main") || document.body;
  const links = Array.from(main.querySelectorAll("a[href]"));
  const seen = new Set();
  const items = [];
  for (const link of links) {
    if (link.closest("nav, header, footer, aside")) {
      continue;
    }
    const href = link.getAttribute("href");
    if (!href) {
      continue;
    }
    let url;
    try {
      url = new URL(href, window.location.href);
    } catch (error) {
      continue;
    }
    if (!isLikelyWsjArticleUrl(url)) {
      continue;
    }
    const title = normalizeText(link.textContent || "");
    if (title.length < 20) {
      continue;
    }
    const key = `${url.origin}${url.pathname}`;
    if (seen.has(key)) {
      continue;
    }
    seen.add(key);
    items.push({ title, url: url.toString() });
  }
  return items;
};

const extractListItems = (options = {}) => {
  const { log = true } = options;
  if (isWsjHost(window.location.hostname)) {
    const wsjItems = extractWsjFrontPageItems();
    if (wsjItems.length > 0) {
      if (log) {
        logEvent("info", "WSJ list extracted", { count: wsjItems.length });
      }
      return wsjItems;
    }
  }
  const links = Array.from(document.querySelectorAll("a"));
  const items = links
    .filter((link) => link.href && normalizeText(link.textContent || "").length > 8)
    .slice(0, 100)
    .map((link) => ({
      title: normalizeText(link.textContent),
      url: link.href
    }));
  if (log) {
    logEvent("info", "Generic list extracted", { count: items.length });
  }
  return items;
};

const extractListWithWait = async (options = {}) => {
  const timeoutMs = options.timeoutMs || 8000;
  const intervalMs = options.intervalMs || 400;
  const start = Date.now();
  let items = extractListItems({ log: false });
  if (items.length > 0) {
    logEvent("info", "List extracted after wait", { count: items.length, waitedMs: 0 });
    return items;
  }
  while (Date.now() - start < timeoutMs) {
    await sleep(intervalMs);
    items = extractListItems({ log: false });
    if (items.length > 0) {
      break;
    }
  }
  logEvent("info", "List extracted after wait", {
    count: items.length,
    waitedMs: Date.now() - start
  });
  return items;
};

const extractMetaContent = (selectors) => {
  for (const selector of selectors) {
    const el = document.querySelector(selector);
    if (el && el.content) {
      const value = el.content.trim();
      if (value) {
        return value;
      }
    }
  }
  return null;
};

const extractSection = () =>
  extractMetaContent([
    "meta[property='article:section']",
    "meta[name='article:section']",
    "meta[name='section']",
    "meta[property='section']",
    "meta[name='parsely-section']",
    "meta[name='dc.subject']"
  ]);

const extractPublishedAtRaw = () =>
  extractMetaContent([
    "meta[property='article:published_time']",
    "meta[name='article:published_time']",
    "meta[name='pubdate']",
    "meta[name='publishdate']",
    "meta[name='timestamp']",
    "meta[property='og:pubdate']",
    "meta[name='date']",
    "meta[name='dc.date']",
    "meta[name='parsely-pub-date']",
    "meta[name='sailthru.date']"
  ]);

const cleanupArticleHtml = (html) => {
  if (typeof DOMParser === "undefined") {
    return html;
  }
  const doc = new DOMParser().parseFromString(html, "text/html");
  const MAX_JUNK_LENGTH = 300;
  const junkPatterns = [
    /skip to main content/i,
    /this copy is for your personal, non-commercial use only/i,
    /subscriber agreement/i,
    /dow jones reprints/i,
    /djreprints\.com/i,
    /1-800-843-0008/i,
    /what to read next/i,
    /most popular/i,
    /recommended videos/i
  ];
  const selectors = [
    "header",
    "nav",
    "footer",
    "aside",
    "script",
    "style",
    "noscript",
    "svg",
    "form",
    "[aria-label*='Advertisement']",
    "[aria-label*='ad']",
    "[class*='ad-']",
    "[class*='advert']",
    "[class*='promo']",
    "[class*='subscribe']",
    "[class*='newsletter']",
    "[class*='related']",
    "[class*='recommend']",
    "[class*='share']",
    "[class*='social']",
    "[class*='comment']",
    "[id*='ad']",
    "[id*='promo']",
    "[id*='footer']",
    "[id*='header']",
    "[id*='nav']"
  ];
  doc.querySelectorAll(selectors.join(",")).forEach((el) => el.remove());
  doc.querySelectorAll("p, div, span, li").forEach((el) => {
    const text = normalizeText(el.textContent || "");
    if (!text) {
      return;
    }
    if (text.length > MAX_JUNK_LENGTH) {
      return;
    }
    for (const pattern of junkPatterns) {
      if (pattern.test(text)) {
        el.remove();
        break;
      }
    }
  });
  doc.querySelectorAll("a[href*='/market-data/quotes']").forEach((el) => el.remove());
  doc.querySelectorAll("h1").forEach((el) => el.remove());
  doc.querySelectorAll("a").forEach((link) => {
    const text = normalizeText(link.textContent || "");
    if (text.toLowerCase().startsWith("skip to")) {
      link.remove();
    }
  });
  return doc.body ? doc.body.innerHTML : html;
};

const fallbackContentHtml = () => {
  const node =
    document.querySelector("article") ||
    document.querySelector("main") ||
    document.body ||
    document.documentElement;
  return node ? node.innerHTML : document.documentElement.outerHTML;
};

const extractArticle = () => {
  const section = extractSection();
  const publishedAtRaw = extractPublishedAtRaw();
  try {
    const clone = document.cloneNode(true);
    const reader = new Readability(clone);
    const article = reader.parse();
    if (article && article.content) {
      const textContent = article.textContent || document.body?.innerText || null;
      let cleanedHtml = cleanupArticleHtml(article.content);
      let cleanedTextLength = 0;
      try {
        const parsed = new DOMParser().parseFromString(cleanedHtml, "text/html");
        cleanedTextLength = normalizeText(parsed.body?.innerText || "").length;
      } catch (error) {
        cleanedTextLength = 0;
      }
      if (cleanedTextLength < MIN_ARTICLE_TEXT_LENGTH) {
        logEvent("warn", "Readability content too short, using fallback HTML", {
          cleanedTextLength
        });
        cleanedHtml = cleanupArticleHtml(fallbackContentHtml());
      }
      return {
        title: article.title || document.title,
        byline: article.byline,
        excerpt: article.excerpt,
        content_html: cleanedHtml,
        text_content: textContent,
        section,
        published_at_raw: publishedAtRaw
      };
    }
  } catch (error) {
    logEvent("error", "Readability failed", { error: error.message || String(error) });
    console.warn("Readability failed", error);
  }
  logEvent("info", "Fallback HTML used", { url: window.location.href });
  const fallbackHtml = cleanupArticleHtml(fallbackContentHtml());
  return {
    title: document.title,
    byline: null,
    excerpt: null,
    content_html: fallbackHtml,
    text_content: document.body?.innerText || null,
    section,
    published_at_raw: publishedAtRaw
  };
};

const getArticleTextLength = () => {
  const node =
    document.querySelector("article") ||
    document.querySelector("main") ||
    document.body ||
    document.documentElement;
  const text = normalizeText(node?.innerText || "");
  return text.length;
};

const waitForArticleContent = async (options = {}) => {
  const timeoutMs = options.timeoutMs || 12000;
  const intervalMs = options.intervalMs || 400;
  const minTextLength = options.minTextLength || 400;
  const start = Date.now();
  let length = getArticleTextLength();
  while (length < minTextLength && Date.now() - start < timeoutMs) {
    await sleep(intervalMs);
    length = getArticleTextLength();
  }
  return {
    waitedMs: Date.now() - start,
    textLength: length,
    minTextLength
  };
};

const extractArticleWait = async (options = {}) => {
  const result = await waitForArticleContent(options);
  logEvent("info", "Article wait complete", result);
  return extractArticle();
};

browser.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.action === "extractList") {
    logEvent("info", "Extract list requested", {
      url: window.location.href,
      readyState: document.readyState,
      linkCount: document.querySelectorAll("a[href]").length
    });
    sendResponse({ items: extractListItems() });
    return;
  }
  if (message.action === "extractListWait") {
    logEvent("info", "Extract list (wait) requested", {
      url: window.location.href,
      readyState: document.readyState,
      linkCount: document.querySelectorAll("a[href]").length
    });
    extractListWithWait(message.options || {})
      .then((items) => sendResponse({ items }))
      .catch((error) => sendResponse({ error: error.message || String(error) }));
    return true;
  }
  if (message.action === "captureArticle") {
    sendResponse({ article: extractArticle() });
    return;
  }
  if (message.action === "captureArticleWait") {
    extractArticleWait(message.options || {})
      .then((article) => sendResponse({ article }))
      .catch((error) => sendResponse({ error: error.message || String(error) }));
    return true;
  }
});

const normalizeText = (value) => value.replace(/\s+/g, " ").trim();
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const MIN_ARTICLE_TEXT_LENGTH = 200;
const MAX_IMAGE_WIDTH = 600;
const WSJ_MARKET_TOKENS = new Set([
  "Select",
  "DJIA",
  "S&P 500",
  "Nasdaq",
  "Russell 2000",
  "U.S. 10 Yr",
  "VIX",
  "Gold",
  "Bitcoin",
  "Crude Oil",
  "Dollar Index",
  "KBW Nasdaq Bank Index",
  "S&P GSCI Index Spot"
]);
const WSJ_MENU_INDICATORS = [
  "The Wall Street Journal",
  "English Edition",
  "Print Edition",
  "Latest Headlines",
  "Puzzles",
  "More"
];
const BLOOMBERG_MENU_INDICATORS = [
  "Bloomberg",
  "Markets",
  "Technology",
  "Politics",
  "Businessweek",
  "Wealth",
  "Pursuits",
  "Opinion",
  "Green",
  "Industries",
  "Economics"
];

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
const isBloombergHost = (hostname) =>
  hostname === "bloomberg.com" ||
  hostname.endsWith(".bloomberg.com") ||
  hostname === "businessweek.com" ||
  hostname.endsWith(".businessweek.com");

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
  const last = path.split("/").filter(Boolean).pop() || "";
  if (!/-[0-9a-f]{8}$/i.test(last)) {
    return false;
  }
  if (
    /latest-headlines|print-edition|livecoverage|live-coverage|journalcollection|buy-side|video|podcasts/i.test(last)
  ) {
    return false;
  }
  if (
    /news|markets|opinion|personal-finance|real-estate|lifestyle|business|world|economy|tech|arts|sports|science|us/i.test(last)
  ) {
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

const extractMetaContent = (selectors, doc = document) => {
  for (const selector of selectors) {
    const el = doc.querySelector(selector);
    if (el && el.content) {
      const value = el.content.trim();
      if (value) {
        return value;
      }
    }
  }
  return null;
};

const extractSection = (doc) =>
  extractMetaContent([
    "meta[property='article:section']",
    "meta[name='article:section']",
    "meta[name='section']",
    "meta[property='section']",
    "meta[name='parsely-section']",
    "meta[name='dc.subject']"
  ], doc);

const extractByline = (doc) =>
  extractMetaContent([
    "meta[name='author']",
    "meta[property='author']",
    "meta[property='article:author']",
    "meta[name='parsely-author']",
    "meta[name='sailthru.author']",
    "meta[name='dc.creator']",
    "meta[name='byl']"
  ], doc);

const extractPublishedAtRaw = (doc) =>
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
  ], doc);

const parseSrcsetBestUrl = (srcset) => {
  if (!srcset) {
    return null;
  }
  const entries = srcset
    .split(",")
    .map((entry) => entry.trim())
    .filter(Boolean);
  if (!entries.length) {
    return null;
  }
  let bestUrl = null;
  let bestScore = -1;
  for (const entry of entries) {
    const parts = entry.split(/\s+/).filter(Boolean);
    const url = parts[0];
    const descriptor = parts[1] || "";
    let score = 0;
    if (descriptor.endsWith("w")) {
      score = parseInt(descriptor.replace("w", ""), 10) || 0;
    } else if (descriptor.endsWith("x")) {
      score = Math.round((parseFloat(descriptor.replace("x", "")) || 0) * 100);
    }
    if (score >= bestScore) {
      bestScore = score;
      bestUrl = url;
    }
  }
  return bestUrl || entries[entries.length - 1].split(/\s+/)[0];
};

const getAmpUrl = (doc = document) => {
  const link = doc.querySelector("link[rel='amphtml']");
  if (link && link.href) {
    return link.href;
  }
  const canonical = doc.querySelector("link[rel='canonical']");
  if (canonical && canonical.href && canonical.href.includes("/amp/")) {
    return canonical.href;
  }
  return null;
};

const normalizeAmpDocument = (doc) => {
  if (!doc) {
    return;
  }
  const removeTags = ["amp-iframe", "amp-video", "amp-ad", "amp-analytics", "amp-social-share"];
  removeTags.forEach((tag) => doc.querySelectorAll(tag).forEach((node) => node.remove()));
  doc.querySelectorAll("amp-img").forEach((node) => {
    const img = doc.createElement("img");
    const srcset = node.getAttribute("data-srcset") || node.getAttribute("srcset");
    const srcsetUrl = parseSrcsetBestUrl(srcset);
    const src = srcsetUrl || node.getAttribute("data-src") || node.getAttribute("src");
    if (src) {
      img.setAttribute("src", src);
    }
    const alt = node.getAttribute("alt");
    if (alt) {
      img.setAttribute("alt", alt);
    }
    node.replaceWith(img);
  });
};

const fetchAmpDocument = async (ampUrl) => {
  if (!ampUrl) {
    return null;
  }
  try {
    const response = await fetch(ampUrl, { credentials: "include" });
    if (!response.ok) {
      return null;
    }
    const text = await response.text();
    const doc = new DOMParser().parseFromString(text, "text/html");
    normalizeAmpDocument(doc);
    return doc;
  } catch (error) {
    logEvent("warn", "AMP fetch failed", { url: ampUrl, error: error.message || String(error) });
    return null;
  }
};

const pickPictureSource = (img) => {
  const picture = img.closest("picture");
  if (!picture) {
    return null;
  }
  const sources = Array.from(picture.querySelectorAll("source"));
  for (const source of sources) {
    const srcset = source.getAttribute("data-srcset") || source.getAttribute("srcset");
    const srcsetUrl = parseSrcsetBestUrl(srcset);
    if (srcsetUrl) {
      return srcsetUrl;
    }
    const src = source.getAttribute("data-src") || source.getAttribute("src");
    if (src) {
      return src;
    }
  }
  return null;
};

const pickAncestorSource = (img) => {
  const attrs = [
    "data-src",
    "data-original",
    "data-image",
    "data-img",
    "data-lazy-src",
    "data-full-src"
  ];
  let node = img.parentElement;
  for (let depth = 0; depth < 3 && node; depth += 1) {
    for (const attr of attrs) {
      const value = node.getAttribute(attr);
      if (value) {
        return value;
      }
    }
    node = node.parentElement;
  }
  return null;
};

const pickImageSource = (img) => {
  const attrs = [
    "currentsourceurl",
    "currentSourceUrl",
    "currentSourceURL",
    "data-native-src",
    "data-src",
    "data-lazy-src",
    "data-original",
    "data-hires",
    "data-full-src",
    "data-large-src",
    "data-image",
    "data-img",
    "data-attr-src"
  ];
  for (const attr of attrs) {
    const value = img.getAttribute(attr);
    if (value) {
      return value;
    }
  }
  const pictureUrl = pickPictureSource(img);
  if (pictureUrl) {
    return pictureUrl;
  }
  const ancestorUrl = pickAncestorSource(img);
  if (ancestorUrl) {
    return ancestorUrl;
  }
  const srcset = img.getAttribute("data-srcset") || img.getAttribute("srcset");
  const srcsetUrl = parseSrcsetBestUrl(srcset);
  if (srcsetUrl) {
    return srcsetUrl;
  }
  return img.currentSrc || img.getAttribute("src") || img.src || null;
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

const normalizeImageUrl = (src, baseUrl) => {
  if (!src) {
    return null;
  }
  let raw = src.trim();
  if (!raw) {
    return null;
  }
  if (raw.startsWith("data:")) {
    return raw;
  }
  if (raw.startsWith("blob:")) {
    return null;
  }
  if (raw.startsWith("//")) {
    raw = `https:${raw}`;
  }
  let url;
  try {
    url = new URL(raw, baseUrl);
  } catch (error) {
    return null;
  }
  if (isPlaceholderImage(url.toString())) {
    return null;
  }
  if (url.hostname.endsWith("wsj.net") || url.hostname.endsWith("wsj.com")) {
    if (/\/im-\d+\/OR\/?$/.test(url.pathname)) {
      url.pathname = url.pathname.replace(/\/OR\/?$/, "");
    }
    const widthParam = url.searchParams.get("width") || url.searchParams.get("w");
    if (widthParam) {
      const widthValue = parseInt(widthParam, 10);
      if (widthValue && widthValue > MAX_IMAGE_WIDTH) {
        url.searchParams.set("width", String(MAX_IMAGE_WIDTH));
        url.searchParams.delete("w");
      }
    } else {
      url.searchParams.set("width", String(MAX_IMAGE_WIDTH));
    }
  }
  if (/-1x-1\.(jpg|jpeg|png|webp)$/i.test(url.pathname)) {
    url.pathname = url.pathname.replace(
      /-1x-1\.(jpg|jpeg|png|webp)$/i,
      `-${MAX_IMAGE_WIDTH}x-1.$1`
    );
  }
  const genericWidth = url.searchParams.get("width");
  if (genericWidth) {
    const numeric = parseInt(genericWidth, 10);
    if (numeric && numeric > 1200) {
      url.searchParams.set("width", String(MAX_IMAGE_WIDTH));
    }
  }
  return url.toString();
};

const normalizeImages = (doc, baseUrl) => {
  doc.querySelectorAll("img").forEach((img) => {
    const src = pickImageSource(img);
    const normalized = normalizeImageUrl(src, baseUrl);
    if (!normalized) {
      img.remove();
      return;
    }
    img.setAttribute("src", normalized);
    ["srcset", "data-srcset", "sizes", "width", "height", "style", "loading", "decoding"].forEach(
      (attr) => img.removeAttribute(attr)
    );
  });
  doc.querySelectorAll("figure").forEach((figure) => {
    if (!figure.querySelector("img")) {
      figure.remove();
    }
  });
};

const looksLikeNameToken = (text) => {
  if (!text || text.length > 60) {
    return false;
  }
  if (/\d/.test(text)) {
    return false;
  }
  if (!/^[A-Za-z'\\-\\.\\s,]+$/.test(text)) {
    return false;
  }
  const words = text
    .replace(/,/g, " ")
    .trim()
    .split(/\s+/)
    .filter(Boolean);
  if (!words.length || words.length > 6) {
    return false;
  }
  return words.every((word) => /^[A-Z]/.test(word));
};

const stripLeadingBylineBlocks = (doc, bylineValue) => {
  if (!doc.body) {
    return;
  }
  const normalizedByline = bylineValue ? normalizeText(bylineValue) : "";
  const blocks = Array.from(doc.body.querySelectorAll("p, div, section"));
  let started = false;
  for (let i = 0; i < Math.min(blocks.length, 14); i += 1) {
    const block = blocks[i];
    const text = normalizeText(block.textContent || "");
    if (!text) {
      continue;
    }
    const lower = text.toLowerCase();
    if (!started) {
      if (lower === "by" || lower.startsWith("by ")) {
        started = true;
        block.remove();
        continue;
      }
      if (normalizedByline && (text === normalizedByline || text === `By ${normalizedByline}`)) {
        started = true;
        block.remove();
        continue;
      }
      continue;
    }
    if (lower === "and" || lower === "," || lower === "&") {
      block.remove();
      continue;
    }
    if (text.replace(/\s+/g, "") === "***") {
      block.remove();
      continue;
    }
    if (looksLikeNameToken(text)) {
      block.remove();
      continue;
    }
    break;
  }
};

const removeUrlOnlyBlocks = (doc) => {
  doc.querySelectorAll("p, div, section, li").forEach((el) => {
    const text = normalizeText(el.textContent || "");
    if (/^https?:\/\/\S+$/i.test(text)) {
      el.remove();
    }
  });
};

const cleanupArticleHtml = (html, baseUrl = window.location.href, options = {}) => {
  if (typeof DOMParser === "undefined") {
    return html;
  }
  const doc = new DOMParser().parseFromString(html, "text/html");
  const { byline } = options;
  const MAX_JUNK_LENGTH = 300;
  const cssTextPatterns = [
    /\/\*\s*theme vars/i,
    /--colors-/i,
    /--space-presets-/i,
    /--typography-presets-/i,
    /:host\s*\{/i
  ];
  const junkPatterns = [
    /skip to main content/i,
    /this copy is for your personal, non-commercial use only/i,
    /distribution and use of this material are governed by our subscriber agreement/i,
    /for non-personal use or to order multiple copies/i,
    /subscriber agreement/i,
    /dow jones reprints/i,
    /djreprints\.com/i,
    /1-800-843-0008/i,
    /gift unlocked/i,
    /listen to article/i,
    /^listen$/i,
    /^share$/i,
    /^resize$/i,
    /^print$/i,
    /sponsored offers/i,
    /utility bar/i,
    /conversation/i,
    /what to read next/i,
    /most popular/i,
    /recommended videos/i,
    /videos most popular/i,
    /most popular news/i,
    /further reading/i,
    /show conversation/i,
    /advertisement/i,
    /coverage and analysis/i,
    /navigating the markets/i,
    /^write to\b/i
  ];
  const bloombergJunkPatterns = [
    /^bloomberg$/i,
    /^bloomberg businessweek$/i,
    /^bloomberg news$/i,
    /^businessweek$/i,
    /^most read$/i,
    /^most popular$/i,
    /^related stories$/i,
    /^more from bloomberg$/i,
    /^more from businessweek$/i,
    /^recommended/i,
    /^read more$/i,
    /^read next$/i,
    /^continue reading$/i,
    /^sign up/i,
    /^subscribe/i,
    /^newsletter/i,
    /^get the/i,
    /bloomberg l\.p\./i
  ];
  const selectors = [
    "nav",
    "footer",
    "aside",
    "script",
    "style",
    "noscript",
    "svg",
    "form",
    "[data-testid='ad-container']",
    "[data-spotim-app]",
    "[data-spot-im-class]",
    "[aria-label*='Advertisement']",
    "[aria-label*='ad']",
    "[aria-label*='Sponsored']",
    "[aria-label*='Listen To Article']",
    "[aria-label*='What to Read Next']",
    "[aria-label*='Utility Bar']",
    "[aria-label*='Conversation']",
    "[aria-label*='Comment']",
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
    "[id*='nav']"
  ];
  const bloombergSelectors = [
    "[data-tracking-type*='related']",
    "[data-tracking-type*='recommend']",
    "[data-tracking-type*='newsletter']",
    "[data-component*='ad']"
  ];
  let isWsj = false;
  let isBloomberg = false;
  try {
    const hostname = new URL(baseUrl).hostname;
    isWsj = isWsjHost(hostname);
    isBloomberg = isBloombergHost(hostname);
  } catch (error) {
    isWsj = false;
    isBloomberg = false;
  }
  const isMarketValue = (text) =>
    /^[-+]?\d{1,3}(?:,\d{3})*(?:\.\d+)?%?$/.test(text) || /^\d+\/\d+$/.test(text);
  const marketHintText = (el) => {
    const parent = el.parentElement || el;
    const parentText = normalizeText(parent.textContent || "");
    return Array.from(WSJ_MARKET_TOKENS).some((token) => parentText.includes(token));
  };
  const hasMenuIndicators = (text) => {
    let hits = 0;
    for (const token of WSJ_MENU_INDICATORS) {
      if (text.includes(token)) {
        hits += 1;
      }
      if (hits >= 2) {
        return true;
      }
    }
    return false;
  };
  const hasBloombergMenuIndicators = (text) => {
    let hits = 0;
    for (const token of BLOOMBERG_MENU_INDICATORS) {
      if (text.includes(token)) {
        hits += 1;
      }
      if (hits >= 5) {
        return true;
      }
    }
    return false;
  };
  const activeSelectors = isBloomberg ? selectors.concat(bloombergSelectors) : selectors;
  const activeJunkPatterns = isBloomberg ? junkPatterns.concat(bloombergJunkPatterns) : junkPatterns;
  doc.querySelectorAll(activeSelectors.join(",")).forEach((el) => el.remove());
  doc.querySelectorAll("header").forEach((el) => {
    const text = normalizeText(el.textContent || "");
    const linkCount = el.querySelectorAll("a").length;
    if (
      linkCount >= 4 ||
      /skip to|subscribe|sign in|log in|account|customer service/i.test(text)
    ) {
      el.remove();
    }
  });
  doc.querySelectorAll("p, div, span, li").forEach((el) => {
    const text = normalizeText(el.textContent || "");
    if (!text) {
      return;
    }
    if (isWsj) {
      if (WSJ_MARKET_TOKENS.has(text)) {
        el.remove();
        return;
      }
      if (isMarketValue(text) && marketHintText(el)) {
        el.remove();
        return;
      }
      if (hasMenuIndicators(text)) {
        el.remove();
        return;
      }
    }
    if (isBloomberg) {
      if (hasBloombergMenuIndicators(text)) {
        el.remove();
        return;
      }
    }
    if (cssTextPatterns.some((pattern) => pattern.test(text))) {
      el.remove();
      return;
    }
    if (text.length > MAX_JUNK_LENGTH) {
      return;
    }
    for (const pattern of activeJunkPatterns) {
      if (pattern.test(text)) {
        el.remove();
        break;
      }
    }
  });
  doc.querySelectorAll("p").forEach((el) => {
    const link = el.querySelector("a[href]");
    if (!link) {
      return;
    }
    const text = normalizeText(el.textContent || "");
    if (/^https?:\/\//i.test(text) && text.length <= 200) {
      el.remove();
      return;
    }
    if (text === link.href) {
      el.remove();
    }
  });
  doc.querySelectorAll("a[href]").forEach((link) => {
    const text = normalizeText(link.textContent || "");
    if (!/^https?:\/\//i.test(text)) {
      return;
    }
    if (link.href !== text) {
      return;
    }
    const parent = link.closest("p, div, section, li");
    if (parent && normalizeText(parent.textContent || "") === text) {
      parent.remove();
    } else {
      link.remove();
    }
  });
  removeUrlOnlyBlocks(doc);
  if (isWsj) {
    stripLeadingBylineBlocks(doc, byline);
  }
  doc.querySelectorAll("a[href*='/market-data/quotes']").forEach((el) => el.remove());
  doc.querySelectorAll("h1").forEach((el) => el.remove());
  doc.querySelectorAll("a").forEach((link) => {
    const text = normalizeText(link.textContent || "");
    if (text.toLowerCase().startsWith("skip to")) {
      link.remove();
    }
  });
  const truncateAfterBlockMatch = (patterns, requiredText) => {
    if (!doc.body) {
      return;
    }
    const blocks = Array.from(doc.body.querySelectorAll("p, div, section, li, h3, h4"));
    for (const block of blocks) {
      const text = normalizeText(block.textContent || "");
      if (!text) {
        continue;
      }
      if (requiredText && !text.toLowerCase().includes(requiredText.toLowerCase())) {
        continue;
      }
      if (!patterns.some((pattern) => pattern.test(text))) {
        continue;
      }
      let node = block;
      while (node) {
        const next = node.nextSibling;
        node.remove();
        node = next;
      }
      break;
    }
  };
  if (isWsj) {
    truncateAfterBlockMatch([/^write to\b/i], "@wsj.com");
    truncateAfterBlockMatch(
      [/^videos\b/i, /^most popular/i, /^further reading/i, /^show conversation/i, /^advertisement/i],
      null
    );
  }
  if (isBloomberg) {
    truncateAfterBlockMatch(
      [/^most read/i, /^most popular/i, /^related stories/i, /^more from/i, /^recommended/i],
      null
    );
  }
  normalizeImages(doc, baseUrl);
  return doc.body ? doc.body.innerHTML : html;
};

const fallbackContentHtml = (doc) => {
  const node =
    doc.querySelector("article") ||
    doc.querySelector("main") ||
    doc.body ||
    doc.documentElement;
  return node ? node.innerHTML : doc.documentElement.outerHTML;
};

const extractArticleFromDocument = (doc, baseUrl) => {
  const section = extractSection(doc);
  const metaByline = extractByline(doc);
  const publishedAtRaw = extractPublishedAtRaw(doc);
  try {
    const clone = doc.cloneNode(true);
    const reader = new Readability(clone);
    const article = reader.parse();
    if (article && article.content) {
      const textContent = article.textContent || doc.body?.innerText || null;
      const byline = article.byline || metaByline;
      const normalizedByline = byline ? normalizeText(byline) : null;
      let cleanedHtml = cleanupArticleHtml(article.content, baseUrl, {
        byline: normalizedByline
      });
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
        cleanedHtml = cleanupArticleHtml(fallbackContentHtml(doc), baseUrl, {
          byline: normalizedByline
        });
      }
      return {
        title: article.title || doc.title,
        byline: normalizedByline,
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
  logEvent("info", "Fallback HTML used", { url: baseUrl });
  const fallbackByline = metaByline ? normalizeText(metaByline) : null;
  const fallbackHtml = cleanupArticleHtml(fallbackContentHtml(doc), baseUrl, {
    byline: fallbackByline
  });
  return {
    title: doc.title,
    byline: fallbackByline,
    excerpt: null,
    content_html: fallbackHtml,
    text_content: doc.body?.innerText || null,
    section,
    published_at_raw: publishedAtRaw
  };
};

const extractArticleWithAmp = async () => {
  const baseUrl = window.location.href;
  let hostname = "";
  try {
    hostname = new URL(baseUrl).hostname;
  } catch (error) {
    hostname = "";
  }
  if (hostname && (isWsjHost(hostname) || isBloombergHost(hostname))) {
    const ampUrl = getAmpUrl(document);
    if (ampUrl) {
      const ampDoc = await fetchAmpDocument(ampUrl);
      if (ampDoc) {
        const ampArticle = extractArticleFromDocument(ampDoc, ampUrl);
        const textLength = normalizeText(ampArticle?.text_content || "").length;
        if (textLength >= MIN_ARTICLE_TEXT_LENGTH) {
          logEvent("info", "AMP extraction used", { url: ampUrl, textLength });
          return ampArticle;
        }
      }
    }
  }
  return extractArticleFromDocument(document, baseUrl);
};

const extractArticle = () => extractArticleFromDocument(document, window.location.href);

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
  return await extractArticleWithAmp();
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

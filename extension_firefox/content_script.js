const extractListItems = () => {
  const links = Array.from(document.querySelectorAll("a"));
  const items = links
    .filter((link) => link.href && link.textContent.trim().length > 8)
    .slice(0, 100)
    .map((link) => ({
      title: link.textContent.trim(),
      url: link.href
    }));
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

const extractArticle = () => {
  const section = extractSection();
  const publishedAtRaw = extractPublishedAtRaw();
  try {
    const clone = document.cloneNode(true);
    const reader = new Readability(clone);
    const article = reader.parse();
    if (article && article.content) {
      const textContent = article.textContent || document.body?.innerText || null;
      return {
        title: article.title || document.title,
        byline: article.byline,
        excerpt: article.excerpt,
        content_html: article.content,
        text_content: textContent,
        section,
        published_at_raw: publishedAtRaw
      };
    }
  } catch (error) {
    console.warn("Readability failed", error);
  }
  return {
    title: document.title,
    byline: null,
    excerpt: null,
    content_html: document.documentElement.outerHTML,
    text_content: document.body?.innerText || null,
    section,
    published_at_raw: publishedAtRaw
  };
};

browser.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.action === "extractList") {
    sendResponse({ items: extractListItems() });
  }
  if (message.action === "captureArticle") {
    sendResponse({ article: extractArticle() });
  }
});

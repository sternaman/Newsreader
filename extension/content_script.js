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

const extractArticle = () => {
  try {
    const clone = document.cloneNode(true);
    const reader = new Readability(clone);
    const article = reader.parse();
    if (article && article.content) {
      return {
        title: article.title || document.title,
        byline: article.byline,
        excerpt: article.excerpt,
        content_html: article.content
      };
    }
  } catch (error) {
    console.warn("Readability failed", error);
  }
  return {
    title: document.title,
    byline: null,
    excerpt: null,
    content_html: document.documentElement.outerHTML
  };
};

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.action === "extractList") {
    sendResponse({ items: extractListItems() });
  }
  if (message.action === "captureArticle") {
    sendResponse({ article: extractArticle() });
  }
});

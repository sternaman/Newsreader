class Readability {
  constructor(doc) {
    this._doc = doc;
  }

  _getCandidates() {
    const selectors = ["article", "main", "section", "div"];
    const nodes = [];
    selectors.forEach((selector) => {
      this._doc.querySelectorAll(selector).forEach((el) => nodes.push(el));
    });
    return nodes;
  }

  _scoreNode(node) {
    const text = node.textContent || "";
    return text.replace(/\s+/g, " ").trim().length;
  }

  parse() {
    const candidates = this._getCandidates();
    let bestNode = null;
    let bestScore = 0;
    candidates.forEach((node) => {
      const score = this._scoreNode(node);
      if (score > bestScore) {
        bestScore = score;
        bestNode = node;
      }
    });

    if (!bestNode) {
      bestNode = this._doc.body;
    }

    const textContent = bestNode.textContent || "";
    const excerpt = textContent.replace(/\s+/g, " ").trim().slice(0, 180);

    return {
      title: this._doc.title,
      byline: null,
      excerpt,
      content: bestNode.innerHTML
    };
  }
}

if (typeof module !== "undefined") {
  module.exports = Readability;
}

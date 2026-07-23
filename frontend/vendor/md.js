/* md.js — minimal, safe-ish markdown renderer (zero-build, no dependencies).
 *
 * Escapes HTML first, then applies a small subset of Markdown:
 * fenced code blocks, inline code, bold, italic, headings, bullet lists,
 * blockquotes, and links. Sufficient for chat/agent output in an MVP.
 *
 * NOT a full CommonMark implementation; deliberately tiny to avoid an
 * external vendored bundle. For richer rendering, swap in marked.min.js later.
 */
(function (global) {
  "use strict";

  function escapeHtml(s) {
    return s.replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function inline(s) {
    // inline code
    s = s.replace(/`([^`]+)`/g, function (_, c) { return "<code>" + escapeHtml(c) + "</code>"; });
    // links [text](url) — restrict to http(s)/mailto
    s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+|mailto:[^\s)]+)\)/g,
      function (_, t, u) { return '<a href="' + u + '" rel="noopener noreferrer" target="_blank">' + escapeHtml(t) + "</a>"; });
    // bold then italic
    s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/(^|[^*])\*([^*]+)\*/g, "$1<em>$2</em>");
    return s;
  }

  function render(md) {
    if (!md) return "";
    var src = String(md).replace(/\r\n/g, "\n").split("\n");
    var out = [];
    var i = 0;
    while (i < src.length) {
      var line = src[i];

      // fenced code block
      var fence = line.match(/^```(\w*)\s*$/);
      if (fence) {
        var lang = fence[1] || "";
        var code = [];
        i++;
        while (i < src.length && !/^```\s*$/.test(src[i])) { code.push(src[i]); i++; }
        i++; // skip closing fence
        out.push('<pre><code class="lang-' + escapeHtml(lang) + '">' + escapeHtml(code.join("\n")) + "</code></pre>");
        continue;
      }

      // heading
      var h = line.match(/^(#{1,6})\s+(.*)$/);
      if (h) {
        var level = h[1].length;
        out.push("<h" + level + ">" + inline(escapeHtml(h[2])) + "</h" + level + ">");
        i++;
        continue;
      }

      // blockquote
      if (/^>\s?/.test(line)) {
        var quote = [];
        while (i < src.length && /^>\s?/.test(src[i])) { quote.push(src[i].replace(/^>\s?/, "")); i++; }
        out.push("<blockquote>" + inline(escapeHtml(quote.join(" "))) + "</blockquote>");
        continue;
      }

      // bullet list
      if (/^[-*]\s+/.test(line)) {
        var items = [];
        while (i < src.length && /^[-*]\s+/.test(src[i])) { items.push(src[i].replace(/^[-*]\s+/, "")); i++; }
        out.push("<ul>" + items.map(function (it) { return "<li>" + inline(escapeHtml(it)) + "</li>"; }).join("") + "</ul>");
        continue;
      }

      // blank line
      if (line.trim() === "") { i++; continue; }

      // paragraph: gather consecutive non-blank, non-special lines
      var para = [];
      while (i < src.length && src[i].trim() !== "" &&
        !/^```/.test(src[i]) && !/^#{1,6}\s/.test(src[i]) &&
        !/^>\s?/.test(src[i]) && !/^[-*]\s+/.test(src[i])) {
        para.push(src[i]); i++;
      }
      out.push("<p>" + inline(escapeHtml(para.join(" "))) + "</p>");
    }
    return out.join("\n");
  }

  global.MD = { render: render, escapeHtml: escapeHtml };
})(window);

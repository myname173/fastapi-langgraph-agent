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
    // images ![alt](url) — allow http(s) or root-relative paths (e.g. the
    // chart tool returns /charts/<file>.svg). MUST run before the link rule
    // below, otherwise "![alt](url)" is turned into "!" + <a>, and the chart
    // never renders as an <img> (investor.js enhanceCharts looks for
    // img[src^="/charts/"]). `s` is already HTML-escaped by the caller, so
    // alt/url are safe to interpolate into the attributes as-is.
    s = s.replace(/!\[([^\]]*)\]\((https?:\/\/[^\s)]+|\/[^\s)]+)\)/g,
      function (_, alt, u) { return '<img class="md-img" src="' + u + '" alt="' + alt + '" loading="lazy" />'; });
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

      // GFM table: a header row of cells, followed by a separator row of
      // dashes/colons, then body rows. Emits <table> so downstream
      // consumers (e.g. investor.js enhanceTables financial-card pipeline)
      // can find <table> nodes. Without this, LLM markdown tables render as
      // raw "| a | b |" text in a <p> and no enhancement triggers.
      if (line.indexOf("|") !== -1 && i + 1 < src.length &&
          /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)+\|?\s*$/.test(src[i + 1])) {
        var splitRow = function (r) {
          var s = r.replace(/^\s*\|/, "").replace(/\|\s*$/, "");
          return s.split("|").map(function (c) { return c.trim(); });
        };
        var tHeaders = splitRow(line);
        var tSeps = splitRow(src[i + 1]);
        var tAligns = tHeaders.map(function (_, idx) {
          var sep = (tSeps[idx] || "").trim();
          if (/^:-+:$/.test(sep)) return ' style="text-align:center"';
          if (/^-+:$/.test(sep)) return ' style="text-align:right"';
          if (/^:-+$/.test(sep)) return ' style="text-align:left"';
          return "";
        });
        i += 2; // skip header + separator
        var tBody = [];
        while (i < src.length && src[i].indexOf("|") !== -1 && src[i].trim() !== "") {
          tBody.push(splitRow(src[i]));
          i++;
        }
        var tHead = "<thead><tr>" + tHeaders.map(function (h, idx) {
          return "<th" + (tAligns[idx] || "") + ">" + inline(escapeHtml(h)) + "</th>";
        }).join("") + "</tr></thead>";
        var tBodyHtml = "<tbody>" + tBody.map(function (row) {
          return "<tr>" + row.map(function (cell, idx) {
            return "<td" + (tAligns[idx] || "") + ">" + inline(escapeHtml(cell)) + "</td>";
          }).join("") + "</tr>";
        }).join("") + "</tbody>";
        out.push("<table>" + tHead + tBodyHtml + "</table>");
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

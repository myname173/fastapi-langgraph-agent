/* highlight.js — lightweight regex-based syntax highlighter.
 *
 * Zero-dep, zero-build. Handles common languages with token-based highlighting.
 * Output: HTML with <span class="hljs-*"> wrappers compatible with styles.css.
 *
 * Usage: HL.highlight(code, lang) → HTML string
 *        HL.highlightAuto(code)   → auto-detect language
 */
(function (global) {
  "use strict";

  var HL = {};

  // ---- Language definitions ----
  var LANG = {
    python: {
      keywords: /\b(?:False|None|True|and|as|assert|async|await|break|class|continue|def|del|elif|else|except|finally|for|from|global|if|import|in|is|lambda|nonlocal|not|or|pass|raise|return|try|while|with|yield|print|range|len|int|str|float|list|dict|set|tuple|bool|type|isinstance|super|self|cls)\b/g,
      builtin: /\b(?:print|range|len|int|str|float|list|dict|set|tuple|bool|type|isinstance|super|open|input|enumerate|zip|map|filter|sorted|reversed|all|any|abs|min|max|sum|round|format|hasattr|getattr|setattr|delattr)\b/g,
      number: /\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b/g,
      string: /(?:f|r|b|u)?(?:'''|"""|'[^']*'|"[^"]*")/g,
      comment: /#.*/g,
      decorator: /@\w+/g,
    },
    javascript: {
      keywords: /\b(?:const|let|var|function|class|extends|new|this|return|if|else|for|while|do|switch|case|break|continue|try|catch|finally|throw|async|await|import|export|default|from|typeof|instanceof|in|of|void|delete|yield|static|get|set|true|false|null|undefined|NaN|Infinity)\b/g,
      builtin: /\b(?:console|document|window|Array|Object|String|Number|Boolean|Promise|Map|Set|JSON|Math|Date|RegExp|Error|fetch|setTimeout|setInterval|clearTimeout|clearInterval|parseInt|parseFloat)\b/g,
      number: /\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b/g,
      string: /(?:`[^`]*`|'[^']*'|"[^"]*")/g,
      comment: /\/\/.*|\/\*[\s\S]*?\*\//g,
    },
    typescript: {
      keywords: /\b(?:const|let|var|function|class|extends|new|this|return|if|else|for|while|do|switch|case|break|continue|try|catch|finally|throw|async|await|import|export|default|from|typeof|instanceof|in|of|void|delete|yield|static|get|set|true|false|null|undefined|interface|type|enum|implements|abstract|readonly|as|is|keyof|infer|never|unknown|any|string|number|boolean|symbol|bigint|object)\b/g,
      builtin: /\b(?:console|document|window|Promise|Map|Set|JSON|fetch)\b/g,
      number: /\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b/g,
      string: /(?:`[^`]*`|'[^']*'|"[^"]*")/g,
      comment: /\/\/.*|\/\*[\s\S]*?\*\//g,
    },
    bash: {
      keywords: /\b(?:if|then|else|elif|fi|for|while|do|done|case|esac|in|function|return|exit|export|local|source|echo|cd|ls|pwd|mkdir|rm|cp|mv|cat|grep|sed|awk|chmod|curl|wget|git|docker|npm|yarn|pip|python|node|npm|npx|uv|source)\b/g,
      string: /(?:'[^']*'|"[^"]*")/g,
      comment: /#.*/g,
      number: /\b\d+\b/g,
      variable: /\$\w+|\${[^}]*}/g,
    },
    sql: {
      keywords: /\b(?:SELECT|FROM|WHERE|INSERT|INTO|VALUES|UPDATE|SET|DELETE|CREATE|TABLE|ALTER|DROP|INDEX|JOIN|LEFT|RIGHT|INNER|OUTER|ON|AND|OR|NOT|NULL|IS|IN|LIKE|BETWEEN|ORDER|BY|GROUP|HAVING|LIMIT|OFFSET|UNION|ALL|DISTINCT|AS|CASE|WHEN|THEN|ELSE|END|COUNT|SUM|AVG|MAX|MIN|PRIMARY|KEY|FOREIGN|REFERENCES|DEFAULT|CHECK|UNIQUE|EXISTS|CAST|COALESCE|IFNULL|NULLIF)\b/gi,
      string: /'[^']*'/g,
      comment: /--.*|\/\*[\s\S]*?\*\//g,
      number: /\b\d+(?:\.\d+)?\b/g,
    },
    json: {
      string: /"[^"\\]*(?:\\.[^"\\]*)*"/g,
      number: /\b-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b/g,
      keyword: /\b(?:true|false|null)\b/g,
    },
    html: {
      tag: /<\/?[\w-]+(?:\s[^>]*)?\/?>/g,
      attr: /\b(?:class|id|style|href|src|alt|type|name|value|placeholder|disabled|required|readonly|onclick|onchange|onsubmit|data-[\w-]+)\b(?=\s*=)/g,
      string: /"[^"]*"|'[^']*'/g,
      comment: /<!--[\s\S]*?-->/g,
    },
    css: {
      keyword: /\b(?:@media|@import|@keyframes|!important)\b/g,
      property: /\b(?:color|background|border|margin|padding|width|height|font|display|position|top|right|bottom|left|flex|grid|gap|align|justify|opacity|transform|transition|animation|overflow|z-index|box-shadow|text-align|font-size|font-weight|line-height|border-radius|cursor|pointer-events|user-select|white-space|word-wrap|word-break)\b/g,
      number: /\b\d+(?:\.\d+)?(?:px|em|rem|%|vh|vw|s|ms|deg)?\b/g,
      string: /"[^"]*"|'[^']*'/g,
      comment: /\/\*[\s\S]*?\*\//g,
    },
  };

  // ---- Language aliases ----
  var ALIAS = {
    js: "javascript",
    ts: "typescript",
    jsx: "javascript",
    tsx: "typescript",
    py: "python",
    sh: "bash",
    shell: "bash",
    zsh: "bash",
    yaml: "bash",   // fallback
    yml: "bash",
    txt: null,      // no highlighting
    text: null,
    plain: null,
    plaintext: null,
  };

  // ---- Tokenizer ----
  function tokenize(code, langDef) {
    if (!langDef) return escapeHTML(code);

    // Collect all matches with their positions
    var tokens = [];
    var patterns = [
      { name: "comment", re: langDef.comment },
      { name: "string", re: langDef.string },
      { name: "keyword", re: langDef.keywords },
      { name: "builtin", re: langDef.builtin },
      { name: "number", re: langDef.number },
      { name: "decorator", re: langDef.decorator },
      { name: "variable", re: langDef.variable },
      { name: "tag", re: langDef.tag },
      { name: "attr", re: langDef.attr },
      { name: "property", re: langDef.property },
    ];

    var protectedRegions = [];
    patterns.forEach(function (p) {
      if (!p.re) return;
      var m;
      // Reset lastIndex for global regex
      p.re.lastIndex = 0;
      while ((m = p.re.exec(code)) !== null) {
        if (protectedRegions.some(function (r) { return m.index >= r.start && m.index < r.end; })) continue;
        tokens.push({ start: m.index, end: m.index + m[0].length, type: p.name, text: m[0] });
        // Strings and comments protect their content from further matching
        if (p.name === "string" || p.name === "comment") {
          protectedRegions.push({ start: m.index, end: m.index + m[0].length });
        }
      }
    });

    // Sort tokens by start position
    tokens.sort(function (a, b) { return a.start - b.start || b.end - a.end; });

    // Remove overlapping tokens (keep the longer one)
    var filtered = [];
    tokens.forEach(function (t) {
      if (filtered.length === 0 || t.start >= filtered[filtered.length - 1].end) {
        filtered.push(t);
      }
    });

    // Build HTML
    var result = "";
    var pos = 0;
    filtered.forEach(function (t) {
      if (t.start > pos) result += escapeHTML(code.slice(pos, t.start));
      result += '<span class="hljs-' + t.type + '">' + escapeHTML(t.text) + '</span>';
      pos = t.end;
    });
    if (pos < code.length) result += escapeHTML(code.slice(pos));

    return result;
  }

  function escapeHTML(s) {
    return s
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  // ---- Detect language from class name ----
  function detectLang(classList) {
    if (!classList) return null;
    var lang = null;
    var classes = classList.split(/\s+/);
    for (var i = 0; i < classes.length; i++) {
      var c = classes[i].toLowerCase();
      var m = c.match(/^(?:language-|lang-)?(\w+)$/);
      if (m) {
        var name = m[1];
        // Check aliases
        if (ALIAS[name] !== undefined) {
          lang = ALIAS[name];
          if (lang) return lang;
          continue;
        }
        if (LANG[name]) return name;
      }
    }
    return lang;
  }

  // ---- Public API ----
  HL.highlight = function (code, lang) {
    var resolved = lang ? (ALIAS[lang.toLowerCase()] !== undefined ? ALIAS[lang.toLowerCase()] : lang.toLowerCase()) : null;
    var langDef = LANG[resolved] || null;
    return tokenize(code, langDef);
  };

  HL.highlightAuto = function (code) {
    // Simple heuristics: count keyword matches for each language
    var best = null;
    var bestScore = 0;
    for (var name in LANG) {
      if (!LANG.hasOwnProperty(name)) continue;
      var def = LANG[name];
      var score = 0;
      if (def.keywords) {
        var m;
        def.keywords.lastIndex = 0;
        while ((m = def.keywords.exec(code)) !== null) score++;
      }
      if (score > bestScore) {
        bestScore = score;
        best = name;
      }
    }
    // Require at least 2 keyword matches for auto-detection
    if (bestScore >= 2) {
      return tokenize(code, LANG[best]);
    }
    return escapeHTML(code);
  };

  /* Enhance code blocks in an HTML container:
   * - Wrap <pre><code> in .code-block-wrapper
   * - Add copy button
   * - Apply syntax highlighting
   * - Detect language from class attribute
   */
  HL.enhanceCodeBlocks = function (container) {
    var pres = container.querySelectorAll("pre");
    for (var i = 0; i < pres.length; i++) {
      var pre = pres[i];
      // Skip already wrapped
      if (pre.parentElement && pre.parentElement.classList.contains("code-block-wrapper")) continue;

      var code = pre.querySelector("code");
      var rawCode = code ? code.textContent : pre.textContent;
      var lang = code ? detectLang(code.className) : null;

      // Create wrapper
      var wrapper = document.createElement("div");
      wrapper.className = "code-block-wrapper";

      // Language label
      if (lang) {
        var label = document.createElement("span");
        label.className = "code-lang";
        label.textContent = lang;
        wrapper.appendChild(label);
      }

      // Copy button
      var copyBtn = document.createElement("button");
      copyBtn.className = "code-copy";
      copyBtn.textContent = "📋";
      copyBtn.title = "复制代码";
      copyBtn.setAttribute("aria-label", "复制代码");
      copyBtn.addEventListener("click", function (e) {
        var btn = e.currentTarget;
        var wrapperEl = btn.parentElement;
        var preEl = wrapperEl.querySelector("pre");
        var text = preEl ? preEl.textContent : "";
        copyToClipboard(text, btn);
      });
      wrapper.appendChild(copyBtn);

      // Move pre into wrapper
      pre.parentElement.insertBefore(wrapper, pre);
      wrapper.appendChild(pre);

      // Apply highlighting if code element exists
      if (code && lang) {
        code.innerHTML = HL.highlight(rawCode, lang);
      }
    }
  };

  function copyToClipboard(text, btn) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () {
        showCopied(btn);
      }).catch(function () {
        fallbackCopy(text, btn);
      });
    } else {
      fallbackCopy(text, btn);
    }
  }

  function fallbackCopy(text, btn) {
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); showCopied(btn); } catch (e) { /* ignore */ }
    document.body.removeChild(ta);
  }

  function showCopied(btn) {
    btn.classList.add("copied");
    btn.textContent = "✓";
    setTimeout(function () {
      btn.classList.remove("copied");
      btn.textContent = "📋";
    }, 1500);
  }

  // Export
  global.HL = HL;
})(window);

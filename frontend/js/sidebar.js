/* sidebar.js — Investor sidebar: watchlist, portfolio, risk profile, alerts.
 *
 * A collapsible right-panel rendered alongside the chat view. Each section
 * is an independent card that fetches data via the existing authFetch API
 * client. Sections gracefully degrade when their backend is disabled.
 */
(function (global) {
  "use strict";

  var AUTOREFRESH_MS = 30000; // refresh active data every 30 s

  var Sidebar = {
    el: null,          // root .investor-sidebar
    toggleBtn: null,   // #btn-sidebar
    visible: false,
    timer: null,       // refresh interval id
    features: {
      watchlist: false,
      portfolio: false,
      risk: false,
      alert: false,
    },
  };

  /* ---- lifecycle ---- */

  Sidebar.mount = function (parentEl) {
    if (Sidebar.el) return;
    Sidebar.el = document.createElement("aside");
    Sidebar.el.className = "investor-sidebar";
    Sidebar.el.setAttribute("aria-label", "投资者工具面板");
    Sidebar.el.setAttribute("role", "complementary");
    Sidebar.el.innerHTML = Sidebar._template();
    parentEl.appendChild(Sidebar.el);

    // toggle button — find it in the DOM (it's in the navbar, not inside sidebar)
    Sidebar.toggleBtn = document.getElementById("btn-sidebar");
    if (!Sidebar.toggleBtn) {
      // Dynamically insert toggle button into navbar if not present
      var nav = document.querySelector(".navbar");
      if (nav) {
        var btn = document.createElement("button");
        btn.id = "btn-sidebar";
        btn.className = "btn-sidebar";
        btn.setAttribute("aria-label", "打开工具面板");
        btn.setAttribute("title", "投资者工具面板");
        btn.textContent = "📊";
        btn.addEventListener("click", Sidebar.toggle);
        nav.appendChild(btn);
        Sidebar.toggleBtn = btn;
      }
    } else {
      Sidebar.toggleBtn.addEventListener("click", Sidebar.toggle);
    }

    // restore visibility
    var stored = localStorage.getItem("investor_sidebar") === "true";
    if (stored) Sidebar.show(false);

    // deferred fetch — wait for DOM to settle
    setTimeout(function () { Sidebar._fetchAll(); }, 200);
  };

  Sidebar.unmount = function () {
    if (Sidebar.timer) { clearInterval(Sidebar.timer); Sidebar.timer = null; }
    if (Sidebar.el) { Sidebar.el.remove(); Sidebar.el = null; }
  };

  /* ---- toggle ---- */

  Sidebar.toggle = function () {
    Sidebar.visible ? Sidebar.hide() : Sidebar.show(true);
  };

  Sidebar.show = function (animate) {
    if (!Sidebar.el) return;
    Sidebar.visible = true;
    Sidebar.el.classList.add("is-open");
    if (animate !== false) Sidebar.el.classList.add("is-animate");
    else Sidebar.el.classList.remove("is-animate");
    localStorage.setItem("investor_sidebar", "true");
    if (Sidebar.toggleBtn) Sidebar.toggleBtn.classList.add("active");
    Sidebar._fetchAll();
    Sidebar._startRefresh();
  };

  Sidebar.hide = function () {
    if (!Sidebar.el) return;
    Sidebar.visible = false;
    Sidebar.el.classList.remove("is-open");
    localStorage.setItem("investor_sidebar", "false");
    if (Sidebar.toggleBtn) Sidebar.toggleBtn.classList.remove("active");
    Sidebar._stopRefresh();
  };

  /* ---- refresh ---- */

  Sidebar._startRefresh = function () {
    Sidebar._stopRefresh();
    Sidebar.timer = setInterval(function () {
      if (Sidebar.visible) Sidebar._fetchAll();
    }, AUTOREFRESH_MS);
  };

  Sidebar._stopRefresh = function () {
    if (Sidebar.timer) { clearInterval(Sidebar.timer); Sidebar.timer = null; }
  };

  /* ---- fetch ---- */

  Sidebar._fetchAll = function () {
    Sidebar._loadWatchlist();
    Sidebar._loadPortfolio();
    Sidebar._loadRiskProfile();
    Sidebar._loadAlerts();
  };

  /* -- watchlist -- */

  Sidebar._loadWatchlist = function () {
    if (!Sidebar.features.watchlist) return;
    API.authFetch("/watchlist")
      .then(function (r) { return r.ok ? r.json() : []; })
      .then(function (items) { Sidebar._renderWatchlist(items); })
      .catch(function () { /* silent */ });
  };

  Sidebar._renderWatchlist = function (items) {
    var list = Sidebar.el && Sidebar.el.querySelector(".sidebar-watchlist-list");
    if (!list) return;
    if (!items || !items.length) {
      list.innerHTML = '<div class="sidebar-empty">点击 + 添加自选股</div>';
      return;
    }
    list.innerHTML = items.map(function (i) {
      return (
        '<div class="sidebar-wl-item" data-id="' + Sidebar._esc(i.id) + '" data-symbol="' + Sidebar._esc(i.symbol) + '">' +
        '  <span class="sidebar-wl-symbol">' + Sidebar._esc(i.symbol) + '</span>' +
        (i.name ? '  <span class="sidebar-wl-name">' + Sidebar._esc(i.name) + '</span>' : '') +
        '  <button class="sidebar-wl-del" data-id="' + Sidebar._esc(i.id) + '" title="移除">×</button>' +
        '</div>'
      );
    }).join("");

    // bind delete
    var dels = list.querySelectorAll(".sidebar-wl-del");
    for (var d = 0; d < dels.length; d++) {
      dels[d].addEventListener("click", Sidebar._onWatchlistRemove);
    }
    // bind click to send quick-check message
    var rows = list.querySelectorAll(".sidebar-wl-item");
    for (var r = 0; r < rows.length; r++) {
      rows[r].addEventListener("click", Sidebar._onWatchlistClick);
    }
  };

  Sidebar._onWatchlistClick = function (e) {
    var symbol = this.getAttribute("data-symbol");
    if (!symbol) return;
    // focus the chat input and pre-fill with a quick query
    var input = document.getElementById("chat-input");
    if (input) {
      input.focus();
      Sidebar._sendQuickQuery("快速分析一下 " + symbol + " 当前走势");
    }
  };

  Sidebar._onWatchlistRemove = function (e) {
    e.stopPropagation();
    var id = this.getAttribute("data-id");
    if (!id) return;
    API.authFetch("/watchlist/" + encodeURIComponent(id), { method: "DELETE" })
      .then(function () { setTimeout(Sidebar._loadWatchlist, 500); })
      .catch(function () { /* silent */ });
  };

  /* -- portfolio quick summary -- */

  Sidebar._loadPortfolio = function () {
    if (!Sidebar.features.portfolio) return;
    // GET /portfolio returns a list of portfolios; the sidebar summary needs
    // the /portfolio/{id}/analysis aggregation (total_value, holdings, PnL).
    API.authFetch("/portfolio")
      .then(function (r) { return r.ok ? r.json() : []; })
      .then(function (items) {
        if (!items || !items.length) {
          Sidebar._renderPortfolio(null);
          return;
        }
        var firstId = items[0].id;
        return API.authFetch(
          "/portfolio/" + encodeURIComponent(firstId) + "/analysis"
        )
          .then(function (r) { return r.ok ? r.json() : null; })
          .then(function (data) { Sidebar._renderPortfolio(data); });
      })
      .catch(function () { /* silent */ });
  };

  Sidebar._renderPortfolio = function (data) {
    var el = Sidebar.el && Sidebar.el.querySelector(".sidebar-portfolio-body");
    if (!el) return;
    if (!data) {
      el.innerHTML = '<div class="sidebar-empty">暂无组合，请先创建</div>';
      return;
    }
    if (!data.holdings || !data.holdings.length) {
      el.innerHTML =
        '<div class="sidebar-pnl">总市值 ¥' +
        (data.total_value || 0).toLocaleString("zh-CN", { maximumFractionDigits: 2 }) +
        '</div>' +
        '<div class="sidebar-empty">暂无持仓记录</div>';
      return;
    }
    var total = data.total_value || 0;
    var cost = data.total_cost || 0;
    var pnl = total - cost;
    var pnlPct = cost > 0 ? ((pnl / cost) * 100) : 0;
    var pnlClass = pnl >= 0 ? "sidebar-up" : "sidebar-down";
    var pnlSign = pnl >= 0 ? "+" : "";

    el.innerHTML =
      '<div class="sidebar-pnl ' + pnlClass + '">' +
      '  总市值 ¥' + total.toLocaleString("zh-CN", { maximumFractionDigits: 2 }) +
      '  <span>盈亏：' + pnlSign + pnl.toLocaleString("zh-CN", { maximumFractionDigits: 2 }) +
      ' (' + pnlSign + pnlPct.toFixed(2) + '%)</span>' +
      '</div>' +
      '<div class="sidebar-holdings">' +
      data.holdings.slice(0, 5).map(function (h) {
        return (
          '<div class="sidebar-hold-item">' +
          '  <span class="sidebar-hl-sym">' + Sidebar._esc(h.symbol || h.code) + '</span>' +
          '  <span class="sidebar-hl-qty">' + (h.shares || 0) + '股</span>' +
          '</div>'
        );
      }).join("") +
      '</div>';
  };

  /* -- risk profile -- */

  Sidebar._loadRiskProfile = function () {
    if (!Sidebar.features.risk) return;
    API.authFetch("/risk-profile/me")
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        // API returns {risk_profile: {...}} or {risk_profile: null}
        Sidebar._renderRiskProfile(data && data.risk_profile);
      })
      .catch(function () { /* silent */ });
  };

  Sidebar._renderRiskProfile = function (p) {
    var el = Sidebar.el && Sidebar.el.querySelector(".sidebar-risk-body");
    if (!el) return;
    if (!p) {
      el.innerHTML = '<div class="sidebar-empty">未设置风险画像</div>' +
        '<button class="sidebar-risk-btn sidebar-btn">设置风险画像</button>';
      var btn = el.querySelector(".sidebar-risk-btn");
      if (btn) btn.addEventListener("click", Sidebar._showRiskSetup);
      return;
    }
    var styleMap = { conservative: "保守型", balanced: "稳健型", aggressive: "激进型" };
    var style = styleMap[p.risk_tolerance] || p.risk_tolerance || "未设置";
    var horizonMap = {
      short_term: "短期 (1年以内)",
      medium_term: "中期 (1-5年)",
      long_term: "长期 (5年以上)",
    };
    var horizon = horizonMap[p.horizon] || p.horizon || "-";
    el.innerHTML =
      '<div class="sidebar-risk-row">' +
      '  <span class="sidebar-risk-label">风险偏好</span>' +
      '  <span class="sidebar-risk-value">' + style + '</span>' +
      '</div>' +
      '<div class="sidebar-risk-row">' +
      '  <span class="sidebar-risk-label">投资目标</span>' +
      '  <span class="sidebar-risk-value">' + Sidebar._esc(p.investment_goals || "-") + '</span>' +
      '</div>' +
      '<div class="sidebar-risk-row">' +
      '  <span class="sidebar-risk-label">投资周期</span>' +
      '  <span class="sidebar-risk-value">' + horizon + '</span>' +
      '</div>' +
      '<div class="sidebar-risk-row">' +
      '  <span class="sidebar-risk-label">月均可投</span>' +
      '  <span class="sidebar-risk-value">¥' + (p.monthly_invest || 0) + '</span>' +
      '</div>' +
      '<div class="sidebar-risk-row">' +
      '  <span class="sidebar-risk-label">投资经验</span>' +
      '  <span class="sidebar-risk-value">' + Sidebar._esc(p.experience || "-") + '</span>' +
      '</div>' +
      '<button class="sidebar-risk-btn sidebar-btn">修改</button>';
    var btn2 = el.querySelector(".sidebar-risk-btn");
    if (btn2) btn2.addEventListener("click", Sidebar._showRiskSetup);
  };

  Sidebar._showRiskSetup = function () {
    var dialog = document.getElementById("sidebar-risk-dialog");
    if (!dialog) {
      dialog = document.createElement("div");
      dialog.id = "sidebar-risk-dialog";
      dialog.className = "sidebar-dialog-overlay";
      dialog.innerHTML =
        '<div class="sidebar-dialog">' +
        '  <h4>风险画像设置</h4>' +
        '  <label>风险偏好' +
        '    <select id="rd-tolerance">' +
        '      <option value="conservative">保守型</option>' +
        '      <option value="balanced" selected>稳健型</option>' +
        '      <option value="aggressive">激进型</option>' +
        '    </select>' +
        '  </label>' +
        '  <label>投资目标' +
        '    <input id="rd-goals" type="text" placeholder="如：长期资本增值">' +
        '  </label>' +
        '  <label>投资周期' +
        '    <select id="rd-horizon">' +
        '      <option value="short_term">短期 (1年以内)</option>' +
        '      <option value="medium_term">中期 (1-5年)</option>' +
        '      <option value="long_term">长期 (5年以上)</option>' +
        '    </select>' +
        '  </label>' +
        '  <label>月均可投资金额 (元)' +
        '    <input id="rd-monthly" type="number" min="0" value="5000" step="500">' +
        '  </label>' +
        '  <label>投资经验' +
        '    <input id="rd-experience" type="text" placeholder="如：5年以上A股经验">' +
        '  </label>' +
        '  <div class="sidebar-dialog-actions">' +
        '    <button class="sidebar-btn" id="rd-save">保存</button>' +
        '    <button class="sidebar-btn sidebar-btn-ghost" id="rd-close">取消</button>' +
        '  </div>' +
        '</div>';
      document.body.appendChild(dialog);

      dialog.querySelector("#rd-close").addEventListener("click", function () { dialog.remove(); });
      dialog.querySelector("#rd-save").addEventListener("click", Sidebar._saveRiskProfile);
      dialog.addEventListener("click", function (e) { if (e.target === dialog) dialog.remove(); });
    }
  };

  Sidebar._saveRiskProfile = function () {
    var body = {
      risk_tolerance: document.getElementById("rd-tolerance").value,
      investment_goals: document.getElementById("rd-goals").value || "",
      horizon: document.getElementById("rd-horizon").value,
      monthly_invest: parseFloat(document.getElementById("rd-monthly").value) || 0,
      experience: document.getElementById("rd-experience").value || "",
    };
    API.authFetch("/risk-profile/me", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(function () {
      var dialog = document.getElementById("sidebar-risk-dialog");
      if (dialog) dialog.remove();
      Sidebar._loadRiskProfile();
    }).catch(function () { /* silent */ });
  };

  /* -- alerts -- */

  Sidebar._loadAlerts = function () {
    if (!Sidebar.features.alert) return;
    API.authFetch("/alerts?active_only=true")
      .then(function (r) { return r.ok ? r.json() : []; })
      .then(function (items) { Sidebar._renderAlerts(items); })
      .catch(function () { /* silent */ });
  };

  Sidebar._renderAlerts = function (items) {
    var list = Sidebar.el && Sidebar.el.querySelector(".sidebar-alert-list");
    if (!list) return;
    if (!items || !items.length) {
      list.innerHTML = '<div class="sidebar-empty">暂无活跃预警</div>';
      return;
    }
    var condMap = { above: "突破上方", below: "跌破下方" };
    list.innerHTML = items.map(function (a) {
      return (
        '<div class="sidebar-alert-item" data-id="' + Sidebar._esc(a.id) + '">' +
        '  <span class="sidebar-alert-sym">' + Sidebar._esc(a.symbol) + '</span>' +
        '  <span class="sidebar-alert-cond">' + (condMap[a.condition] || a.condition) + '</span>' +
        '  <span class="sidebar-alert-price">¥' + Sidebar._esc(String(a.price)) + '</span>' +
        '  <button class="sidebar-alert-del" data-id="' + Sidebar._esc(a.id) + '" title="删除">×</button>' +
        '</div>'
      );
    }).join("");

    var dels = list.querySelectorAll(".sidebar-alert-del");
    for (var d = 0; d < dels.length; d++) {
      dels[d].addEventListener("click", function (e) {
        e.stopPropagation();
        var id = this.getAttribute("data-id");
        API.authFetch("/alerts/" + encodeURIComponent(id), { method: "DELETE" })
          .then(function () { setTimeout(Sidebar._loadAlerts, 300); })
          .catch(function () { /* silent */ });
      });
    }
  };

  /* -- add watchlist -- */

  Sidebar._onAddWatchlist = function () {
    var symbol = prompt("请输入股票代码（如 sh600519）：");
    if (!symbol) return;
    var name = prompt("股票名称（可选）：") || "";
    var qs = "symbol=" + encodeURIComponent(symbol);
    if (name) qs += "&name=" + encodeURIComponent(name);
    API.authFetch("/watchlist?" + qs, { method: "POST" })
      .then(function () { Sidebar._loadWatchlist(); })
      .catch(function () { /* silent */ });
  };

  Sidebar._onAddAlert = function () {
    var symbol = prompt("请输入股票代码（如 sh600519）：");
    if (!symbol) return;
    var cond = prompt("触发条件（above 突破上方 / below 跌破下方）：", "above");
    if (!cond || (cond !== "above" && cond !== "below")) return;
    var price = parseFloat(prompt("触发价格（元）："));
    if (isNaN(price) || price <= 0) return;
    var note = prompt("备注（可选）：") || "";
    API.authFetch("/alerts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        symbol: symbol,
        condition: cond,
        price: price,
        note: note,
      }),
    }).then(function () { Sidebar._loadAlerts(); })
      .catch(function () { /* silent */ });
  };

  /* -- helpers -- */

  Sidebar._sendQuickQuery = function (query) {
    var input = document.getElementById("chat-input");
    if (!input) return;
    // Use a simple approach: set the input value and dispatch an input event
    var nativeInputValueSetter = Object.getOwnPropertyDescriptor(
      HTMLInputElement.prototype, "value"
    ).set;
    nativeInputValueSetter.call(input, query);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  };

  Sidebar._esc = function (s) {
    if (!s) return "";
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  };

  /* ---- HTML template ---- */

  Sidebar._template = function () {
    var sections = [];
    if (Sidebar.features.watchlist) {
      sections.push(
        '<div class="sidebar-section">' +
        '  <div class="sidebar-section-hd">' +
        '    <span>⭐ 自选股</span>' +
        '    <button class="sidebar-add-btn" id="sidebar-add-wl" aria-label="添加自选股" title="添加自选股">+</button>' +
        '  </div>' +
        '  <div class="sidebar-watchlist-list sidebar-section-bd"></div>' +
        '</div>'
      );
    }
    if (Sidebar.features.portfolio) {
      sections.push(
        '<div class="sidebar-section">' +
        '  <div class="sidebar-section-hd">' +
        '    <span>📈 组合概览</span>' +
        '  </div>' +
        '  <div class="sidebar-portfolio-body sidebar-section-bd"></div>' +
        '</div>'
      );
    }
    if (Sidebar.features.risk) {
      sections.push(
        '<div class="sidebar-section">' +
        '  <div class="sidebar-section-hd">' +
        '    <span>🎯 风险画像</span>' +
        '  </div>' +
        '  <div class="sidebar-risk-body sidebar-section-bd"></div>' +
        '</div>'
      );
    }
    if (Sidebar.features.alert) {
      sections.push(
        '<div class="sidebar-section">' +
        '  <div class="sidebar-section-hd">' +
        '    <span>🔔 价格预警</span>' +
        '    <button class="sidebar-add-btn" id="sidebar-add-alert" aria-label="添加预警" title="添加预警">+</button>' +
        '  </div>' +
        '  <div class="sidebar-alert-list sidebar-section-bd"></div>' +
        '</div>'
      );
    }
    if (!sections.length) {
      sections.push(
        '<div class="sidebar-section">' +
        '  <div class="sidebar-empty" style="padding:2rem 1rem;text-align:center;">' +
        '    工具面板未启用<br><small>启用自选股/预警等模块即可</small>' +
        '  </div>' +
        '</div>'
      );
    }
    return sections.join("");
  };

  /* ---- init on mount ---- */

  Sidebar.initFeatures = function () {
    // Check which features are available via backend config
    // We detect by trying GET requests — if 404, feature is disabled
    var checks = [];
    if (Sidebar.features.watchlist) {
      checks.push(
        API.authFetch("/watchlist")
          .then(function (r) { Sidebar.features.watchlist = r.ok; })
          .catch(function () { Sidebar.features.watchlist = false; })
      );
    }
    if (Sidebar.features.portfolio) {
      checks.push(
        API.authFetch("/portfolio")
          .then(function (r) { Sidebar.features.portfolio = r.ok; })
          .catch(function () { Sidebar.features.portfolio = false; })
      );
    }
    if (Sidebar.features.risk) {
      checks.push(
        API.authFetch("/risk-profile/me")
          .then(function (r) { Sidebar.features.risk = r.ok; })
          .catch(function () { Sidebar.features.risk = false; })
      );
    }
    if (Sidebar.features.alert) {
      checks.push(
        API.authFetch("/alerts")
          .then(function (r) { Sidebar.features.alert = r.ok; })
          .catch(function () { Sidebar.features.alert = false; })
      );
    }
    return Promise.all(checks).then(function () {
      // Rebuild template with detected features
      if (Sidebar.el) {
        Sidebar.el.innerHTML = Sidebar._template();
        setTimeout(function () { Sidebar._fetchAll(); }, 100);
      }
    });
  };

  // Bind events after template render
  var origShow = Sidebar.show;
  Sidebar.show = function (animate) {
    origShow(animate);
    // Re-bind add buttons
    setTimeout(function () {
      var addWl = document.getElementById("sidebar-add-wl");
      if (addWl) addWl.addEventListener("click", Sidebar._onAddWatchlist);
      var addAlert = document.getElementById("sidebar-add-alert");
      if (addAlert) addAlert.addEventListener("click", Sidebar._onAddAlert);
    }, 50);
  };

  global.Sidebar = Sidebar;
})(window);

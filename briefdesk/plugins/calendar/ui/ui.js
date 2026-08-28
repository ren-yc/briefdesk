/* calendar 插件前端：日历视图的完整前端随插件包分发。
 *
 * 核心只提供通用加载器与视图钩子（app.js 的 registerPluginView）：
 * - 本文件注入后自行创建日历按钮 / 视图容器 / 两个浮层（DOM 不入核心 index.html）；
 * - 入口显隐、视图模式、hash 路由（#calendar）、数据加载、交互全部由本插件负责；
 * - 复用的核心全局工具：esc / escAttr / catColor / currentItems / renderItemRow /
 *   handleRowAction / verifyItem / doRecategorize / fetchContext / openSubjectTimeline /
 *   clearSearch / updateActiveNav / syncHash / fetchData / fetchSidebarData /
 *   syncBodyScrollLock / parseLocalTime / isDateOnly / fmtDate / fmtMonthDay /
 *   parseExtraTimes / timeExpired / batchMode / exitBatchMode / inlineSvgIcons 等。
 */
(function () {
  "use strict";
  const PLUGIN = "calendar";

  // ── 状态 ──
  let calendarMode = false;       // 日历视图开关
  let calYear = 0, calMonth = 0;  // 当前显示的日历年/月（月 1-12）
  let calMemoOnly = false;        // 仅看备忘录截止（localStorage 持久化）
  let calAllItems = [];           // 日历原始数据（未过滤，供筛选开关重渲染）
  let calMonthItems = [];         // 当前月日历数据（供详情浮层查找）
  let calDetailItem = null;       // 日历详情浮层当前卡片
  // 详情浮层里是否发生过改数据的操作（备忘/忽略/改分类）。
  // 关闭时原先无条件 loadCalendar()：只是打开看一眼再关掉也会白跑一次
  // /api/calendar 请求并整月重渲染。
  let calDetailDirty = false;

  // ── 元素（init 时创建）──
  let $calendarBtn = null;
  let $calendarView = null;
  let $calDetailModal = null;
  let $calDetailBody = null;
  let $calDayModal = null;
  let $calDayTitle = null;
  let $calDayList = null;

  // ── DOM 构建：入口移到侧边栏「订阅」前（与订阅/备忘录/已忽略同组：
  // 同类——主内容区视图切换——集中在 nav-special）；视图容器进 main；浮层挂 body ──
  function buildDom() {
    $calendarBtn = document.createElement("a");
    $calendarBtn.id = "calendar-btn";
    $calendarBtn.href = "#";
    $calendarBtn.className = "cat-link";
    $calendarBtn.title = "日历视图（查看带时间的活动/截止安排）";
    $calendarBtn.innerHTML =
      '<span class="cat-link-main"><img src="/icons/calendar.svg" class="icon-sm cat-icon" alt="">日历</span>';
    const $subs = document.getElementById("subs-link");
    if ($subs && $subs.parentNode) $subs.parentNode.insertBefore($calendarBtn, $subs);

    $calendarView = document.createElement("div");
    $calendarView.id = "calendar-view";
    $calendarView.classList.add("hidden");
    const $main = document.querySelector("main");
    const $loadMoreWrap = document.getElementById("load-more-wrap");
    if ($loadMoreWrap) $loadMoreWrap.after($calendarView);
    else if ($main) $main.appendChild($calendarView);

    $calDetailModal = document.createElement("div");
    $calDetailModal.id = "calendar-detail-modal";
    $calDetailModal.className = "modal hidden";
    $calDetailModal.setAttribute("role", "dialog");
    $calDetailModal.setAttribute("aria-label", "卡片详情");
    $calDetailModal.innerHTML =
      '<div class="modal-content detail-content">'
      + '<div class="group-overlay-head">'
      + "<h2>卡片详情</h2>"
      + '<button id="cal-detail-close" title="关闭 (Esc)">×</button>'
      + "</div>"
      + '<div id="cal-detail-body"></div>'
      + "</div>";
    document.body.appendChild($calDetailModal);
    $calDetailBody = document.getElementById("cal-detail-body");

    $calDayModal = document.createElement("div");
    $calDayModal.id = "cal-day-modal";
    $calDayModal.className = "modal hidden";
    $calDayModal.setAttribute("role", "dialog");
    $calDayModal.setAttribute("aria-label", "当日事件");
    $calDayModal.innerHTML =
      '<div class="modal-content detail-content">'
      + '<div class="group-overlay-head">'
      + '<h2 id="cal-day-title"></h2>'
      + '<button id="cal-day-close" title="关闭 (Esc)">×</button>'
      + "</div>"
      + '<div id="cal-day-list"></div>'
      + "</div>";
    document.body.appendChild($calDayModal);
    $calDayTitle = document.getElementById("cal-day-title");
    $calDayList = document.getElementById("cal-day-list");
  }

  // ── 视图 ──
  function enterCalendarMode({ syncHash: sh = true } = {}) {
    calendarMode = true;
    if (batchMode) exitBatchMode();
    document.body.classList.add("calendar-mode");
    $calendarBtn.classList.add("active");
    $calendarView.classList.remove("hidden");
    if (sh) syncHash("push");
    const now = new Date();
    calYear = now.getFullYear();
    calMonth = now.getMonth() + 1;
    loadCalendar();
    fetchSidebarData(); // 侧边栏/颜色数据只随 /api/items 下发：进入日历即补拉（覆盖 F5 刷新）
  }

  function exitCalendarMode({ syncHash: sh = true } = {}) {
    calendarMode = false;
    document.body.classList.remove("calendar-mode");
    $calendarBtn.classList.remove("active");
    $calendarView.classList.add("hidden");
    if (sh) syncHash("push");
    fetchData();
  }

  async function loadCalendar() {
    if (!calendarMode) return;
    const first = new Date(calYear, calMonth - 1, 1);
    const gridStart = new Date(first);
    gridStart.setDate(1 - first.getDay()); // 周日首列
    const gridEnd = new Date(gridStart);
    gridEnd.setDate(gridStart.getDate() + 41); // 6 周 × 7 天
    try {
      const res = await fetch("/api/calendar?from=" + fmtDate(gridStart) + "&to=" + fmtDate(gridEnd));
      if (!res.ok) throw new Error("HTTP " + res.status);
      const data = await res.json();
      calAllItems = data.items || []; // 保留原始数据，筛选开关切换时据此重渲染
      renderCalendar(calAllItems);
    } catch (err) {
      console.error("Calendar error:", err);
      // 原先直接 innerHTML 覆盖整个视图，连月份导航一起抹掉：
      // 加载失败后用户既不能重试也不能切换月份，只能刷新页面。
      // 改为只替换网格区域，保留头部导航，并提供重试按钮。
      const grid = $calendarView.querySelector(".cal-grid");
      const box = '<div class="cal-error" role="alert">' +
        '<p class="text-muted">日历加载失败</p>' +
        '<button type="button" class="cal-retry">重试</button></div>';
      if (grid) grid.outerHTML = box;
      else $calendarView.innerHTML = renderCalHead() + box;
      const retry = $calendarView.querySelector(".cal-retry");
      if (retry) retry.addEventListener("click", loadCalendar);
    }
  }

  function findCalendarItem(id) {
    const all = calMonthItems.concat(currentItems);
    return all.find(i => String(i.id) === id) || null;
  }

  function calDaySet(item) {
    // 卡片占位的全部日期：主 start/end + extra_times 各时间点
    // 「仅看备忘录截止」模式：只收 end 类时间点
    const days = new Set();
    for (const t of [item.start, item.end]) {
      if (typeof t === "string" && t && (!calMemoOnly || t === item.end)) days.add(t.slice(0, 10));
    }
    for (const e of parseExtraTimes(item)) {
      if (!calMemoOnly || e.type === "end") days.add(e.time.slice(0, 10));
    }
    return days;
  }

  function dayTimeEntry(item, dateStr) {
    // 卡片在指定日期的具体时间点条目：取落在该日的时间点（主字段或 extra_times）
    // 中最早者，携带其 type 与 label；无则 null
    let best = null;
    const consider = (t, type, label) => {
      if (typeof t === "string" && t.slice(0, 10) === dateStr && (!best || t < best.time)) {
        best = { time: t, type: type, label: label };
      }
    };
    if (item.start && !calMemoOnly) consider(item.start, "start", "");
    if (item.end) consider(item.end, "end", "");
    for (const e of parseExtraTimes(item)) {
      if (!calMemoOnly || e.type === "end") consider(e.time, e.type, e.label || "");
    }
    return best;
  }

  function calChipLabel(item, dateStr) {
    // 日历 chip 的该日时间标签：任务名优先（「截止 部门宣传视频」），
    // 其次时刻（HH:MM），date-only 无标签时只给「截止/开始」前缀
    const e = dayTimeEntry(item, dateStr);
    if (!e) return { text: "", expired: false };
    const prefix = e.type === "start" ? "开始" : "截止";
    const hhmmPart = e.time.slice(11, 16);
    let text = "";
    if (e.label) text = prefix + " " + e.label;
    else if (hhmmPart) text = hhmmPart;
    else text = prefix;
    return { text: text, expired: timeExpired(e.time) };
  }

  // 头部导航（上月/今天/仅看备忘录/标题/下月/返回列表）。
  // 抽成 helper 供正常渲染与加载失败态共用：失败时也必须保留可操作的导航。
  function renderCalHead() {
    let h = '<div class="cal-head">';
    h += '<button class="cal-nav" data-nav="prev" title="上月" aria-label="上一个月">‹</button>';
    h += '<button class="cal-today">今天</button>';
    h += '<button class="cal-memo-toggle' + (calMemoOnly ? " active" : "") +
      '" aria-pressed="' + (calMemoOnly ? "true" : "false") +
      '" title="仅显示备忘录卡片的截止日期">仅看备忘录截止</button>';
    h += '<span class="cal-title" role="heading" aria-level="2">' + calYear + " 年 " + calMonth + " 月</span>";
    h += '<button class="cal-nav" data-nav="next" title="下月" aria-label="下一个月">›</button>';
    h += '<button class="cal-exit">返回列表</button>';
    h += "</div>";
    return h;
  }

  function renderCalendar(items) {
    // 「仅看备忘录截止」：只渲染备忘录卡片（is_verified=1）
    if (calMemoOnly) items = items.filter(it => it.is_verified === 1);
    calMonthItems = items;
    const first = new Date(calYear, calMonth - 1, 1);
    const gridStart = new Date(first);
    gridStart.setDate(1 - first.getDay());
    const byDay = new Map();
    for (const it of items) {
      for (const d of calDaySet(it)) {
        if (!byDay.has(d)) byDay.set(d, []);
        byDay.get(d).push(it);
      }
    }
    const todayStr = fmtDate(new Date());
    let html = '';
    html += renderCalHead();
    html += '<div class="cal-weekdays">' + ["日", "一", "二", "三", "四", "五", "六"].map(w => "<span>" + w + "</span>").join("") + "</div>";
    html += '<div class="cal-grid">';
    const cur = new Date(gridStart);
    for (let i = 0; i < 42; i++) {
      const ds = fmtDate(cur);
      const dayItems = (byDay.get(ds) || []).slice().sort((a, b) =>
        ((a.start || a.end) || "").localeCompare((b.start || b.end) || ""));
      const inMonth = cur.getMonth() === calMonth - 1;
      const isToday = ds === todayStr;
      html += '<div class="cal-cell' + (inMonth ? "" : " out") + (isToday ? " today" : "") + '" data-date="' + ds + '">';
      html += '<span class="cal-daynum">' + cur.getDate() + "</span>";
      html += '<div class="cal-chips">';
      dayItems.slice(0, 3).forEach((it, riseI) => {
        // 按当日对应的时间点渲染：时间标签（任务名/时刻）与过期样式都取该日的点，
        // 而不是整卡主字段（多时间点卡片在每个日期显示各自的任务信息）
        const chip = calChipLabel(it, ds);
        html += '<button class="cal-chip' + (chip.expired ? " expired" : "") + '" data-id="' + escAttr(it.id) + '" style="--cat:' + escAttr(catColor.get(it.category) || "#6B7280") + ';--rise-i:' + riseI + '" title="' + escAttr(it.title) + '">' + esc(chip.text) + (chip.text ? " " : "") + esc(it.title) + "</button>";
      });
      if (dayItems.length > 3) html += '<button class="cal-more" data-date="' + ds + '">+' + (dayItems.length - 3) + " 更多</button>";
      html += "</div></div>";
      cur.setDate(cur.getDate() + 1);
    }
    html += "</div>";
    $calendarView.innerHTML = html;
  }

  // ── 卡片详情浮层（复用核心 renderItemRow / handleRowAction）──
  function openCalDetail(item) {
    if (!item) return;
    calDetailItem = item;
    calDetailDirty = false;
    $calDetailBody.innerHTML = renderItemRow(item, { cls: "cal-detail-row", showSubject: true, collapsible: false, showArticleLink: false });
    $calDetailModal.classList.remove("hidden");
    syncBodyScrollLock();
    const row = $calDetailBody.querySelector(".cal-detail-row");
    if (row) {
      const ctxDiv = row.querySelector(".card-quote-context");
      const source = row.dataset.source;
      const sessionId = row.dataset.sessionId;
      const msgTime = row.dataset.msgtime;
      const msgId = row.dataset.msgid;
      if (source && sessionId) {
        fetchContext(ctxDiv, source, sessionId, parseInt(msgTime) || 0, msgId);
      } else {
        ctxDiv.innerHTML = '<p class=\'text-muted\'>缺少会话ID，无法加载上下文（旧数据不支持）</p>';
      }
    }
  }

  function closeCalDetail() {
    $calDetailModal.classList.add("hidden");
    syncBodyScrollLock();
    // 只在详情里真的改过数据时才重拉整月
    if (calendarMode && calDetailDirty) loadCalendar();
    calDetailDirty = false;
  }

  // ── 当日事件浮层（"+n" 入口）──
  function openCalDay(dateStr) {
    if (!dateStr) return;
    const items = calMonthItems.filter(it => calDaySet(it).has(dateStr));
    if (!items.length) return;
    const d = new Date(+dateStr.slice(0, 4), +dateStr.slice(5, 7) - 1, +dateStr.slice(8, 10));
    $calDayTitle.textContent = fmtMonthDay(d) + " · 共 " + items.length + " 条";
    $calDayList.innerHTML = items.slice().sort((a, b) =>
      ((a.start || a.end) || "").localeCompare((b.start || b.end) || ""))
      .map(it => {
        // 当日浮层同样按该日对应时间点渲染（任务名/时刻 + 过期样式）
        const chip = calChipLabel(it, dateStr);
        return '<button class="cal-chip' + (chip.expired ? " expired" : "") + '" data-id="' + escAttr(it.id) + '" style="--cat:' + escAttr(catColor.get(it.category) || "#6B7280") + '" title="' + escAttr(it.title) + '">' + esc(chip.text) + (chip.text ? " " : "") + esc(it.title) + "</button>";
      }).join("");
    $calDayModal.classList.remove("hidden");
    syncBodyScrollLock();
  }

  function closeCalDay() {
    $calDayModal.classList.add("hidden");
    syncBodyScrollLock();
  }

  // ── 事件绑定 ──
  function bindEvents() {
    $calendarBtn.addEventListener("click", (e) => {
      e.preventDefault();
      if (calendarMode) exitCalendarMode(); else enterCalendarMode();
    });
    $calendarView.addEventListener("click", (e) => {
      const nav = e.target.closest(".cal-nav");
      if (nav) {
        if (nav.dataset.nav === "prev") { calMonth -= 1; if (calMonth < 1) { calMonth = 12; calYear -= 1; } }
        else { calMonth += 1; if (calMonth > 12) { calMonth = 1; calYear += 1; } }
        loadCalendar();
        return;
      }
      if (e.target.closest(".cal-today")) {
        const now = new Date();
        calYear = now.getFullYear();
        calMonth = now.getMonth() + 1;
        loadCalendar();
        return;
      }
      if (e.target.closest(".cal-memo-toggle")) {
        calMemoOnly = !calMemoOnly;
        try { localStorage.setItem("briefdesk.calMemoOnly", calMemoOnly ? "1" : "0"); } catch { /* ignore */ }
        $calendarView.classList.add("cal-filter-anim"); // 触发 chip 错落浮现动画
        renderCalendar(calAllItems); // 本地过滤重渲染，不重新请求
        setTimeout(() => $calendarView.classList.remove("cal-filter-anim"), 600);
        return;
      }
      if (e.target.closest(".cal-exit")) {
        exitCalendarMode();
        return;
      }
      const chip = e.target.closest(".cal-chip");
      if (chip && chip.dataset.id) {
        const it = findCalendarItem(chip.dataset.id);
        if (it) openCalDetail(it);
        return;
      }
      // 当天事件超过 3 条的 "+n"：弹出当日全部事件
      const more = e.target.closest(".cal-more");
      if (more && more.dataset.date) {
        openCalDay(more.dataset.date);
        return;
      }
    });

    // 详情浮层：关闭/遮罩/分类跳转/主体时间线/行内操作
    document.getElementById("cal-detail-close").addEventListener("click", closeCalDetail);
    $calDetailModal.addEventListener("click", (e) => {
      if (e.target === $calDetailModal) { closeCalDetail(); return; }
      const catChip = e.target.closest(".card-category");
      if (catChip && catChip.dataset.cat) {
        const cat = catChip.dataset.cat;
        closeCalDetail();
        exitCalendarMode({ syncHash: false });
        clearSearch();
        currentCategory = cat;
        currentVerified = "unverified";
        updateActiveNav();
        syncHash();
        fetchData();
        return;
      }
      const subjLink = e.target.closest(".subject-link");
      if (subjLink && subjLink.dataset.subject) {
        const subj = subjLink.dataset.subject;
        closeCalDetail();
        openSubjectTimeline(subj);
        return;
      }
      const btn = e.target.closest("button");
      if (btn) {
        handleRowAction(e, {
          rowOf: () => $calDetailBody.querySelector(".cal-detail-row"),
          closeBothOnRecat: false, // 日历详情：修正分类只收起同类菜单（保持原行为）
          copyItemOf: () => calDetailItem || {},
          recatOption: (id, cat) => { calDetailDirty = true; doRecategorize(id, cat); },
          verify: (id, btn, row) => {
            const isMemo = row.classList.contains("memo");
            const isIgnored = row.classList.contains("ignored");
            const value = btn.classList.contains("btn-memo") ? (isMemo ? 0 : 1) : (isIgnored ? 0 : -1);
            calDetailDirty = true;
            verifyItem(id, value, row);
          },
        });
        return;
      }
    });

    // 当日事件浮层：关闭按钮/遮罩；条目点击 → 复用卡片详情浮层
    document.getElementById("cal-day-close").addEventListener("click", closeCalDay);
    $calDayModal.addEventListener("click", (e) => {
      if (e.target === $calDayModal) { closeCalDay(); return; }
      const chip = e.target.closest(".cal-chip");
      if (chip && chip.dataset.id) {
        const it = findCalendarItem(chip.dataset.id);
        if (it) openCalDetail(it);
      }
    });
  }

  // ── 核心视图钩子：hash 路由 / fetchData / Esc / 侧边栏数据联动 ──
  function registerViewHook() {
    registerPluginView({
      name: PLUGIN,
      matches: (v) => !!v && v.view === PLUGIN,
      hash: (v) => {
        if (v && v.view === PLUGIN) enterCalendarMode({ syncHash: false });
        else exitCalendarMode({ syncHash: false });
      },
      isActive: () => calendarMode,
      refresh: () => loadCalendar(),
      onEsc: () => {
        if (!$calDetailModal.classList.contains("hidden")) { closeCalDetail(); return true; }
        if (!$calDayModal.classList.contains("hidden")) { closeCalDay(); return true; }
        return false;
      },
      buildHash: () => calendarMode ? "#calendar" : null,
      sidebarReady: () => { if (calendarMode && calMonthItems.length) renderCalendar(calMonthItems); },
    });
  }

  // ── 入口：核心加载器注入本脚本后调用 ──
  function init(api) {
    if (!api || typeof api.isLoaded !== "function" || !api.isLoaded(PLUGIN)) return;
    try { calMemoOnly = localStorage.getItem("briefdesk.calMemoOnly") === "1"; } catch { /* ignore */ }
    buildDom();
    bindEvents();
    registerViewHook();
    inlineSvgIcons(); // 内联按钮图标（与核心图标一致）
    // F5 刷新 #calendar：加载器注入晚于核心 hash 初始化，此处自查补进入
    const v = parseHash();
    if (v && v.view === PLUGIN) enterCalendarMode({ syncHash: false });
  }

  window.briefdeskPlugins = window.briefdeskPlugins || {};
  window.briefdeskPlugins.calendar = { init: init };
})();

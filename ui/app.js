// ── State ──
let currentCategory = "全部";
let currentVerified = "unverified"; // "all" | "unverified" | "memo" | "ignored"
let currentSearch = ""; // 搜索词，非空时进入跨分类搜索模式
let preSearchCategory = ""; // 进入搜索前浏览的分类，清空搜索后恢复
let searchDebounceTimer = null;
let refreshIntervalSec = 300;
let catColor = new Map(); // 类别 → 颜色（/api/items 的 allCategories 携带）
let refreshTimer = null;
let selectAllBusy = false;
let saveBusy = false; // 设置弹窗保存防重入
let isSyncing = false; // 最近一次 fetchData 响应中的 status.syncing
let pendingChanges = null; // 同步中保存的待应用操作列表（同步完成后执行）
let fetchSeq = 0;
let stream = null;
let streamReconnectTimer = null;
let streamRefreshDebounceTimer = null;
let collapseGroups = localStorage.getItem("briefdesk.collapseGroups") !== "expanded"; // 默认折叠
let lastVisibleItems = []; // 最近一次渲染的可见卡（折叠开关切换时重渲染）
let viewSourceItems = [];   // 当前查询已加载的全部卡片（分页追加/模式切换的渲染基准）
let groupMap = new Map(); // subject+category → members（当前响应，浮层数据源）
let animateOnNextRender = false; // 折叠/展开切换后列表错落浮现（非切换刷新不做动画）

// ── 增量渲染与交互状态 ──
let currentItems = [];          // 当前已渲染（过滤后）的卡片数据，增量 diff 基准
let newItemIds = new Set();     // 自动刷新到达、尚未被用户确认的新卡片 id
let lastQueryKey = "";          // 上次完整查询签名，决定重置分页还是保留已加载页数
let searchCats = [];            // 搜索过滤条用的启用类别（含颜色）
let searchGroups = [];          // 搜索过滤条用的完整来源群选项（不受分页截断）
let searchFilterCat = "";       // 搜索模式下选中的类别过滤（"" = 全部类别）
let searchFilterRange = "";     // 搜索模式下的时间范围过滤："" | "1d" | "3d" | "7d"
let searchHistory = [];         // 搜索历史（localStorage briefdesk.searchHistory，最多 10 条）
let searchHistIndex = -1;       // 历史下拉中当前高亮项下标（-1 = 无）
let timeTicker = null;          // 相对时间每分钟刷新定时器
let manualSyncWait = false;     // 手动点击同步后等待 synced 事件
let syncWaitTimer = null;       // 手动同步兜底超时
let lastUndo = null;            // 最近一次可撤销的卡片操作 { id, prevValue }
let lastModalFocus = null;      // 弹窗打开前的焦点元素

// ── 操控层级状态（二级/三级界面）──
let overlayKey = "";            // 当前打开的浮层键 (subject + category)
let overlayNeedsSync = false;   // 浮层内做过操作，关闭时需刷新主列表
let overlayScrolls = new Map(); // subjectKey -> 浮层滚动位置（重开恢复）
let overlayExpandedIds = new Set(); // 浮层内展开详情行的 id
let currentViewKey = "";        // 当前视图键（分类+验证态+搜索词）
let currentExpandedIds = new Set(); // 当前视图展开的卡片 id
let quoteExpandedIds = new Set();   // 原文引用已展开全部内容的卡片 id（跨视图保留）
let kbFocusIndex = -1;        // 键盘焦点卡片下标（kbUnits，-1 = 无焦点）
let kbUnits = [];             // 当前可见卡片单元（主列表 .item-card，折叠模式为代表卡）
let kbFadeTimer = null;       // 焦点淡出定时器（无键盘操作一段时间后自动淡出）
const KB_FOCUS_FADE_MS = 4000;         // 无操作多少毫秒后开始淡出
const KB_FOCUS_FADE_TRANSITION_MS = 400; // 淡出过渡时长
let viewStates = new Map();     // viewKey -> { expandedIds:Set, scrollTop:number }
let contextCache = new Map();   // "source|session|t" -> /api/context 的 messages 数组（高亮按卡片现渲染）
let pendingScrollTop = 0;       // 全量渲染后要恢复的滚动位置
// 灯箱缩放/拖拽
let lightboxScale = 1;
let lightboxTx = 0;
let lightboxTy = 0;
let lightboxDrag = null;        // { startX, startY, tx, ty }

// ── 设置弹窗草稿（点"保存"才应用）──
let catDraft = null; // 类别草稿行 [{key, id|null, name, prompt, color, enabled, item_count}]
let catOriginal = []; // 打开弹窗时的服务端类别快照（diff 基准）
let catDeleted = []; // 待删除类别 [{key, row, purgeItems}]
let sessionOriginal = []; // 打开弹窗时的服务端会话快照（diff 基准）
let selectedTypes = new Set(); // 群聊筛选类型（多选）：选中的类型；空集 = 全部/不筛选
let selectedSources = new Set(); // 群聊筛选消息源（多选）：选中的源；空集 = 全部/不筛选
let sessionTimeFilter = "all";  // 群聊筛选按时间过滤：'all'（不过滤）或小时数；默认取服务端 BACKFILL_HOURS
let sessionDefaultBackfill = 24; // 服务端默认回填窗口（BACKFILL_HOURS，来自 /api/sessions backfillHours）
const SESSION_TIME_PRESETS = { "6": 6, "12": 12, "24": 24, "48": 48, "72": 72, "168": 168 };
let enabledSources = []; // 实际启用消息源（/api/status 的 sources keys，决定筛选芯片）
// 首次使用向导 step2 的会话筛选状态（与设置「群聊筛选」同规则、独立状态，互不干扰）
let onboardTypes = new Set();    // 类型多选：空集 = 全部
let onboardSources = new Set();  // 消息源多选：空集 = 全部
let onboardSearch = "";          // 会话名称搜索词
let onboardTime = "all";         // 时间过滤：'all' 或小时数
let catSeq = 0; // 新增类别行的临时 key 计数器

// ── 第一梯队功能状态 ──
let batchMode = false;          // 批量操作模式
let selectedIds = new Set();    // 批量模式已选卡片 id
let hasMore = false;            // /api/items 是否还有下一页
let nextOffset = 0;             // 服务端返回的下一页偏移（不使用过滤后卡片数推导）
let loadedPageCount = 1;        // 当前已加载页数；自动刷新按此页数重新获取
let pageLoading = false;        // “加载更多”请求互斥/按钮状态
let listRefreshing = false;     // 自动/SSE 刷新多页期间禁止并发加载下一页
let activeItemQuery = null;     // 最近成功列表查询的参数快照（加载更多必须复用）
let searchFilterGroup = "";     // 搜索模式下的来源群过滤（"" = 全部来源）
let subscriptions = [];         // 关键词订阅 [{id, keywords, enabled}]
let blocklist = [];             // 降噪黑名单 [{id, keywords, enabled}]（命中卡片渲染时隐藏）
let notifyMode = "off";         // 桌面通知模式：off | all | keywords
let lastNotifyAt = 0;           // 通知节流时间戳

// ── 时效 / 时间线状态 ──
let hideExpired = false;            // 隐藏截止时间已过的卡片
let timeline = null;                // 主体时间线 {subject, items, offset, hasMore, count, expandedIds}
const PAGE_SIZE = 100;          // 每页卡片数（后端 limit 上限 200）
const TITLE_BASE = "简报台 (BRIEFDESK)";

// ── Elements ──
const $nav = document.getElementById("category-nav");
const $itemSearch = document.getElementById("item-search");
const $itemSearchClear = document.getElementById("item-search-clear");
const $content = document.getElementById("content");
const $itemsContainer = document.getElementById("items-container");
const $loadingState = document.getElementById("loading-state");
const $emptyState = document.getElementById("empty-state");
const $filteredEmptyState = document.getElementById("filtered-empty-state");
const $statusText = document.getElementById("status-text");
const $statusProgress = document.getElementById("status-progress");
const $statusIndicator = document.getElementById("status-indicator");
const $settingsModal = document.getElementById("settings-modal");
const $settingsMenu = document.getElementById("settings-menu");
const $refreshInterval = document.getElementById("refresh-interval");
const $memoLink = document.getElementById("memo-link");
const $ignoredLink = document.getElementById("ignored-link");
const $settingsSave = document.getElementById("settings-save");
const $settingsClose = document.getElementById("settings-close");
const $syncBtn = document.getElementById("sync-btn");
const $settingsLinkTop = document.getElementById("settings-link-top");
const $sessionList = document.getElementById("session-list");
const $sessionSearch = document.getElementById("session-search");
const $sessionTypeFilter = document.getElementById("session-type-filter");
const $sessionSourceFilter = document.getElementById("session-source-filter");
const $sessionSourceGroup = document.getElementById("session-source-group");
const $onboardSessionSearch = document.getElementById("onboard-session-search");
const $onboardSessionList = document.getElementById("onboard-sessions");
const $onboardTypeFilter = document.getElementById("onboard-type-filter");
const $onboardSourceFilter = document.getElementById("onboard-source-filter");
const $onboardSourceGroup = document.getElementById("onboard-source-group");
const $onboardTimePreset = document.getElementById("onboard-time-preset");
const $onboardTimeCustom = document.getElementById("onboard-time-custom");
const $sessionTimePreset = document.getElementById("session-time-preset");
const $sessionTimeCustom = document.getElementById("session-time-custom");
const $categoryToggles = document.getElementById("category-toggles");
const $categoryAdd = document.getElementById("category-add");
const $catAddForm = document.getElementById("cat-add-form");
const $catAddName = document.getElementById("cat-add-name");
const $catAddPrompt = document.getElementById("cat-add-prompt");
const $catAddPalette = document.getElementById("cat-add-palette");
const $lightbox = document.getElementById("lightbox");
const $lightboxImg = document.getElementById("lightbox-img");
const $lightboxPrev = document.getElementById("lightbox-prev");
const $lightboxNext = document.getElementById("lightbox-next");
const $lightboxClose = document.getElementById("lightbox-close");
const $lightboxCounter = document.getElementById("lightbox-counter");
const $collapseToggle = document.getElementById("collapse-toggle");
const $groupOverlay = document.getElementById("group-overlay");
const $groupOverlayTitle = document.getElementById("group-overlay-title");
const $groupOverlayList = document.getElementById("group-overlay-list");

let lightboxSrcs = [];
let lightboxIndex = 0;

const $toastContainer = document.getElementById("toast-container");
const $kbHelpModal = document.getElementById("kb-help-modal");
const $onboardModal = document.getElementById("onboard-modal");
const $newItemsBar = document.getElementById("new-items-bar");
const $newItemsBarText = document.getElementById("new-items-bar-text");
const $newItemsBarBtn = document.getElementById("new-items-bar-btn");
const $filterBar = document.getElementById("filter-bar");
const $groupOverlayContent = document.querySelector("#group-overlay .modal-content");
const $listCount = document.getElementById("list-count");
const $subsLink = document.getElementById("subs-link");
const $batchToggle = document.getElementById("batch-toggle");
const $batchBar = document.getElementById("batch-bar");
const $batchCount = document.getElementById("batch-count");
const $batchSelectAll = document.getElementById("batch-select-all");
const $batchMemo = document.getElementById("batch-memo");
const $batchIgnore = document.getElementById("batch-ignore");
const $batchUnverify = document.getElementById("batch-unverify");
const $batchDelete = document.getElementById("batch-delete");
const $batchExit = document.getElementById("batch-exit");
const $batchConfirmModal = document.getElementById("batch-confirm-modal");
const $batchConfirmN = document.getElementById("batch-confirm-n");
const $batchConfirmDelete = document.getElementById("batch-confirm-delete");
const $batchConfirmCancel = document.getElementById("batch-confirm-cancel");
const $loadMoreWrap = document.getElementById("load-more-wrap");
const $statusPopover = document.getElementById("status-popover");
const $themeToggle = document.getElementById("theme-toggle");
const $notifyMode = document.getElementById("notify-mode");
const $subsList = document.getElementById("subs-list");
const $subsAdd = document.getElementById("subs-add");
const $subsAddForm = document.getElementById("subs-add-form");
const $subsAddKw = document.getElementById("subs-add-kw");
const $blockList = document.getElementById("block-list");
const $blockAdd = document.getElementById("block-add");
const $blockAddForm = document.getElementById("block-add-form");
const $blockAddKw = document.getElementById("block-add-kw");
const $hideExpiredToggle = document.getElementById("hide-expired-toggle");
const $timelineModal = document.getElementById("subject-timeline-modal");
const $timelineTitle = document.getElementById("timeline-title");
const $timelineList = document.getElementById("timeline-list");
const $timelineLoadMoreWrap = document.getElementById("timeline-load-more-wrap");
const $pluginsList = document.getElementById("plugins-list");

// ── Init ──
document.addEventListener("DOMContentLoaded", () => {
  inlineSvgIcons();
  preloadSvgIcons();
  loadSettings();
  initTheme();
  applyRandomFavicon();
  loadSearchHistory();
  loadSubscriptions();
  loadBlocklist();
  initOnboarding(); // 首次使用向导（异步检查，不阻塞首屏）
  initViewFromHash();
  // 先装配插件前端（注入 ui.js / 注册行内扩展等）再拉首屏数据：
  // 行内扩展（提醒按钮等）注册完成前不渲染列表，避免竞态导致按钮缺失
  // （此前 /api/items 先于插件装配返回时，首屏无提醒按钮，须等重渲染才出现）
  loadPluginFrontends().finally(() => fetchData());
  startRefreshTimer();
  startTimeTicker();
  connectRealtimeStream();
  setupEvents();
});

// ── Inline SVG Icons ──
// <img> 引用的外部 SVG 中 fill="currentColor" 不继承父元素颜色（浏览器渲染为黑色），
// 因此把图标 fetch 后内联进 DOM（<span> 包裹），颜色由 CSS color 统一控制。
const _svgCache = new Map();

// 预取 UI 用到的全部图标，使后续渲染内联命中缓存（同步替换，不闪黑）
function preloadSvgIcons() {
  const paths = new Set();
  document.querySelectorAll("img[src$='.svg']").forEach((img) => {
    const src = img.getAttribute("src");
    if (src) paths.add(src);
  });
  Object.values(_CAT_ICONS).forEach((p) => p && paths.add(p));
  _CAT_PALETTE.forEach((c) => paths.add(c.icon));
  Object.values(_STATUS_ICONS).forEach((p) => paths.add(p));
  paths.add("/图标/8-界面/箭头上.svg");
  paths.add("/图标/8-界面/箭头下.svg");
  paths.add("/图标/10-编辑/复制.svg");   // 卡片"复制"按钮（动态渲染）
  paths.add("/图标/8-界面/更改.svg");    // 卡片"修正分类"按钮（动态渲染）
  paths.add("/图标/9-媒体/闹钟.svg");    // 卡片"提醒"按钮（动态渲染）
  for (const src of paths) {
    if (_svgCache.has(src)) continue;
    fetch(src)
      .then((res) => (res.ok ? res.text() : null))
      .then((t) => {
        if (t && t.trim().startsWith("<svg")) _svgCache.set(src, t);
      })
      .catch(() => { });
  }
}

async function inlineSvgIcons(root = document) {
  root.querySelectorAll("img.icon, img.icon-sm, img.icon-lg").forEach((img) => {
    const src = img.getAttribute("src") || "";
    if (!src.endsWith(".svg")) return;
    _loadInlineSvg(img, src);
  });
}

async function _loadInlineSvg(img, src) {
  try {
    let svgText = _svgCache.get(src);
    if (svgText === undefined) {
      const res = await fetch(src);
      if (!res.ok) return;
      svgText = await res.text();
      // SPA fallback 对不存在的路径会返回 index.html，防御性跳过
      if (!svgText.trim().startsWith("<svg")) return;
      _svgCache.set(src, svgText);
    }
    if (!img.isConnected) return; // 等待期间该节点已被替换/移除
    const span = document.createElement("span");
    span.className = img.className;
    if (img.id) span.id = img.id;
    if (img.alt) span.setAttribute("aria-label", img.alt);
    span.innerHTML = svgText;
    img.replaceWith(span);
  } catch {
    // 保留原始 <img>（黑色兜底），不阻断页面
  }
}

// 动态渲染（innerHTML 更新）后立即内联新增图标：
// MutationObserver 回调在浏览器渲染前执行，缓存命中时替换是同步的，
// 渲染与替换落在同一帧，不会出现黑色图标闪烁
const _svgObserver = new MutationObserver(() => inlineSvgIcons());
_svgObserver.observe(document.body, { childList: true, subtree: true });

window.addEventListener("beforeunload", () => {
  if (stream) {
    stream.close();
    stream = null;
  }
  if (streamReconnectTimer) {
    clearTimeout(streamReconnectTimer);
    streamReconnectTimer = null;
  }
});

// ── 深色模式：跟随系统时监听系统主题变化 ──
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
  if ((localStorage.getItem("briefdesk.theme") || "light") === "system") applyTheme();
});

// ── 标签页重新可见：恢复标题（保留新卡片高亮，待用户滚动确认）──
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && document.title !== TITLE_BASE) document.title = TITLE_BASE;
});

// ── Events ──
function setupEvents() {
  $nav.addEventListener("click", (e) => {
    const link = e.target.closest(".cat-link");
    if (!link) return;
    e.preventDefault();
    exitPluginViews(); // 插件视图（日历等）→ 列表视图
    clearSearch();
    currentCategory = link.dataset.category;
    currentVerified = link.dataset.verified || "unverified";
    updateActiveNav();
    syncHash();
    fetchData();
  });

  // "查看全部" link in filtered-empty-state
  $content.addEventListener("click", (e) => {
    const link = e.target.closest(".reset-filter-link");
    if (!link) return;
    e.preventDefault();
    exitPluginViews();
    clearSearch();
    currentCategory = "全部";
    currentVerified = "unverified";
    updateActiveNav();
    syncHash();
    fetchData();
  });

  $memoLink.addEventListener("click", (e) => {
    e.preventDefault();
    exitPluginViews();
    clearSearch();
    currentCategory = "全部";
    currentVerified = "memo";
    updateActiveNav();
    syncHash();
    fetchData();
  });

  $ignoredLink.addEventListener("click", (e) => {
    e.preventDefault();
    exitPluginViews();
    clearSearch();
    currentCategory = "全部";
    currentVerified = "ignored";
    updateActiveNav();
    syncHash();
    fetchData();
  });

  // ── Sidebar search ──
  $itemSearch.addEventListener("input", () => {
    if (searchDebounceTimer) clearTimeout(searchDebounceTimer);
    searchDebounceTimer = setTimeout(() => {
      searchDebounceTimer = null;
      applySearch($itemSearch.value.trim());
    }, 300);
  });

  // 搜索框键盘：历史导航（↑/↓）+ Enter 提交/选择历史项
  $itemSearch.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      const items = Array.from($searchHistoryDropdown.querySelectorAll(".search-history-item"));
      if (!items.length) return;
      e.preventDefault();
      const delta = e.key === "ArrowDown" ? 1 : -1;
      searchHistIndex = (searchHistIndex + delta + items.length) % items.length;
      items.forEach((el, i) => el.classList.toggle("active", i === searchHistIndex));
      if (items[searchHistIndex]) items[searchHistIndex].scrollIntoView({ block: "nearest" });
      return;
    }
    if (e.key !== "Enter") return;
    // 历史下拉开着且高亮某项 → 选择该项
    if (!$searchHistoryDropdown.classList.contains("hidden") && searchHistIndex >= 0) {
      const item = $searchHistoryDropdown.querySelectorAll(".search-history-item")[searchHistIndex];
      if (item) { $itemSearch.value = item.textContent.trim(); applySearch($itemSearch.value); }
      return;
    }
    const first = $itemsContainer.querySelector(".item-card, .group-collapsed, .group-header");
    if (first) first.scrollIntoView({ behavior: "smooth", block: "start" });
  });

  // 搜索历史下拉：聚焦为空时显示，点击项选择（mousedown 先于 blur 触发，防下拉先隐藏）
  $itemSearch.addEventListener("focus", () => renderSearchHistoryDropdown(true));
  $itemSearch.addEventListener("blur", () => setTimeout(() => renderSearchHistoryDropdown(false), 200));
  $itemSearch.addEventListener("input", () => {
    renderSearchHistoryDropdown(false); // 有输入时隐藏历史
  });
  // 点击下拉/输入框之外任意处 → 关闭（mousedown 兜底，不依赖 blur 时序）
  document.addEventListener("mousedown", (e) => {
    if ($searchHistoryDropdown.classList.contains("hidden")) return;
    if (e.target.closest("#search-history-dropdown") || e.target.closest("#item-search")) return;
    renderSearchHistoryDropdown(false);
  });
  $searchHistoryDropdown.addEventListener("mousedown", (e) => {
    const item = e.target.closest(".search-history-item");
    if (!item) return;
    e.preventDefault(); // 保持输入框焦点
    $itemSearch.value = item.textContent.trim();
    applySearch($itemSearch.value);
  });

  $itemSearchClear.addEventListener("click", () => {
    $itemSearch.value = "";
    applySearch("");
  });

  function openSettings(e) {
    e.preventDefault();
    lastModalFocus = document.activeElement;
    $settingsModal.classList.remove("hidden");
    syncBodyScrollLock();
    setSettingsPanel("general"); // 二级菜单：每次打开默认回到「常规」
    $sessionSearch.value = "";
    selectedTypes.clear(); // 每次打开重置类型筛选（回到"全部"）
    updateSessionTypeChips();
    selectedSources.clear(); // 每次打开重置消息源筛选（回到"全部"）
    loadSourceFilterChips(); // 按 /api/status 实际启用源渲染多选芯片
    loadSessions();
    loadCategories();
    loadAboutSources();
    loadPlugins();
    setTimeout(() => $refreshInterval.focus(), 0);
  }

  // 二级菜单切换：仅显示选中分组的面板，草稿跨分组保留（保存时统一提交）
  $settingsMenu.addEventListener("click", (e) => {
    const btn = e.target.closest(".settings-menu-item");
    if (!btn) return;
    setSettingsPanel(btn.dataset.panel);
  });

  $settingsLinkTop.addEventListener("click", openSettings);

  // ── Category management (delegation on #category-toggles) ──
  // 草稿模式：勾选只改本地草稿（catDraft），点设置弹窗"保存"后才调 API
  $categoryToggles.addEventListener("change", (e) => {
    const cb = e.target.closest("input[type=checkbox][data-cat-id]");
    if (!cb) return;
    const item = catDraft && catDraft.find(c => c.key === cb.dataset.catId);
    if (item) item.enabled = cb.checked ? 1 : 0;
  });

  // 类别行按钮委托（行渲染在 #category-toggles 内，冒泡可达）
  $categoryToggles.addEventListener("click", (e) => {
    const btn = e.target.closest("button");
    if (!btn) return;
    const row = btn.closest(".cat-row");
    const key = row ? row.dataset.catId : null;

    if (btn.classList.contains("cat-edit")) {
      enterCategoryEdit(row, btn);
    } else if (btn.classList.contains("cat-edit-save")) {
      confirmEditCategory(row);
    } else if (btn.classList.contains("cat-edit-cancel")) {
      renderCategoryToggles(); // 重渲染退出编辑态
    } else if (btn.classList.contains("cat-del")) {
      if (row.dataset.isNew === "1") {
        markDelete(key, false); // 新增行无历史卡片，直接移除
      } else {
        row.querySelector(".cat-del-confirm").classList.remove("hidden");
        btn.classList.add("hidden");
      }
    } else if (btn.classList.contains("cat-del-cancel")) {
      row.querySelector(".cat-del-confirm").classList.add("hidden");
      row.querySelector(".cat-del").classList.remove("hidden");
    } else if (btn.classList.contains("cat-del-keep")) {
      markDelete(key, false);
    } else if (btn.classList.contains("cat-del-purge")) {
      markDelete(key, true);
    } else if (btn.classList.contains("cat-undo")) {
      undoDelete(key);
    }
  });

  // 行内编辑表单的色板选择（在 #category-toggles 内，冒泡可达）
  $categoryToggles.addEventListener("click", (e) => {
    const swatch = e.target.closest(".cat-swatch");
    if (!swatch) return;
    const palette = swatch.closest(".cat-palette");
    palette.querySelectorAll(".cat-swatch").forEach(s => {
      s.classList.toggle("selected", s === swatch);
    });
  });

  $categoryAdd.addEventListener("click", () => {
    $catAddForm.classList.remove("hidden");
    $categoryAdd.classList.add("hidden");
    $catAddName.value = "";
    $catAddPrompt.value = "";
    renderPalette($catAddPalette, "#6B7280");
    $catAddName.focus();
  });

  // 添加表单（#cat-add-form）是 #category-toggles 的兄弟元素，委托绑在
  // #category-toggles 上收不到其内部事件，须直接绑定到表单自身
  $catAddForm.addEventListener("click", (e) => {
    const btn = e.target.closest("button");
    if (!btn) return;
    if (btn.id === "cat-add-save") {
      confirmAddCategory();
    } else if (btn.id === "cat-add-cancel") {
      $catAddForm.classList.add("hidden");
      $categoryAdd.classList.remove("hidden");
    }
  });

  // 添加表单色板选择
  $catAddPalette.addEventListener("click", (e) => {
    const swatch = e.target.closest(".cat-swatch");
    if (!swatch) return;
    $catAddPalette.querySelectorAll(".cat-swatch").forEach(s => {
      s.classList.toggle("selected", s === swatch);
    });
  });

  // Session toggle (draft mode): 勾选只改本地 checkbox 状态，不调 API；
  // 点设置弹窗"保存"后按 sessionOriginal 快照 diff 统一应用
  $sessionList.addEventListener("change", (e) => {
    const cb = e.target.closest("input[type=checkbox]");
    if (!cb) return;
    updateSessionSelectAll(); // 全选框三态（全选/半选/未选）随勾选实时更新
  });

  // Refresh sessions from source
  document.getElementById("session-refresh").addEventListener("click", async () => {
    const btn = document.getElementById("session-refresh");
    const icon = document.getElementById("session-refresh-icon");
    const text = document.getElementById("session-refresh-text");
    btn.disabled = true;
    icon.classList.add("icon-spin");
    text.textContent = "发现中...";
    try {
      const res = await fetch("/api/sessions/refresh", { method: "POST" });
      const data = await res.json();
      renderSessions(data.sessions || []);
      fetchData();
    } catch (err) {
      console.error("Refresh sessions error:", err);
    } finally {
      btn.disabled = false;
      icon.classList.remove("icon-spin");
      text.textContent = "发现新群聊";
    }
  });

  // Session search filter
  $sessionSearch.addEventListener("input", applySessionFilters);

  // 消息类型筛选（多选，与消息源筛选/名称搜索叠加生效）：
  // "全部" = 清空选中；类型 chip 独立开关；取消最后一个类型后回到"全部"（空集），不会出现空列表
  $sessionTypeFilter.addEventListener("click", (e) => {
    const chip = e.target.closest(".filter-chip");
    if (!chip) return;
    const type = chip.dataset.type;
    if (!type || type === "all") {
      selectedTypes.clear();
    } else if (selectedTypes.has(type)) {
      selectedTypes.delete(type);
    } else {
      selectedTypes.add(type);
    }
    updateSessionTypeChips();
    applySessionFilters();
  });

  // 消息源筛选（多选，与类型筛选/搜索叠加生效）：
  // "全部" = 清空选中；源 chip 独立开关；取消最后一个源后回到"全部"（空集），不会出现空列表
  $sessionSourceFilter.addEventListener("click", (e) => {
    const chip = e.target.closest(".filter-chip");
    if (!chip) return;
    const src = chip.dataset.source;
    if (src === "all") {
      selectedSources.clear();
    } else if (selectedSources.has(src)) {
      selectedSources.delete(src);
    } else {
      selectedSources.add(src);
    }
    updateSessionSourceChips();
    applySessionFilters();
  });

  // 按时间过滤（预设下拉 + 自定义小时常驻共存，与类型/源/搜索叠加生效，仅显示层）：
  // 下拉 = 快捷设置 + 档位指示；选中“自定义”仅聚焦输入框等用户输入；输入框空/0 视为“全部”
  $sessionTimePreset.addEventListener("change", () => {
    const v = $sessionTimePreset.value;
    if (v === "custom") {
      $sessionTimeCustom.focus(); // 仅聚焦输入框，保留当前值等用户修改
    } else {
      setSessionTimeFilter(v === "all" ? "all" : Number(v));
    }
  });
  $sessionTimeCustom.addEventListener("input", () => {
    setSessionTimeFilter($sessionTimeCustom.value); // 空/0 → all；数字 → 过滤
  });

  // "保存"统一应用三类更改：刷新间隔（localStorage）+ 类别草稿 + 会话草稿。
  // 同步进行中时延迟到同步完成后应用（本次同步按旧配置跑，避免数据与配置不一致）。
  $settingsSave.addEventListener("click", async () => {
    if (saveBusy) return;
    saveBusy = true;
    $settingsSave.disabled = true;
    try {
      saveSettings(); // 刷新间隔与同步数据无关，立即生效
      const ops = collectAllOps(); // 保存时快照，弹窗重开/草稿重载不影响挂起
      if (!ops) return; // collectAllOps 已弹窗说明（未加载/名称冲突），中止本次保存
      // 实时查询同步状态（isSyncing 是缓存值，另一标签页/启动首轮可能已开始同步）
      let syncingNow = isSyncing;
      try {
        const statusRes = await fetch("/api/status");
        if (statusRes.ok) syncingNow = !!(await statusRes.json()).syncing;
      } catch { /* 查询失败回退缓存值 */ }
      if (syncingNow) {
        pendingChanges = ops; // 覆盖旧挂起项，最新意图为准
        showToast("当前正在同步，更改将在同步完成后自动应用", { type: "info", duration: 6000 });
      } else {
        pendingChanges = null; // 直接应用时丢弃历史挂起项（最新保存为准），
                               // 否则 fetchData 会在其后再应用一遍旧操作
        await runSettingsOps(ops);
      }
      closeSettingsModal();
      showToast("设置已保存", { type: "success", duration: 2500 });
      startRefreshTimer();
      fetchData();
    } catch (err) {
      console.error("Save settings error:", err);
      showToast("保存失败，部分更改可能未生效，请重试", { type: "error", duration: 6000 });
      // 已应用的前缀操作（如删除）不可回滚：重载草稿对齐服务端真相，
      // 避免基于过期草稿重复操作（对已删类别再删 → 404）
      await loadCategories();
      await loadSessions();
    } finally {
      saveBusy = false;
      $settingsSave.disabled = false;
    }
  });

  $settingsClose.addEventListener("click", closeSettingsModal);

  $settingsModal.addEventListener("click", (e) => {
    if (e.target === $settingsModal) closeSettingsModal();
  });

  // 立即同步：接后端真实状态——409 提示已在同步；成功则等 synced SSE 事件
  // （或 120s 兜底超时）恢复按钮，不再假装转 2 秒
  $syncBtn.addEventListener("click", async () => {
    if ($syncBtn.disabled) return;
    try {
      const res = await fetch("/api/sync", { method: "POST" });
      if (res.status === 409) {
        showToast("同步已在后台进行中，无需重复触发", { type: "info", duration: 4000 });
        return;
      }
      if (!res.ok) throw new Error("HTTP " + res.status);
      manualSyncWait = true;
      setSyncButton(true);
      if (syncWaitTimer) clearTimeout(syncWaitTimer);
      syncWaitTimer = setTimeout(() => {
        syncWaitTimer = null;
        manualSyncWait = false;
        setSyncButton(false);
        showToast("同步耗时较长，仍在后台进行中", { type: "info", duration: 4000 });
      }, 120000);
    } catch (err) {
      console.error("Sync error:", err);
      showToast("同步失败，请检查后端状态后重试", { type: "error", duration: 5000 });
    }
  });

  // Item actions delegation
  $itemsContainer.addEventListener("click", (e) => {
    // 批量模式：折叠组 = 整组选择单元（点组头或代表卡任意位置切换整组）；
    // 平铺/单卡 = 单卡选择；勾选框自身走 change 事件
    if (batchMode) {
      const coll = e.target.closest(".group-collapsed");
      if (coll && coll.dataset.key) {
        if (e.target.closest(".batch-check")) return; // 勾选框：change 事件处理
        toggleGroupSelect(coll.dataset.key, coll);
        return;
      }
      const ghead = e.target.closest(".group-header");
      if (ghead && ghead.dataset.key) {
        if (e.target.closest(".batch-check")) return; // 勾选框：change 事件处理
        toggleGroupSelect(ghead.dataset.key, ghead);
        return;
      }
      const card = e.target.closest(".item-card");
      if (!card) return;
      if (e.target.closest(".batch-check")) return;
      toggleSelect(String(card.dataset.id), card);
      return;
    }

    // 类别标签 → 跳转到该分类视图（chip 样式暗示可点）
    const catChip = e.target.closest(".card-category");
    if (catChip && catChip.dataset.cat) {
      const cat = catChip.dataset.cat;
      clearSearch();
      currentCategory = cat;
      currentVerified = "unverified";
      updateActiveNav();
      syncHash();
      fetchData();
      return;
    }

    // 主体名 → 打开主体时间线（折叠/平铺组头与单卡主体链接）
    const subjLink = e.target.closest(".subject-link");
    if (subjLink && subjLink.dataset.subject) {
      openSubjectTimeline(subjLink.dataset.subject);
      return;
    }

    // Image click → lightbox
    const img = e.target.closest(".card-images img");
    if (img) {
      const card = img.closest(".item-card");
      const srcs = Array.from(card.querySelectorAll(".card-images img")).map(el => el.src);
      openLightbox(srcs, srcs.indexOf(img.src));
      return;
    }

    // Handle quote toggle (div, not button)
    const toggle = e.target.closest(".card-quote-toggle");
    if (toggle) {
      const card = toggle.closest(".item-card");
      const quote = card.querySelector(".card-quote");
      const isOpen = !quote.classList.contains("open");
      quote.classList.toggle("open");
      toggle.innerHTML = isOpen
        ? '<img src="/图标/8-界面/箭头上.svg" class="icon-sm" alt="">原文引用'
        : '<img src="/图标/8-界面/箭头下.svg" class="icon-sm" alt="">原文引用';

      // 展开状态记入视图状态，视图切换后恢复
      if (isOpen) currentExpandedIds.add(card.dataset.id);
      else currentExpandedIds.delete(card.dataset.id);

      // Fetch context on first expand
      if (isOpen) {
        const ctxDiv = card.querySelector(".card-quote-context");
        if (ctxDiv && ctxDiv.classList.contains("hidden")) {
          ctxDiv.classList.remove("hidden");
          const source = card.dataset.source;
          const sessionId = card.dataset.sessionId;
          const msgTime = card.dataset.msgtime;
          const msgId = card.dataset.msgid;
          if (source && sessionId) {
            fetchContext(ctxDiv, source, sessionId, parseInt(msgTime) || 0, msgId);
          } else {
            ctxDiv.innerHTML = '<p class="text-muted">缺少会话ID，无法加载上下文（旧数据不支持）</p>';
          }
        }
      }
      return;
    }

    const btn = e.target.closest("button");
    if (!btn) return;

    // 折叠组入口：打开同主体浮层
    if (btn.classList.contains("group-more-btn")) {
      openGroupOverlay(btn.dataset.subject, btn.dataset.cat);
      return;
    }

    const card = btn.closest(".item-card");
    if (!card) return;

    if (handleRowAction(e, {
      rowOf: () => card,
      closeBothOnRecat: true, // 列表卡：修正分类/提醒菜单互斥收起（保持原行为）
      copyItemOf: () => currentItems.find(x => String(x.id) === card.dataset.id),
      recatOption: (id, cat) => doRecategorize(id, cat),
      verify: (id, btn, row) => {
        const isActive = btn.classList.contains("active");
        const value = btn.classList.contains("btn-memo") ? (isActive ? 0 : 1) : (isActive ? 0 : -1);
        // 折叠组内的代表卡操作后重拉整组（组结构/代表卡可能变化）
        verifyItem(id, value, row, { refresh: !!row.closest(".group-collapsed") });
      },
    })) return;
  });

  // Lightbox controls
  $lightboxClose.addEventListener("click", closeLightbox);
  $lightboxPrev.addEventListener("click", () => lightboxStep(-1));
  $lightboxNext.addEventListener("click", () => lightboxStep(1));
  $lightbox.addEventListener("click", (e) => {
    if (e.target === $lightbox) closeLightbox();
  });

  // 灯箱缩放：滚轮放大/缩小（围绕中心），双击复位
  $lightboxImg.addEventListener("wheel", (e) => {
    e.preventDefault();
    const factor = e.deltaY < 0 ? 1.15 : 1 / 1.15;
    lightboxScale = Math.min(6, Math.max(1, lightboxScale * factor));
    if (lightboxScale === 1) { lightboxTx = 0; lightboxTy = 0; }
    applyLightboxTransform();
  }, { passive: false });
  $lightboxImg.addEventListener("dblclick", () => {
    lightboxScale = 1; lightboxTx = 0; lightboxTy = 0;
    applyLightboxTransform();
  });
  // 放大后拖拽平移
  $lightboxImg.addEventListener("mousedown", (e) => {
    if (lightboxScale <= 1) return;
    e.preventDefault();
    lightboxDrag = { startX: e.clientX, startY: e.clientY, tx: lightboxTx, ty: lightboxTy };
    if (e.pointerId !== undefined) $lightboxImg.setPointerCapture(e.pointerId);
    $lightboxImg.classList.add("dragging");
  });
  $lightboxImg.addEventListener("mousemove", (e) => {
    if (!lightboxDrag) return;
    lightboxTx = lightboxDrag.tx + (e.clientX - lightboxDrag.startX);
    lightboxTy = lightboxDrag.ty + (e.clientY - lightboxDrag.startY);
    applyLightboxTransform();
  });
  const endLightboxDrag = () => {
    lightboxDrag = null;
    $lightboxImg.classList.remove("dragging");
  };
  $lightboxImg.addEventListener("mouseup", endLightboxDrag);
  $lightboxImg.addEventListener("mouseleave", endLightboxDrag);

  // Collapse toggle（折叠/展开分段控制，状态存 localStorage）
  $collapseToggle.addEventListener("click", (e) => {
    const segBtn = e.target.closest(".seg-btn");
    if (!segBtn) return;
    const wantCollapsed = segBtn.dataset.mode === "collapsed";
    if (wantCollapsed === collapseGroups) return;
    collapseGroups = wantCollapsed;
    localStorage.setItem("briefdesk.collapseGroups", collapseGroups ? "collapsed" : "expanded");
    updateCollapseToggle(); // 立即同步按钮选中态（不依赖渲染路径，空视图/异常时也即时响应）
    animateOnNextRender = true; // 模式切换 → 列表错落浮现
    renderItems(viewSourceItems, { full: true });
  });

  // Group overlay（同主体卡片浮层）
  document.getElementById("group-overlay-close").addEventListener("click", closeGroupOverlay);
  $groupOverlay.addEventListener("click", (e) => {
    if (e.target === $groupOverlay) {
      closeGroupOverlay();
      return;
    }
    // 类别标签 → 关闭浮层并跳转到该分类
    const catChip = e.target.closest(".card-category");
    if (catChip && catChip.dataset.cat) {
      const cat = catChip.dataset.cat;
      closeGroupOverlay();
      clearSearch();
      currentCategory = cat;
      currentVerified = "unverified";
      updateActiveNav();
      syncHash();
      fetchData();
      return;
    }
    // 图片 → 放大查看
    const img = e.target.closest(".card-images img");
    if (img) {
      const row = img.closest(".ov-row");
      const srcs = Array.from(row.querySelectorAll(".card-images img")).map(el => el.src);
      openLightbox(srcs, srcs.indexOf(img.src));
      return;
    }
    const btn = e.target.closest("button");
    if (btn) {
      handleRowAction(e, {
        rowOf: (b) => b.closest(".ov-row"),
        closeBothOnRecat: false, // 浮层：修正分类只收起同类菜单（保持原行为）
        copyItemOf: (row) => currentItems.find(x => String(x.id) === row.dataset.id),
        recatOption: (id, cat) => doRecategorize(id, cat),
        verify: (id, btn, row) => {
          const isMemo = row.classList.contains("memo");
          const isIgnored = row.classList.contains("ignored");
          const value = btn.classList.contains("btn-memo") ? (isMemo ? 0 : 1) : (isIgnored ? 0 : -1);
          // 浮层内连续处理：仅本地更新该行，不关浮层、不整页重拉；
          // 主列表在浮层关闭时一次性收敛
          verifyItem(id, value, row, { overlay: true });
        },
      });
      return;
    }
    // 点行主体区展开/收起完整内容（原文引用/图片/上下文）
    // isOpen = 切换前详情是否已展开；!isOpen 即"本次点击是展开动作"
    const row = e.target.closest(".ov-row");
    if (!row) return;
    toggleRowDetail(row, overlayExpandedIds);
  });

  // 浮层滚动位置记忆（按主体）
  $groupOverlayContent.addEventListener("scroll", () => {
    if (overlayKey) overlayScrolls.set(overlayKey, $groupOverlayContent.scrollTop || 0);
  });

  // ── Esc 统一关闭：灯箱 → 快捷键帮助 → 批量确认 → 同主体浮层 → 时间线 → 插件浮层（日历等）→ 设置弹窗 → 状态面板 → 搜索历史 → 清空搜索 ──
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (!$lightbox.classList.contains("hidden")) { closeLightbox(); return; }
    if (!$kbHelpModal.classList.contains("hidden")) { closeKbHelp(); return; }
    if (!$batchConfirmModal.classList.contains("hidden")) { closeBatchConfirm(); return; }
    if (!$groupOverlay.classList.contains("hidden")) { closeGroupOverlay(); return; }
    if (!$timelineModal.classList.contains("hidden")) { closeSubjectTimeline(); return; }
    if (consumePluginEsc()) return; // 插件视图浮层（calendar 详情/当日事件）优先于设置弹窗
    if (!$onboardModal.classList.contains("hidden")) { closeOnboarding({ skip: true }); return; }
    if (!$settingsModal.classList.contains("hidden")) { closeSettingsModal(); return; }
    if (!$statusPopover.classList.contains("hidden")) { closeStatusPanel(); return; }
    if (!$searchHistoryDropdown.classList.contains("hidden")) { renderSearchHistoryDropdown(false); return; }
    if (document.activeElement === $itemSearch && $itemSearch.value) {
      $itemSearch.value = "";
      applySearch("");
    }
  });

  // ── 键盘快捷键（j/k 导航、m/i/u/c/Enter 操作、/ 搜索、? 帮助）──
  document.addEventListener("keydown", handleKbShortcut);
  const $kbHelpClose = document.getElementById("kb-help-close");
  if ($kbHelpClose) $kbHelpClose.addEventListener("click", closeKbHelp);
  const $kbHelpBtn = document.getElementById("kb-help-btn");
  if ($kbHelpBtn) $kbHelpBtn.addEventListener("click", openKbHelp);

  // ── Hash 路由：前进/后退或手动改地址栏时同步视图 ──
  window.addEventListener("hashchange", () => {
    const v = parseHash();
    // 主体时间线
    if (v && v.subject) {
      if (!timeline || timeline.subject !== v.subject) {
        openSubjectTimeline(v.subject, { syncHash: false });
      }
      return;
    }
    if (timeline) closeSubjectTimeline({ syncHash: false });
    // 插件视图（calendar 等）：匹配 hash → 通知进入；其它/空 hash → 通知退出
    if (v && v.view) { notifyPluginViews("hash", v); return; }
    notifyPluginViews("hash", null);
    // 列表视图
    if (v) {
      applyHashView(v);
    } else {
      currentCategory = "全部";
      currentVerified = "unverified";
      currentSearch = "";
      preSearchCategory = "";
      searchFilterCat = "";
      searchFilterRange = "";
      searchFilterGroup = "";
      $itemSearch.value = "";
      $itemSearchClear.classList.add("hidden");
      updateActiveNav();
    }
    fetchData();
  });

  // 连接失败时的"重试"按钮（status-text 由 updateStatus 频繁重写，事件委托在容器上）
  $statusText.addEventListener("click", (e) => {
    if (e.target.closest(".status-retry")) fetchData();
  });

  // ── 数据导出（关于面板）──
  const $exportItems = document.getElementById("export-items-btn");
  const $exportRecat = document.getElementById("export-recat-btn");
  if ($exportItems) $exportItems.addEventListener("click", () => {
    const query = makeItemQuery(); // 复用当前筛选条件
    downloadExport("/api/export/items?" + itemPageParams(query, 0).toString());
  });
  if ($exportRecat) $exportRecat.addEventListener("click", () => {
    downloadExport("/api/export/recat-samples?format=jsonl");
  });

  // ── 数据备份 / 恢复（关于面板）──
  const $backupBtn = document.getElementById("backup-download");
  const $restoreBtn = document.getElementById("restore-upload");
  const $restoreFile = document.getElementById("restore-file");
  if ($backupBtn) $backupBtn.addEventListener("click", () => downloadExport("/api/backup"));
  if ($restoreBtn && $restoreFile) $restoreBtn.addEventListener("click", () => $restoreFile.click());
  if ($restoreFile) $restoreFile.addEventListener("change", async (e) => {
    const f = e.target.files && e.target.files[0];
    e.target.value = ""; // 允许再次选择同一文件
    if (!f) return;
    if (!window.confirm("将用所选备份替换当前数据（上传校验通过后，重启应用生效）。\n确定继续？")) return;
    const fd = new FormData();
    fd.append("file", f);
    try {
      const res = await fetch("/api/restore", { method: "POST", body: fd });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        showToast("恢复失败：" + (data.detail || ("HTTP " + res.status)), { type: "error", duration: 6000 });
        return;
      }
      showToast(data.message || "校验通过，重启后生效", { type: "success", duration: 6000 });
    } catch (err) {
      console.error("Restore error:", err);
      showToast("恢复上传失败，请重试", { type: "error", duration: 5000 });
    }
  });

  // ── 首次使用向导：按钮与步骤流转 ──
  const $onbSkip = document.getElementById("onboard-skip");
  const $onb1Next = document.getElementById("onboard-1-next");
  const $onb2Back = document.getElementById("onboard-2-back");
  const $onb2Next = document.getElementById("onboard-2-next");
  const $onb3Back = document.getElementById("onboard-3-back");
  const $onb3Start = document.getElementById("onboard-3-start");
  if ($onbSkip) $onbSkip.addEventListener("click", () => closeOnboarding({ skip: true }));
  if ($onb1Next) $onb1Next.addEventListener("click", () => {
    renderOnboardSessions();
    showOnboardStep(2);
  });
  if ($onb2Back) $onb2Back.addEventListener("click", () => showOnboardStep(1));
  if ($onb2Next) $onb2Next.addEventListener("click", async () => {
    const btn = $onb2Next;
    btn.disabled = true; btn.textContent = "保存中...";
    const { fail } = await saveOnboardSessions();
    btn.disabled = false; btn.textContent = "保存并继续";
    if (fail) showToast("部分会话保存失败，可稍后在设置中重试", { type: "error", duration: 5000 });
    fillOnboardBackfill(); // step3 回填窗口显示当前 BACKFILL_HOURS（config）
    showOnboardStep(3);
  });
  if ($onb3Back) $onb3Back.addEventListener("click", () => showOnboardStep(2));
  if ($onb3Start) $onb3Start.addEventListener("click", async () => {
    const btn = $onb3Start;
    btn.disabled = true; btn.textContent = "同步中...";
    try { await postJson("/api/sync"); } catch { /* 已触发同步 */ }
    // 轮询等待同步结束（syncing=false）；超时 60s 仍关闭（同步在后台继续）
    const t0 = Date.now();
    while (Date.now() - t0 < 60000) {
      await new Promise(r => setTimeout(r, 1500));
      try {
        const res = await fetch("/api/status");
        const st = await res.json();
        if (!st.syncing) break;
      } catch { break; }
    }
    btn.disabled = false;
    markOnboarded(); // 完整走完向导（无论是否启用会话）都标记完成，避免刷新后向导重现
    closeOnboarding(); // 不跳过：刷新主列表
  });

  // 向导 step2 会话筛选（与设置「群聊筛选」同规则、独立状态）：
  // 名称搜索 / 类型多选 / 消息源多选 / 时间过滤，叠加生效
  if ($onboardSessionSearch) $onboardSessionSearch.addEventListener("input", () => {
    onboardSearch = $onboardSessionSearch.value.toLowerCase().trim();
    applyOnboardFilters();
  });
  if ($onboardTypeFilter) $onboardTypeFilter.addEventListener("click", (e) => {
    const chip = e.target.closest(".filter-chip");
    if (!chip) return;
    const type = chip.dataset.type;
    if (!type || type === "all") onboardTypes.clear();
    else if (onboardTypes.has(type)) onboardTypes.delete(type);
    else onboardTypes.add(type);
    updateOnboardTypeChips();
    applyOnboardFilters();
  });
  if ($onboardSourceFilter) $onboardSourceFilter.addEventListener("click", (e) => {
    const chip = e.target.closest(".filter-chip");
    if (!chip) return;
    const src = chip.dataset.source;
    if (src === "all") onboardSources.clear();
    else if (onboardSources.has(src)) onboardSources.delete(src);
    else onboardSources.add(src);
    updateOnboardSourceChips();
    applyOnboardFilters();
  });
  if ($onboardTimePreset) $onboardTimePreset.addEventListener("change", () => {
    const v = $onboardTimePreset.value;
    if (v === "custom") {
      if ($onboardTimeCustom) $onboardTimeCustom.focus(); // 仅聚焦，保留当前值等用户输入
    } else {
      onboardTime = v === "all" ? "all" : Number(v);
      applyOnboardFilters();
    }
  });
  if ($onboardTimeCustom) $onboardTimeCustom.addEventListener("input", () => {
    onboardTime = $onboardTimeCustom.value ? Number($onboardTimeCustom.value) : "all";
    applyOnboardFilters();
  });

  // 向导 step2 全选框：仅作用于当前可见会话，不调 API；保存时统一应用
  if ($onboardSessionList) $onboardSessionList.addEventListener("change", (e) => {
    const cb = e.target.closest("input[type=checkbox]");
    if (!cb) return;
    if (cb.id === "onboard-select-all") {
      const check = cb.checked;
      $onboardSessionList.querySelectorAll("input[data-session-id]").forEach(one => {
        const row = one.closest(".session-row");
        if (row && row.style.display !== "none") one.checked = check;
      });
    }
    updateOnboardSelectAll();
  });

  // 新消息浮条：点击滚动回顶部并确认新卡片
  $newItemsBarBtn.addEventListener("click", () => {
    confirmNewItems();
    const scroller = document.scrollingElement || document.documentElement;
    scroller.scrollTo({ top: 0, behavior: "smooth" });
  });

  // 滚动到顶部附近 → 自动确认新卡片；滚离顶部且有未确认新卡片 → 重新显示浮条
  // （实际滚动容器是文档根：.main-layout 仅 min-height，内容高时由页面整体滚动）
  window.addEventListener("scroll", () => {
    if (!newItemIds.size) return;
    const top = (document.scrollingElement && document.scrollingElement.scrollTop) || 0;
    if (top < 150) confirmNewItems();
    else updateNewItemsBar();
  });

  // 搜索过滤条：类别 chips + 时间范围
  $filterBar.addEventListener("click", (e) => {
    const chip = e.target.closest(".filter-chip");
    if (!chip) return;
    searchFilterCat = chip.dataset.cat === "全部类别" ? "" : chip.dataset.cat;
    fetchData();
  });
  $filterBar.addEventListener("change", (e) => {
    if (e.target.classList.contains("filter-group")) {
      searchFilterGroup = e.target.value;
      fetchData();
    } else if (e.target.classList.contains("filter-range")) {
      searchFilterRange = e.target.value;
      fetchData();
    }
  });

  // ── 订阅入口：以全部启用关键词发起跨分类搜索 ──
  $subsLink.addEventListener("click", (e) => {
    e.preventDefault();
    const kws = enabledSubKeywords();
    if (!kws) {
      showToast("尚未设置订阅关键词，可在设置中添加", { type: "info", duration: 4000 });
      return;
    }
    $itemSearch.value = kws;
    applySearch(kws);
  });

  // ── 状态指示器：点击展开运行状态详情面板 ──
  $statusIndicator.addEventListener("click", (e) => {
    if (e.target.closest(".status-retry")) return; // 重试按钮不触发面板
    e.stopPropagation();
    if (!$statusPopover.classList.contains("hidden")) { closeStatusPanel(); return; }
    openStatusPanel();
  });
  $statusIndicator.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openStatusPanel(); }
  });
  document.addEventListener("click", (e) => {
    if ($statusPopover.classList.contains("hidden")) return;
    if (!e.target.closest("#status-popover") && !e.target.closest("#status-indicator")) closeStatusPanel();
  });

  // ── 批量操作控制 ──
  $batchToggle.addEventListener("click", () => {
    if (batchMode) exitBatchMode(); else enterBatchMode();
  });
  $batchExit.addEventListener("click", exitBatchMode);
  $batchMemo.addEventListener("click", () => batchApply("memo"));
  $batchIgnore.addEventListener("click", () => batchApply("ignore"));
  $batchUnverify.addEventListener("click", () => batchApply("unverify"));
  $batchDelete.addEventListener("click", openBatchConfirm);
  $batchSelectAll.addEventListener("click", toggleSelectAll);
  $batchConfirmDelete.addEventListener("click", () => batchApply("delete"));
  $batchConfirmCancel.addEventListener("click", closeBatchConfirm);
  $batchConfirmModal.addEventListener("click", (e) => {
    if (e.target === $batchConfirmModal) closeBatchConfirm();
  });

  // 批量模式：勾选框变更（折叠组 → 整组切换；平铺组头 → 整组切换；单卡 → 单卡）
  $itemsContainer.addEventListener("change", (e) => {
    if (!e.target.matches(".batch-check input")) return;
    const coll = e.target.closest(".group-collapsed");
    if (coll && coll.dataset.key) {
      toggleGroupSelect(coll.dataset.key, coll);
      return;
    }
    const ghead = e.target.closest(".group-header");
    if (ghead && ghead.dataset.key) {
      toggleGroupSelect(ghead.dataset.key, ghead);
      return;
    }
    const card = e.target.closest(".item-card");
    if (!card) return;
    const id = card.dataset.id;
    if (e.target.checked) selectedIds.add(id); else selectedIds.delete(id);
    card.classList.toggle("selected", e.target.checked);
    syncBatchGroupStates(); // 成员卡变化会联动组头半选态
    updateBatchBar();
  });

  // ── 原文引用折叠/展开（主列表/浮层/时间线通用）──
  // 折叠态由 .quote-text.is-collapsed 单一状态类控制（CSS 显隐 preview/full），
  // 避免多个 hidden class 切换不一致导致的"点击无效"。
  document.addEventListener("click", (e) => {
    const btn = e.target.closest(".quote-expand-btn");
    if (!btn) return;
    const root = btn.closest(".item-card, .ov-row");
    if (!root) return;
    const qt = root.querySelector(".quote-text");
    const preview = root.querySelector(".quote-preview");
    const full = root.querySelector(".quote-full");
    if (!qt || !preview || !full) return;
    const id = btn.dataset.id;
    const expanding = qt.classList.contains("is-collapsed");
    qt.classList.toggle("is-collapsed", !expanding);
    btn.textContent = expanding ? btn.dataset.labelExpanded : btn.dataset.labelCollapsed;
    if (expanding) quoteExpandedIds.add(id); else quoteExpandedIds.delete(id);
  });

  // ── 更多/修正分类菜单：点击其它区域关闭；插件行内菜单（提醒等）一并委托 ──
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".btn-more") && !e.target.closest(".card-more-menu")) {
      document.querySelectorAll(".card-more-menu").forEach(m => m.classList.add("hidden"));
    }
    if (!e.target.closest(".btn-recat") && !e.target.closest(".card-recat-menu")) {
      document.querySelectorAll(".card-recat-menu").forEach(m => m.classList.add("hidden"));
    }
    closeItemRowMenus(e);
  });

  // ── 外观模式切换（立即生效并持久化）──
  $themeToggle.addEventListener("click", (e) => {
    const chip = e.target.closest(".filter-chip");
    if (!chip || !chip.dataset.mode) return;
    localStorage.setItem("briefdesk.theme", chip.dataset.mode);
    applyTheme();
  });

  // ── 桌面通知模式（立即生效并持久化；授权在切换时请求）──
  $notifyMode.addEventListener("change", () => {
    const mode = $notifyMode.value;
    localStorage.setItem("briefdesk.notifyMode", mode);
    notifyMode = mode;
    if (mode !== "off" && "Notification" in window && Notification.permission === "default") {
      Notification.requestPermission().then((p) => {
        if (p === "denied") {
          $notifyMode.value = "off";
          localStorage.setItem("briefdesk.notifyMode", "off");
          notifyMode = "off";
          showToast("通知权限被拒绝，请在浏览器设置中开启", { type: "error", duration: 6000 });
        }
      });
    }
  });

  // ── 关键词订阅管理（立即生效并持久化）──
  $subsAdd.addEventListener("click", () => {
    $subsAddForm.classList.remove("hidden");
    $subsAdd.classList.add("hidden");
    $subsAddKw.focus();
  });
  $subsAddForm.addEventListener("click", (e) => {
    const btn = e.target.closest("button");
    if (!btn) return;
    if (btn.id === "subs-add-save") {
      const kw = $subsAddKw.value.trim();
      if (!kw) { showToast("请输入关键词", { type: "error", duration: 4000 }); return; }
      subscriptions.push({ id: "s" + Date.now(), keywords: kw, enabled: true });
      saveSubscriptions();
      renderSubsList();
      $subsAddKw.value = "";
      $subsAddForm.classList.add("hidden");
      $subsAdd.classList.remove("hidden");
      showToast("订阅已添加", { type: "success", duration: 2000 });
      fetchData(); // 重新标记订阅命中卡片
    } else if (btn.id === "subs-add-cancel") {
      $subsAddForm.classList.add("hidden");
      $subsAdd.classList.remove("hidden");
    }
  });
  $subsList.addEventListener("change", (e) => {
    if (!e.target.classList.contains("subs-enabled")) return;
    const row = e.target.closest(".subs-row");
    const sub = row && subscriptions.find(s => s.id === row.dataset.id);
    if (sub) {
      sub.enabled = e.target.checked;
      saveSubscriptions();
      fetchData();
    }
  });
  $subsList.addEventListener("click", (e) => {
    const btn = e.target.closest(".subs-del");
    if (!btn) return;
    const row = btn.closest(".subs-row");
    subscriptions = subscriptions.filter(s => s.id !== row.dataset.id);
    saveSubscriptions();
    renderSubsList();
    fetchData();
  });

  // ── 降噪黑名单（与订阅同构的添加/启停/删除）──
  $blockAdd.addEventListener("click", () => {
    $blockAddForm.classList.remove("hidden");
    $blockAdd.classList.add("hidden");
    $blockAddKw.focus();
  });
  $blockAddForm.addEventListener("click", (e) => {
    const btn = e.target.closest("button");
    if (!btn) return;
    if (btn.id === "block-add-save") {
      const kw = $blockAddKw.value.trim();
      if (!kw) { showToast("请输入关键词", { type: "error", duration: 4000 }); return; }
      blocklist.push({ id: "b" + Date.now(), keywords: kw, enabled: true });
      saveBlocklist();
      renderBlocklist();
      $blockAddKw.value = "";
      $blockAddForm.classList.add("hidden");
      $blockAdd.classList.remove("hidden");
      showToast("黑名单已添加，命中卡片将隐藏", { type: "success", duration: 2000 });
      fetchData();
    } else if (btn.id === "block-add-cancel") {
      $blockAddForm.classList.add("hidden");
      $blockAdd.classList.remove("hidden");
    }
  });
  $blockList.addEventListener("change", (e) => {
    if (!e.target.classList.contains("block-enabled")) return;
    const row = e.target.closest(".subs-row");
    const bl = row && blocklist.find(s => s.id === row.dataset.id);
    if (bl) {
      bl.enabled = e.target.checked;
      saveBlocklist();
      fetchData();
    }
  });
  $blockList.addEventListener("click", (e) => {
    const btn = e.target.closest(".block-del");
    if (!btn) return;
    const row = btn.closest(".subs-row");
    blocklist = blocklist.filter(s => s.id !== row.dataset.id);
    saveBlocklist();
    renderBlocklist();
    fetchData();
  });

  // 隐藏已截止开关（持久化 + 重渲染）
  $hideExpiredToggle.addEventListener("click", () => {
    hideExpired = !hideExpired;
    localStorage.setItem("briefdesk.hideExpired", hideExpired ? "1" : "0");
    $hideExpiredToggle.classList.toggle("active", hideExpired);
    fetchData();
  });

  // ── 主体时间线浮层 ──
  document.getElementById("timeline-close").addEventListener("click", () => closeSubjectTimeline());
  $timelineModal.addEventListener("click", (e) => {
    if (e.target === $timelineModal) {
      closeSubjectTimeline();
      return;
    }
    const catChip = e.target.closest(".card-category");
    if (catChip && catChip.dataset.cat) {
      const cat = catChip.dataset.cat;
      closeSubjectTimeline();
      clearSearch();
      currentCategory = cat;
      currentVerified = "unverified";
      updateActiveNav();
      syncHash();
      fetchData();
      return;
    }
    const img = e.target.closest(".card-images img");
    if (img) {
      const row = img.closest(".tl-row");
      const srcs = Array.from(row.querySelectorAll(".card-images img")).map(el => el.src);
      openLightbox(srcs, srcs.indexOf(img.src));
      return;
    }
    const btn = e.target.closest("button");
    if (btn) {
      handleRowAction(e, {
        rowOf: (b) => b.closest(".tl-row"),
        closeBothOnRecat: false, // 时间线：修正分类只收起同类菜单（保持原行为）
        copyItemOf: (row) => (timeline ? timeline.items : []).find(x => String(x.id) === row.dataset.id),
        recatOption: (id, cat) => { doRecategorize(id, cat); if (timeline) loadTimelinePage(); },
        verify: (id, btn, row) => {
          const isMemo = row.classList.contains("memo");
          const isIgnored = row.classList.contains("ignored");
          const value = btn.classList.contains("btn-memo") ? (isMemo ? 0 : 1) : (isIgnored ? 0 : -1);
          timelineVerify(id, value, row);
        },
      });
      return;
    }
    // 点行主体区展开/收起详情
    const row = e.target.closest(".tl-row");
    if (!row) return;
    toggleRowDetail(row, timeline.expandedIds);
  });

  // 组浮层标题 → 主体时间线（关闭组浮层后打开）
  $groupOverlayTitle.addEventListener("click", (e) => {
    const subj = $groupOverlayTitle.dataset.subject;
    if (!subj) return;
    closeGroupOverlay();
    openSubjectTimeline(subj);
  });
}

// ── Sidebar Search ──
const SEARCH_HISTORY_MAX = 10;
const $searchHistoryDropdown = document.getElementById("search-history-dropdown");

function loadSearchHistory() {
  try {
    const saved = JSON.parse(localStorage.getItem("briefdesk.searchHistory") || "[]");
    searchHistory = Array.isArray(saved) ? saved.filter(t => typeof t === "string" && t.trim()).slice(0, SEARCH_HISTORY_MAX) : [];
  } catch { searchHistory = []; }
}

function saveSearchHistory() {
  try {
    localStorage.setItem("briefdesk.searchHistory", JSON.stringify(searchHistory));
  } catch { /* 忽略存储失败 */ }
}

function addSearchHistory(term) {
  const t = (term || "").trim();
  if (!t) return;
  searchHistory = [t].concat(searchHistory.filter(x => x !== t)).slice(0, SEARCH_HISTORY_MAX);
  saveSearchHistory();
}

function renderSearchHistoryDropdown(show) {
  // 仅当输入框为空且聚焦时展示历史（有输入时是普通搜索模式）
  const visible = !!show && !$itemSearch.value && searchHistory.length > 0;
  $searchHistoryDropdown.classList.toggle("hidden", !visible);
  if (!visible) return;
  searchHistIndex = -1;
  $searchHistoryDropdown.innerHTML =
    '<div class="search-history-head">搜索历史<button type="button" id="search-history-clear" class="search-history-clear">清除</button></div>' +
    searchHistory.map((t, i) =>
      `<button type="button" class="search-history-item" data-index="${i}" title="搜索「${escAttr(t)}」">${esc(t)}</button>`
    ).join("");
  const clearBtn = document.getElementById("search-history-clear");
  if (clearBtn) clearBtn.addEventListener("click", () => {
    searchHistory = [];
    saveSearchHistory();
    renderSearchHistoryDropdown(true);
  });
}

function applySearch(term) {
  exitPluginViews(); // 搜索 → 列表视图（退出插件视图）
  const hadTerm = !!currentSearch;
  if (term && !hadTerm) {
    preSearchCategory = currentCategory;
    searchFilterCat = "";      // 新一次搜索：重置本地过滤
    searchFilterRange = "";
    searchFilterGroup = "";
  }
  currentSearch = term;
  if (term) {
    // 搜索跨全部分类
    currentCategory = "全部";
    addSearchHistory(term); // 用户主动搜索才记历史
  } else if (hadTerm) {
    // 清空搜索后恢复之前浏览的分类；哈希恢复路径可能未记录分类（空串）→ 兜底"全部"
    currentCategory = preSearchCategory || "全部";
  }
  $itemSearchClear.classList.toggle("hidden", !term);
  updateActiveNav();
  syncHash();
  fetchData();
}

function clearSearch() {
  if (searchDebounceTimer) {
    clearTimeout(searchDebounceTimer);
    searchDebounceTimer = null;
  }
  $itemSearch.value = "";
  currentSearch = "";
  preSearchCategory = "";
  searchFilterCat = "";
  searchFilterRange = "";
  searchFilterGroup = "";
  $itemSearchClear.classList.add("hidden");
}

function connectRealtimeStream() {
  if (stream) {
    stream.close();
    stream = null;
  }

  stream = new EventSource("/api/stream");

  stream.addEventListener("items_updated", (ev) => {
    // 同步完成事件（payload {"synced":true}）：恢复同步按钮；手动触发时给完成提示
    try {
      const payload = JSON.parse(ev.data || "{}");
      if (payload.synced) {
        const wasManual = manualSyncWait;
        manualSyncWait = false;
        if (syncWaitTimer) { clearTimeout(syncWaitTimer); syncWaitTimer = null; }
        setSyncButton(false);
        if (wasManual) showToast("同步完成", { type: "success", duration: 2500 });
      }
    } catch { /* 非 JSON 负载忽略 */ }
    if (streamRefreshDebounceTimer) return;
    streamRefreshDebounceTimer = setTimeout(() => {
      streamRefreshDebounceTimer = null;
      fetchData();
    }, 200);
  });

  // 同步进度：新增消息数（含处理中的实时消息）；突发收尾短暂展示后淡出
  stream.addEventListener("sync_progress", (ev) => {
    try { renderSyncProgress(JSON.parse(ev.data || "{}")); } catch { /* 忽略 */ }
  });

  stream.addEventListener("error", () => {
    if (stream) {
      stream.close();
      stream = null;
    }
    if (streamReconnectTimer) return;
    streamReconnectTimer = setTimeout(() => {
      streamReconnectTimer = null;
      connectRealtimeStream();
    }, 2000);
  });
}

// ── Fetch ──
// 当前完整查询的服务端计数，不受前端已加载页数影响。
let viewCounts = { total: 0, groups: 0 };

// 消费 /api/items 响应中的侧边栏/颜色/状态数据（fetchData 正常路径与
// 日历模式补拉共用；先设 catColor 再 renderNav——图标派生依赖颜色）
function applySidebarData(data) {
  catColor = new Map((data.allCategories || []).map(c => [c.name, c.color]));
  if (typeof data.totalCount === "number") viewCounts.total = data.totalCount;
  if (typeof data.groupCount === "number") viewCounts.groups = data.groupCount;
  updateStatus(data.status);
  restoreSyncProgress(data.status && data.status.syncProgress);
  renderNav(data.categories, data.ignoredCount, data.memoCount);
}

// 插件视图（如日历）模式下补拉侧边栏/颜色数据（/api/items 的 limit=1 变体，
// 只消费侧边栏字段，不渲染列表）：修复 F5 刷新插件视图 hash 时侧边栏空白、
// 视图 chip 颜色丢失。数据就绪后通知各插件视图（sidebarReady）自行重渲染。
async function fetchSidebarData() {
  try {
    const res = await fetch("/api/items?verified=unverified&limit=1");
    if (!res.ok) return;
    applySidebarData(await res.json());
    // 颜色/计数就绪后通知插件视图重渲染（用缓存数据，不重复请求）：
    // 修复视图先于侧边栏数据返回时 chip 无色的竞态（两种到达顺序均安全）
    notifyPluginViews("sidebarReady");
  } catch { /* 忽略：插件视图主体不受影响 */ }
}

function itemQueryKeyNow() {
  return JSON.stringify([
    currentCategory,
    currentVerified,
    currentSearch,
    searchFilterCat,
    searchFilterGroup,
    searchFilterRange,
    hideExpired,
  ]);
}

function makeItemQuery() {
  const ranges = { "1d": 86400, "3d": 259200, "7d": 604800 };
  const rangeSeconds = currentSearch ? ranges[searchFilterRange] : 0;
  return {
    key: itemQueryKeyNow(),
    category: currentSearch ? searchFilterCat : (currentCategory === "全部" ? "" : currentCategory),
    verified: currentVerified,
    q: currentSearch,
    sourceGroup: currentSearch ? searchFilterGroup : "",
    minMsgTime: rangeSeconds ? Math.floor(Date.now() / 1000 - rangeSeconds) : 0,
    hideExpired: hideExpired,
    filterNow: "",
  };
}

function itemPageParams(query, offset) {
  const params = new URLSearchParams();
  if (query.category) params.set("category", query.category);
  params.set("verified", query.verified);
  if (query.q) params.set("q", query.q);
  if (query.sourceGroup) params.set("sourceGroup", query.sourceGroup);
  if (query.minMsgTime) params.set("minMsgTime", String(query.minMsgTime));
  if (query.hideExpired) params.set("hideExpired", "true");
  if (query.hideExpired && query.filterNow) params.set("filterNow", query.filterNow);
  params.set("limit", String(PAGE_SIZE));
  params.set("offset", String(offset));
  return params;
}

async function requestItemPage(query, offset) {
  const res = await fetch(`/api/items?${itemPageParams(query, offset)}`);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

function appendUniqueItems(target, incoming, seen) {
  for (const item of (incoming || [])) {
    const id = String(item.id);
    if (seen.has(id)) continue;
    seen.add(id);
    target.push(item);
  }
}

async function fetchData() {
  if (consumePluginRefresh()) return; // 插件视图活跃（日历等）：委派视图自刷新
  const seq = ++fetchSeq;
  $emptyState.classList.add("hidden");
  $filteredEmptyState.classList.add("hidden");

  const query = makeItemQuery();
  const queryChanged = query.key !== lastQueryKey;
  const targetPages = queryChanged ? 1 : Math.max(1, loadedPageCount);
  const isFull = queryChanged;
  listRefreshing = true;
  if (queryChanged) {
    hasMore = false;
    activeItemQuery = null;
  }
  updateLoadMore();

  try {
    const data = await requestItemPage(query, 0);
    if (seq !== fetchSeq) return; // Discard stale response

    // 同步完成 → 应用挂起的设置变更（syncing 已复位为 false）
    if (pendingChanges && !data.status.syncing) {
      const ops = pendingChanges;
      pendingChanges = null;
      try {
        await runSettingsOps(ops);
        // 应用后若设置弹窗打开，重载草稿与服务端对齐，
        // 否则弹窗内基于应用前快照的过期草稿二次保存会 409 冲突
        if (!$settingsModal.classList.contains("hidden")) {
          await loadCategories();
          await loadSessions();
        }
        fetchData(); // 应用后刷新界面（计数/卡片/侧边栏）
        return;
      } catch (err) {
        console.error("Apply pending settings error:", err);
        showToast("同步后应用设置失败，请重新在设置中保存", { type: "error", duration: 6000 });
        // 重载草稿对齐服务端真相，避免重复应用已成功的操作
        await loadCategories();
        await loadSessions();
      }
    }

    // 守卫：当前选中类别已从服务器消失（被删/改名/停用，如另一标签页操作）→ 重置为全部。
    // categories 只含"有未审核卡片"的类别（供侧边栏计数），缺项 ≠ 类别不存在；
    // allCategories（启用类别全集，含 0 卡片）才是存在性与颜色的权威。
    if (currentCategory !== "全部" && !(data.allCategories || []).some(c => c.name === currentCategory)) {
      currentCategory = "全部";
      syncHash();
      return fetchData(); // 旧类别已删/改名/停用 → 重新拉取全部；fetchSeq 守卫丢弃旧响应
    }
    if (currentSearch && searchFilterCat
      && !(data.allCategories || []).some(c => c.name === searchFilterCat)) {
      searchFilterCat = "";
      return fetchData();
    }

    const loaded = [];
    const seen = new Set();
    appendUniqueItems(loaded, data.items, seen);
    let pagesFetched = 1;
    let pageData = data;
    let offset = Number.isInteger(data.nextOffset) ? data.nextOffset : loaded.length;
    if (query.hideExpired && typeof data.filterNow === "string" && data.filterNow) {
      query.filterNow = data.filterNow;
    }

    // 自动刷新保留用户已加载的页数：从第一页重新获取相同页数，确保新增、
    // 删除和跨页分组都以当前服务端结果为准。
    while (pagesFetched < targetPages && pageData.hasMore) {
      pageData = await requestItemPage(query, offset);
      if (seq !== fetchSeq) return;
      appendUniqueItems(loaded, pageData.items, seen);
      const fallbackOffset = offset + (pageData.items || []).length;
      offset = Number.isInteger(pageData.nextOffset) ? pageData.nextOffset : fallbackOffset;
      pagesFetched += 1;
    }

    // 全部目标页成功后再原子更新列表状态，避免刷新中途失败留下半套数据。
    applySidebarData(data);
    renderStatusBanner(data.status);
    hasMore = !!pageData.hasMore;
    nextOffset = offset;
    loadedPageCount = pagesFetched;
    activeItemQuery = query;
    lastQueryKey = query.key;
    searchCats = currentSearch ? (data.allCategories || []) : [];
    searchGroups = currentSearch ? (data.sourceGroups || []) : [];
    viewSourceItems = loaded;

    renderItems(loaded, { full: isFull });
    renderFilterBar();
  } catch (err) {
    console.error("Fetch error:", err);
    $statusIndicator.className = "status offline";
    $statusText.innerHTML = '连接失败 <button type="button" class="status-retry">重试</button>';
    renderStatusBanner({ lastError: "无法连接服务器，请确认应用仍在运行（python main.py）" });
  } finally {
    if (seq === fetchSeq) {
      listRefreshing = false;
      updateLoadMore();
    }
  }
}

// ── Render Nav ──
const _CAT_ICONS = {
  "全部": "/图标/8-界面/网格.svg",
  "活动通知": "/图标/9-媒体/日历.svg",
  "社团招新": "/图标/8-界面/用户组.svg",
  "学术": "/图标/2-物品/书.svg",
  "交易": "/图标/8-界面/更改.svg",
  "实习": "/图标/9-媒体/时间.svg",
};

function renderNav(categories, ignoredCount, memoCount) {
  let html = "";
  for (const cat of categories) {
    const isActive = !currentSearch && cat.key === currentCategory && currentVerified === "unverified";
    // 图标：硬编码默认五类 → 按类别颜色从预设色板映射（自定义/改名类别）→ 空
    const icon = _CAT_ICONS[cat.key] || _paletteIcon(catColor.get(cat.key)) || "";
    // 颜色数据驱动：内联 --cat（遗留类别无 color → 空，回退 CSS 默认）
    const colorStyle = catColor.get(cat.key) ? ` style="--cat:${catColor.get(cat.key)}"` : "";
    html += `
      <a href="#" class="cat-link${isActive ? " active" : ""}"${colorStyle} data-category="${cat.key}" data-verified="unverified">
        ${icon ? `<img src="${icon}" class="icon-sm cat-icon" alt="">` : ""}${esc(cat.key)}
        <span class="cat-count">${cat.count}</span>
      </a>`;
  }
  $nav.innerHTML = html;

  if (memoCount !== undefined) {
    $memoLink.innerHTML = `<span class="cat-link-main"><img src="/图标/8-界面/保存.svg" class="icon-sm cat-icon" alt="">备忘录<span class="cat-count">${memoCount}</span></span>`;
  }
  if (ignoredCount !== undefined) {
    $ignoredLink.innerHTML = `<span class="cat-link-main"><img src="/图标/8-界面/禁止.svg" class="icon-sm cat-icon" alt="">已忽略<span class="cat-count">${ignoredCount}</span></span>`;
  }
}

function updateActiveNav() {
  document.querySelectorAll(".cat-link").forEach(el => {
    const match = !currentSearch && el.dataset.category === currentCategory && el.dataset.verified === currentVerified;
    el.classList.toggle("active", match);
  });
}

// ── Render Items ──
function renderItems(items, { full = false } = {}) {
  viewSourceItems = items;
  $loadingState.classList.add("hidden");
  $emptyState.classList.add("hidden");
  $filteredEmptyState.classList.add("hidden");

  // 黑名单（显示层过滤）：命中卡片从可见集剔除，工具栏提示隐藏数；
  // 不改变服务端分页/计数口径（「共 N 条」仍是服务端总数）
  const blockedVisible = items.filter(isBlocked);
  if (blockedVisible.length) {
    items = items.filter(it => !isBlocked(it));
  }
  updateBlockedHint(blockedVisible.length);

  // /api/items 已按全部有效条件分页；此处不再做会改变 offset/总数口径的二次过滤。
  const out = full ? fullRenderItems(items) : incrementalRenderItems(items);
  updateListCount();
  updateLoadMore();
  updateSubsBadge();
  syncBatchGroupStates();
  rebuildKbUnits();
  return out;
}

// 黑名单隐藏计数提示（工具栏右侧）
function updateBlockedHint(n) {
  const $hint = document.getElementById("blocked-hint");
  if (!$hint) return;
  $hint.classList.toggle("hidden", !n);
  $hint.textContent = `已隐藏 ${n} 条（黑名单）`;
}

// 头部计数始终是当前完整查询的服务端总数，与已加载页数无关。
function updateListCount() {
  if (currentSearch) {
    const n = collapseGroups ? viewCounts.groups + " 组" : viewCounts.total + " 条";
    $listCount.textContent = `关键词「${currentSearch}」命中 ${n}`;
    return;
  }
  if (collapseGroups) {
    $listCount.textContent = "共 " + viewCounts.groups + " 组";
    return;
  }
  $listCount.textContent = "共 " + viewCounts.total + " 条";
}

// ── 键盘快捷键：焦点卡片导航与操作 ──
// 在 renderItems 后重建 kbUnits（主列表 .item-card；折叠模式代表卡即组内最新卡）。
function rebuildKbUnits() {
  kbUnits = Array.from($itemsContainer.querySelectorAll(".item-card"));
  if (kbFocusIndex >= kbUnits.length) {
    kbFocusIndex = -1; // 列表刷新后不自动聚焦
    if (kbFadeTimer) { clearTimeout(kbFadeTimer); kbFadeTimer = null; }
  }
  if (kbFocusIndex >= 0) {
    const el = kbUnits[kbFocusIndex];
    if (el) el.classList.add("kb-focus");
  }
}

function _kbFocusEl() {
  if (kbFocusIndex < 0 || kbFocusIndex >= kbUnits.length) return null;
  const el = kbUnits[kbFocusIndex];
  if (!el || !el.isConnected) return null;
  return el;
}

// 重置/启动焦点淡出计时：每次键盘交互后调用，无操作 KB_FOCUS_FADE_MS 后
// 焦点高亮平滑淡出（.kb-focus-fading 过渡），随后清除焦点索引。
function scheduleKbFade() {
  if (kbFadeTimer) clearTimeout(kbFadeTimer);
  kbFadeTimer = setTimeout(fadeOutKbFocus, KB_FOCUS_FADE_MS);
}

function fadeOutKbFocus() {
  kbFadeTimer = null;
  const el = _kbFocusEl();
  if (!el) return;
  el.classList.add("kb-focus-fading"); // 触发 box-shadow 过渡（淡出）
  setTimeout(() => {
    el.classList.remove("kb-focus", "kb-focus-fading");
  }, KB_FOCUS_FADE_TRANSITION_MS);
  kbFocusIndex = -1;
}

function moveKbFocus(delta) {
  if (!kbUnits.length) return;
  const prev = _kbFocusEl();
  if (prev) prev.classList.remove("kb-focus", "kb-focus-fading");
  const dir = delta > 0 ? 1 : -1;
  kbFocusIndex = Math.min(kbUnits.length - 1, Math.max(0, (kbFocusIndex < 0 ? (dir > 0 ? -1 : kbUnits.length) : kbFocusIndex) + dir));
  const el = _kbFocusEl();
  if (el) {
    el.classList.add("kb-focus");
    el.scrollIntoView({ block: "nearest" });
  }
  scheduleKbFade();
}

function _kbFocusItem() {
  const el = _kbFocusEl();
  if (!el) return null;
  const id = el.dataset.id;
  return viewSourceItems.find(it => String(it.id) === id) || null;
}

// 快捷键帮助浮层（? 开关，Esc 关闭）
function openKbHelp() {
  const modal = document.getElementById("kb-help-modal");
  if (!modal) return;
  lastModalFocus = document.activeElement;
  modal.classList.remove("hidden");
  syncBodyScrollLock();
}
function closeKbHelp() {
  const modal = document.getElementById("kb-help-modal");
  if (!modal) return;
  modal.classList.add("hidden");
  syncBodyScrollLock();
  if (lastModalFocus && document.contains(lastModalFocus)) lastModalFocus.focus();
  lastModalFocus = null;
}

// ── 首次使用向导：环境检查 → 启用群聊 → 触发同步（三步，可跳过）──
// step3 的 BACKFILL_HOURS 当前值复用 GET /api/sessions 的 backfillHours 字段
// （renderOnboardSessions 拉取时缓存；缺失时由 fillOnboardBackfill 补拉）
let onboardBackfillHours = null;
function markOnboarded() {
  try { localStorage.setItem("briefdesk.onboarded", "1"); } catch { }
}

async function initOnboarding() {
  if (localStorage.getItem("briefdesk.onboarded")) return;
  // 已有启用会话 → 视为已使用，不再打扰
  try {
    const res = await fetch("/api/sessions");
    if (!res.ok) return;
    const data = await res.json();
    if (Array.isArray(data.sessions) && data.sessions.some(s => s.enabled)) {
      markOnboarded();
      return;
    }
  } catch { return; } // 后端不可用不弹
  openOnboarding();
}

function openOnboarding() {
  if (!$onboardModal) return;
  lastModalFocus = document.activeElement;
  $onboardModal.classList.remove("hidden");
  syncBodyScrollLock();
  showOnboardStep(1);
  renderOnboardEnv();
}

function showOnboardStep(n) {
  for (let i = 1; i <= 3; i++) {
    const el = document.getElementById("onboard-step-" + i);
    if (el) el.classList.toggle("hidden", i !== n);
  }
}

function closeOnboarding({ skip = false } = {}) {
  if (!$onboardModal) return;
  $onboardModal.classList.add("hidden");
  syncBodyScrollLock();
  if (skip) markOnboarded();
  if (lastModalFocus && document.contains(lastModalFocus)) lastModalFocus.focus();
  lastModalFocus = null;
  if (!skip) fetchData(); // 完整走完向导后刷新主列表
}

async function renderOnboardEnv() {
  const el = document.getElementById("onboard-env");
  if (!el) return;
  el.innerHTML = '<p class="text-muted">检查中...</p>';
  let status = null;
  try {
    const res = await fetch("/api/status");
    if (res.ok) status = await res.json();
  } catch { /* 保持 null */ }
  if (!status) {
    el.innerHTML = '<div class="onboard-warn">无法连接后端，请确认应用正在运行（python main.py）。</div>';
    return;
  }
  const srcs = Object.entries(status.sources || {});
  const srcHtml = srcs.length
    ? srcs.map(([name, s]) => `<span class="onboard-chip">${esc(name)} · ${_STATUS_LABELS[s.status] || s.status || "offline"}</span>`).join("")
    : '<span class="onboard-chip onboard-warn">未启用任何消息源</span>';
  const warn = status.lastError || status.lastWarning;
  const warnHtml = warn
    ? `<div class="onboard-warn">⚠ ${esc(warn)}<br>常见原因：.env 中的必填配置不完整（AI 或消息源的 Token / Key），或消息源未启动；修改配置后需重启应用。</div>`
    : "";
  el.innerHTML = '<div class="onboard-env-row"><span class="onboard-label">消息源</span>' + srcHtml + '</div>' + warnHtml;
}

// step3 回填窗口说明：显示 BACKFILL_HOURS 当前值（复用 /api/sessions 的
// backfillHours 字段——与设置「群聊筛选」默认时间过滤同一通道）
function fillOnboardBackfill() {
  const el = document.getElementById("onboard-backfill-val");
  if (!el) return;
  const render = (hours) => {
    el.textContent = hours === -1 ? "全部历史" : hours + " 小时";
  };
  if (onboardBackfillHours !== null) {
    render(onboardBackfillHours);
    return;
  }
  // 会话接口尚未拉取成功：异步补拉一次再填（失败保持省略号占位）
  fetch("/api/sessions").then(r => (r.ok ? r.json() : null)).then(d => {
    if (d && typeof d.backfillHours === "number" && Number.isFinite(d.backfillHours)) {
      onboardBackfillHours = d.backfillHours;
      render(d.backfillHours);
    }
  }).catch(() => {});
}

async function renderOnboardSessions() {
  const el = document.getElementById("onboard-sessions");
  if (!el) return;
  el.innerHTML = '<p class="text-muted">加载中...</p>';
  let data = null;
  try {
    const res = await fetch("/api/sessions");
    if (res.ok) data = await res.json();
  } catch { /* 保持 null */ }
  // 会话接口携带配置值：缓存 backfillHours 供 step3 展示（复用同一通道）
  onboardBackfillHours =
    data && typeof data.backfillHours === "number" && Number.isFinite(data.backfillHours)
      ? data.backfillHours
      : null;
  const sessions = Array.isArray(data && data.sessions) ? data.sessions : [];
  // 进入 step2 重置筛选状态（与设置每次打开重置类型筛选一致）
  onboardTypes.clear();
  onboardSources.clear();
  onboardSearch = "";
  onboardTime = "all";
  if ($onboardSessionSearch) $onboardSessionSearch.value = "";
  if ($onboardTimePreset) $onboardTimePreset.value = "all";
  if ($onboardTimeCustom) $onboardTimeCustom.value = "";
  updateOnboardTypeChips();
  if (!sessions.length) {
    el.innerHTML = '<p class="text-muted">暂未发现会话，可稍后在 设置 → 群聊筛选 中「发现新群聊」。</p>';
    return;
  }
  // 行结构与设置「群聊筛选」一致（data-is-group/data-is-official/data-source/data-last-active），
  // 共用 sessionRowMatches 过滤规则；群聊默认勾选，私聊/公众号不勾（避免无意监控私聊）
  const allChecked = sessions.length > 0 && sessions.every(s => s.is_group);
  const html = [
    `<label class="session-row session-row-all">
      <input type="checkbox" id="onboard-select-all" ${allChecked ? "checked" : ""}>
      <span>全选</span>
    </label>`,
    ...sessions.map(s => {
      const kindTag = s.is_official ? '公' : (s.is_group ? '群' : '私');
      const checked = s.is_group ? "checked" : "";
      return `
      <label class="session-row" data-is-group="${s.is_group ? "1" : "0"}" data-is-official="${s.is_official ? "1" : "0"}" data-source="${escAttr(s.source)}" data-last-active="${s.last_active || ""}">
        <input type="checkbox" data-source="${escAttr(s.source)}" data-session-id="${escAttr(s.session_id)}" ${checked}>
        <span class="session-name">${esc(s.name || s.session_id)}</span>
        <span class="text-muted" style="font-size:11px">${esc(kindTag)} · ${esc(s.source)} · ${esc(s.session_id.substring(0, 15))}...</span>
      </label>`;
    })
  ].join("");
  el.innerHTML = html + '<p class="text-muted onboard-filter-empty hidden" style="padding:10px 12px">无匹配的会话</p>';
  await loadOnboardSourceChips();
  applyOnboardFilters();
}

// 向导 step2 会话过滤：与设置「群聊筛选」同一规则（sessionRowMatches）、独立状态
function applyOnboardFilters() {
  const list = document.getElementById("onboard-sessions");
  if (!list) return;
  const cutoff = onboardTime === "all" ? 0 : (Date.now() / 1000) - onboardTime * 3600;
  let visible = 0;
  list.querySelectorAll(".session-row:not(.session-row-all)").forEach(row => {
    const show = sessionRowMatches(row, {
      types: onboardTypes,
      sources: onboardSources,
      query: onboardSearch,
      cutoff,
    });
    row.style.display = show ? "" : "none";
    if (show) visible++;
  });
  const empty = list.querySelector(".onboard-filter-empty");
  if (empty) empty.classList.toggle("hidden", visible > 0);
  updateOnboardSelectAll();
}

// 向导全选框三态：全选 / 部分勾选（半选）/ 未选（仅统计当前可见行）
function updateOnboardSelectAll() {
  const all = document.getElementById("onboard-select-all");
  const list = document.getElementById("onboard-sessions");
  if (!all || !list) return;
  const boxes = Array.from(list.querySelectorAll("input[data-session-id]"))
    .filter(b => {
      const row = b.closest(".session-row");
      return row && row.style.display !== "none";
    });
  const checked = boxes.filter(b => b.checked).length;
  all.checked = boxes.length > 0 && checked === boxes.length;
  all.indeterminate = checked > 0 && checked < boxes.length;
}

function updateOnboardTypeChips() {
  if (!$onboardTypeFilter) return;
  $onboardTypeFilter.querySelectorAll(".filter-chip").forEach(c => {
    const t = c.dataset.type;
    const active = (!t || t === "all") ? onboardTypes.size === 0 : onboardTypes.has(t);
    c.classList.toggle("active", active);
  });
}

// 向导内消息源筛选芯片：多源（>=2）时显示；与设置一致复用 enabledSources 列表
async function loadOnboardSourceChips() {
  if (!$onboardSourceGroup || !$onboardSourceFilter) return;
  if (!enabledSources.length) await loadEnabledSources();
  const show = enabledSources.length >= 2;
  $onboardSourceGroup.classList.toggle("hidden", !show);
  $onboardSourceFilter.innerHTML =
    `<button type="button" class="filter-chip" data-source="all">全部</button>` +
    enabledSources.map(s =>
      `<button type="button" class="filter-chip" data-source="${escAttr(s)}">${esc(s)}</button>`
    ).join("");
  updateOnboardSourceChips();
}

function updateOnboardSourceChips() {
  if (!$onboardSourceFilter) return;
  $onboardSourceFilter.querySelectorAll(".filter-chip").forEach(c => {
    const active = c.dataset.source === "all"
      ? onboardSources.size === 0
      : onboardSources.has(c.dataset.source);
    c.classList.toggle("active", active);
  });
}

async function saveOnboardSessions() {
  const checked = Array.from($onboardModal.querySelectorAll("#onboard-sessions input[data-session-id]:checked"));
  let ok = 0, fail = 0;
  for (const cb of checked) {
    try {
      const res = await postJson(`/api/sessions/${encodeURIComponent(cb.dataset.source)}/${encodeURIComponent(cb.dataset.sessionId)}/toggle`);
      if (res && res.success) ok++; else fail++;
    } catch { fail++; }
  }
  return { ok, fail };
}

function handleKbShortcut(e) {
  // 输入态与模态打开时不处理列表快捷键
  const t = e.target;
  if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable)) return;
  if (!$lightbox.classList.contains("hidden") || !$batchConfirmModal.classList.contains("hidden")
    || !$groupOverlay.classList.contains("hidden") || !$timelineModal.classList.contains("hidden")
    || !$settingsModal.classList.contains("hidden") || !$statusPopover.classList.contains("hidden")) return;

  const key = e.key;
  if (key === "/") { // 聚焦搜索
    e.preventDefault();
    $itemSearch.focus();
    return;
  }
  if (key === "?") { // 帮助浮层
    e.preventDefault();
    openKbHelp();
    return;
  }
  if (key === "j" || key === "k") {
    e.preventDefault();
    moveKbFocus(key === "j" ? 1 : -1);
    return;
  }
  const item = _kbFocusItem();
  const el = _kbFocusEl();
  if (!item || !el) return;
  if (key === "m") {
    e.preventDefault();
    verifyItem(String(item.id), item.is_verified === 1 ? 0 : 1, el, { refresh: true });
  } else if (key === "i") {
    e.preventDefault();
    verifyItem(String(item.id), item.is_verified === -1 ? 0 : -1, el, { refresh: true });
  } else if (key === "u") {
    e.preventDefault();
    if (lastUndo) undoVerify(lastUndo);
  } else if (key === "c") {
    e.preventDefault();
    copyItem(item);
  } else if (key === "Enter") {
    e.preventDefault();
    const toggle = el.querySelector(".card-quote-toggle");
    if (toggle) toggle.click();
  } else {
    return; // 非快捷键键位不重置淡出计时
  }
  scheduleKbFade(); // 操作后继续无操作计时（下次淡出）
}

function showEmptyState() {
  $itemsContainer.innerHTML = "";
  // 两个空态互斥：先统一隐藏，再只显示当前分支需要的那个，
  // 避免直接调用 renderItems（搜索过滤/批量模式切换）时残留旧空态
  $emptyState.classList.add("hidden");
  $filteredEmptyState.classList.add("hidden");
  if (currentSearch) {
    $filteredEmptyState.innerHTML = '<p>未找到相关卡片</p><a href="#" class="reset-filter-link">查看全部</a>';
    $filteredEmptyState.classList.remove("hidden");
  } else if (currentCategory === "全部" && currentVerified === "unverified") {
    $emptyState.classList.remove("hidden");
    renderEmptyStateGuide(); // 异步填充新手引导（会话未启用等）
  } else {
    $filteredEmptyState.classList.remove("hidden");
  }
}

// 首屏空态引导：按会话发现/启用情况给出可操作的下一步（新用户友好）。
// 失败或已被后续渲染隐藏时静默保留默认文案。
async function renderEmptyStateGuide() {
  $emptyState.innerHTML = '<p>暂无信息，等待新消息中...</p>';
  let data = null;
  try {
    const res = await fetch("/api/sessions");
    if (res.ok) data = await res.json();
  } catch { /* 忽略 */ }
  if ($emptyState.classList.contains("hidden")) return; // 已被后续渲染隐藏（新数据到达等）
  const sessions = Array.isArray(data && data.sessions) ? data.sessions : [];
  const enabled = sessions.filter(s => s.enabled).length;
  if (!sessions.length) {
    $emptyState.innerHTML =
      '<p>暂未发现任何会话（群聊/私聊）。</p>' +
      '<button type="button" class="empty-guide-btn" id="empty-refresh-sessions">去「群聊筛选」发现会话</button>';
  } else if (!enabled) {
    $emptyState.innerHTML =
      '<p>已发现 ' + sessions.length + ' 个会话，但尚未启用任何会话，消息不会被拉取。</p>' +
      '<button type="button" class="empty-guide-btn" id="empty-open-settings">去设置启用群聊</button>';
  } else {
    $emptyState.innerHTML =
      '<p>已启用 ' + enabled + ' 个会话，等待新消息中...</p>' +
      '<button type="button" class="empty-guide-btn" id="empty-sync-now">立即同步</button>';
  }
  const openBtn = document.getElementById("empty-open-settings");
  if (openBtn) openBtn.addEventListener("click", () => {
    const link = document.getElementById("settings-link-top");
    if (link) link.click();
    setSettingsPanel("sessions"); // openSettings 默认进「常规」，这里直达「群聊筛选」
  });
  const refreshBtn = document.getElementById("empty-refresh-sessions");
  if (refreshBtn) refreshBtn.addEventListener("click", () => {
    const link = document.getElementById("settings-link-top");
    if (link) link.click();
    const btn = document.getElementById("session-refresh");
    if (btn) setTimeout(() => btn.click(), 300); // 等设置弹窗打开后再触发发现
  });
  const syncBtn = document.getElementById("empty-sync-now");
  if (syncBtn) syncBtn.addEventListener("click", () => {
    const btn = document.getElementById("sync-btn");
    if (btn) btn.click();
  });
}

// 卡片/分组的时间戳（秒）：msg_time 优先，回退 created_at
function itemTime(it) {
  if (it.msg_time) return it.msg_time;
  if (it.created_at) return Math.floor(new Date(it.created_at).getTime() / 1000);
  return 0;
}

function groupKeyOf(item) {
  const subject = (item.subject || "").trim();
  if (!subject) return "";
  return subject + "\x01" + (item.category || "");
}

function fullRenderItems(visible) {
  // 仅当视图真正切换（分类/验证态/搜索词变化）时才保存旧现场、恢复新现场；
  // 同视图的全量渲染（撤销恢复、折叠切换、本地过滤）不保存现场——
  // 否则瞬态 DOM（如撤销前已淡出的卡片）会覆盖掉已保存的展开状态
  const newKey = viewKeyNow();
  const viewChanged = newKey !== currentViewKey;
  if (viewChanged) {
    saveViewState();
    currentViewKey = newKey;
    // 展开状态全局共享（跟随卡片），仅恢复本视图的滚动位置
    const st = viewStates.get(currentViewKey);
    pendingScrollTop = st ? st.scrollTop : 0;
  }

  currentItems = visible;
  lastVisibleItems = visible;
  confirmNewItems(); // 视图切换：清除新卡片标记与浮条

  if (visible.length === 0) {
    showEmptyState();
    groupMap.clear(); // 空视图：清陈旧分组，避免浮层打开时渲染已不属于当前视图的成员
    updateCollapseToggle(); // 空视图也要同步折叠/平铺按钮态（否则无数据时按钮点击无响应）
    syncOverlayWithData();
    return [];
  }

  $itemsContainer.innerHTML = buildListHtml(visible);

  // 折叠/展开切换后：卡片错落浮现（--rise-i 驱动错峰动画，封顶 12 步防长列表拖沓）
  if (animateOnNextRender) {
    animateOnNextRender = false;
    const els = $itemsContainer.children;
    for (let i = 0; i < els.length; i++) {
      els[i].style.setProperty("--rise-i", Math.min(i, 12));
    }
    $itemsContainer.classList.add("rising");
  } else {
    $itemsContainer.classList.remove("rising");
  }

  applyExpandedState();
  if (viewChanged) restoreScroll(); // 同视图渲染不重置滚动位置
  updateCollapseToggle();
  syncOverlayWithData();
  return visible;
}

function viewKeyNow() {
  return JSON.stringify([currentCategory, currentVerified, currentSearch]);
}

function saveViewState() {
  if (!currentViewKey) return; // 首次渲染无旧视图
  // 只保存滚动位置：展开状态是卡片的属性（currentExpandedIds 全局共享，跨视图保留）
  const top = (document.scrollingElement && document.scrollingElement.scrollTop) || 0;
  viewStates.set(currentViewKey, { scrollTop: top });
}

function restoreScroll() {
  const scroller = document.scrollingElement || document.documentElement;
  requestAnimationFrame(() => {
    scroller.scrollTop = pendingScrollTop || 0;
  });
}

// 恢复本视图此前展开的卡片（引用 + 已缓存的上下文）
function applyExpandedState() {
  for (const id of currentExpandedIds) {
    const card = $itemsContainer.querySelector('.item-card[data-id="' + CSS.escape(id) + '"]');
    if (!card) continue;
    const quote = card.querySelector(".card-quote");
    const toggle = card.querySelector(".card-quote-toggle");
    if (quote && !quote.classList.contains("open")) {
      quote.classList.add("open");
      if (toggle) toggle.innerHTML = '<img src="/图标/8-界面/箭头上.svg" class="icon-sm" alt="">原文引用';
      const ctxDiv = card.querySelector(".card-quote-context");
      if (ctxDiv && ctxDiv.classList.contains("hidden")) {
        ctxDiv.classList.remove("hidden");
        const source = card.dataset.source;
        const sessionId = card.dataset.sessionId;
        const msgTime = card.dataset.msgtime;
        const msgId = card.dataset.msgid;
        if (source && sessionId) {
          fetchContext(ctxDiv, source, sessionId, parseInt(msgTime) || 0, msgId);
        } else {
          ctxDiv.innerHTML = '<p class="text-muted">缺少会话ID，无法加载上下文（旧数据不支持）</p>';
        }
      }
    }
  }
}

// 浮层打开期间数据变化 → 按最新 groupMap 重渲染浮层行（保留展开态与滚动位置）
function syncOverlayWithData() {
  if ($groupOverlay.classList.contains("hidden") || !overlayKey) return;
  const before = $groupOverlayContent ? $groupOverlayContent.scrollTop : 0;
  renderOverlayList(overlayKey);
  if ($groupOverlayContent) $groupOverlayContent.scrollTop = before;
}

// 列表 HTML：单卡与分组统一按时间倒序排列（时间线单调，日期分隔条才有意义），
// 插入日期分隔条
function buildListHtml(visible) {
  const { groups, singles } = groupVisibleItems(visible);
  groupMap.clear();
  const blocks = [];
  for (const item of singles) {
    blocks.push({ t: itemTime(item), html: renderCard(item) });
  }
  for (const group of groups) {
    groupMap.set(group.key, group.members);
    const rep = group.members[0];
    // 只剩一张 → 直接平铺单卡，不显示折叠/分组头 UI
    if (group.members.length < 2) {
      blocks.push({ t: itemTime(rep), html: renderCard(rep, group.key) });
    } else if (collapseGroups) {
      blocks.push({ t: itemTime(rep), html: renderCollapsedGroup(group) });
    } else {
      blocks.push({ t: itemTime(rep), html: renderGroupHeader(group) + group.members.map(m => renderCard(m, group.key)).join("") });
    }
  }
  blocks.sort((a, b) => b.t - a.t);
  let html = "";
  let lastLabel = "";
  for (const b of blocks) {
    const label = dayLabel(b.t);
    if (label && label !== lastLabel) {
      html += renderDayDivider(label);
      lastLabel = label;
    }
    html += b.html;
  }
  return html;
}

// ── 增量渲染：diff 后仅移除/插入/重建受影响节点，不打断用户阅读 ──
function incrementalRenderItems(visible) {
  const oldMap = new Map(currentItems.map(it => [String(it.id), it]));
  const newMap = new Map(visible.map(it => [String(it.id), it]));
  const added = visible.filter(it => !oldMap.has(String(it.id)));
  const removed = currentItems.filter(it => !newMap.has(String(it.id)));
  currentItems = visible;
  lastVisibleItems = visible;

  if (added.length === 0 && removed.length === 0) {
    updateCollapseToggle();
    return visible;
  }

  // 首屏尚未渲染（异常兜底）→ 直接全量
  if ($itemsContainer.childElementCount === 0) {
    return fullRenderItems(visible);
  }

  // 1) 移除：组内成员 → 标记整组重建；独立卡片 → 淡出
  const rebuiltKeys = new Set();
  for (const it of removed) {
    const key = groupKeyOf(it);
    if (key && (findGroupHead(key) || (groupMap.get(key) || []).length > 0)) {
      rebuiltKeys.add(key);
    } else {
      fadeOutCard(String(it.id));
    }
  }

  // 2) 新增：单卡插入 / 并入已有组 / 全新组
  const addedIds = [];
  const addedGroupKeys = new Set();
  const { groups, singles } = groupVisibleItems(added);
  for (const item of singles) {
    insertBlockAtTime(renderCard(item), itemTime(item));
    addedIds.push(String(item.id));
  }
  for (const g of groups) {
    const existing = !!findGroupHead(g.key) || (groupMap.get(g.key) || []).length > 0;
    if (existing) {
      addedGroupKeys.add(g.key);
      addedIds.push(...g.members.map(m => String(m.id)));
    } else if (g.members.length < 2) {
      insertBlockAtTime(renderCard(g.members[0], g.key), itemTime(g.members[0]));
      addedIds.push(String(g.members[0].id));
    } else {
      const html = collapseGroups
        ? renderCollapsedGroup(g)
        : renderGroupHeader(g) + g.members.map(m => renderCard(m, g.key)).join("");
      insertBlockAtTime(html, itemTime(g.members[0]));
      addedIds.push(...g.members.map(m => String(m.id)));
    }
  }

  // 3) 以最新数据重建 groupMap，再重建受影响组（新成员并入 / 成员被移除）
  const { groups: allGroups } = groupVisibleItems(visible);
  groupMap.clear();
  for (const g of allGroups) groupMap.set(g.key, g.members);
  for (const key of new Set([...rebuiltKeys, ...addedGroupKeys])) {
    rebuildGroup(key);
  }

  // 4) 新卡片高亮 + 浮条 + 日期分隔条
  if (addedIds.length) {
    newItemIds = new Set([...newItemIds, ...addedIds]);
    markNewCards(addedIds);
    // 标签页未读计数 + 桌面通知（仅页面在后台时，避免前台打扰）
    if (document.hidden) {
      document.title = "(" + newItemIds.size + ") BRIEFDESK";
      const addedSet = new Set(addedIds.map(String));
      notifyNewItems(visible.filter(it => addedSet.has(String(it.id))));
    }
  }
  rebuildDayDividers();
  updateNewItemsBar();
  updateCollapseToggle();
  syncOverlayWithData();

  // 全部移除后兜底空态
  if ($itemsContainer.childElementCount === 0) showEmptyState();
  return visible;
}

function fadeOutCard(id) {
  const el = $itemsContainer.querySelector(".item-card[data-id=\"" + CSS.escape(id) + "\"]");
  if (!el) return;
  el.style.transition = "opacity 0.3s";
  el.style.opacity = "0";
  setTimeout(() => {
    el.remove();
    cleanupOrphanDividers();
  }, 300);
}

// 按 msg_time DESC 找到插入位置并插入（跳过日期分隔条）
function insertBlockAtTime(html, timeSec) {
  const tpl = document.createElement("template");
  tpl.innerHTML = html.trim();
  const frag = tpl.content;
  if (!frag.firstElementChild) return null;
  const children = Array.from($itemsContainer.children);
  for (const child of children) {
    if (child.classList.contains("day-divider")) continue;
    if (timeSec > parseFloat(child.dataset.msgtime || "0")) {
      $itemsContainer.insertBefore(frag, child);
      return frag.firstElementChild;
    }
  }
  $itemsContainer.appendChild(frag);
  return frag.firstElementChild;
}

function findGroupHead(key) {
  for (const el of $itemsContainer.children) {
    if ((el.classList.contains("group-collapsed") || el.classList.contains("group-header")) && el.dataset.key === key) {
      return el;
    }
  }
  return null;
}

function removeGroupNodes(key) {
  const head = findGroupHead(key);
  if (!head) {
    // 曾以单卡渲染的同主体（groupKey 挂在卡片上）
    $itemsContainer.querySelectorAll(".item-card[data-key]").forEach(el => {
      if (el.dataset.key === key) el.remove();
    });
    return;
  }
  if (head.classList.contains("group-collapsed")) { head.remove(); return; }
  const toRemove = [head];
  let sib = head.nextElementSibling;
  while (sib && !sib.classList.contains("day-divider") && !sib.classList.contains("group-header") && sib.dataset.key === key) {
    toRemove.push(sib);
    sib = sib.nextElementSibling;
  }
  toRemove.forEach(el => el.remove());
}

function rebuildGroup(key) {
  removeGroupNodes(key);
  const g = groupVisibleItems(currentItems).groups.find(x => x.key === key);
  if (!g || g.members.length === 0) return;
  if (g.members.length < 2) {
    insertBlockAtTime(renderCard(g.members[0], key), itemTime(g.members[0]));
    return;
  }
  const html = collapseGroups
    ? renderCollapsedGroup(g)
    : renderGroupHeader(g) + g.members.map(m => renderCard(m, key)).join("");
  insertBlockAtTime(html, itemTime(g.members[0]));
}

function markNewCards(ids) {
  const idSet = new Set(ids.map(String));
  $itemsContainer.querySelectorAll(".item-card").forEach(el => {
    if (idSet.has(String(el.dataset.id))) el.classList.add("card-new");
  });
  // 折叠组/组头：组内任一成员是新卡片 → 组整体高亮
  $itemsContainer.querySelectorAll(".group-collapsed, .group-header").forEach(el => {
    const members = groupMap.get(el.dataset.key || "") || [];
    if (members.some(m => idSet.has(String(m.id)))) el.classList.add("card-new");
  });
}

// ── 新消息浮条 ──
function updateNewItemsBar() {
  const count = newItemIds.size;
  const top = (document.scrollingElement && document.scrollingElement.scrollTop) || 0;
  if (!count || top < 150) {
    $newItemsBar.classList.add("hidden");
    return;
  }
  $newItemsBarText.textContent = "有 " + count + " 条新信息";
  $newItemsBar.classList.remove("hidden");
}

function confirmNewItems() {
  newItemIds.clear();
  document.querySelectorAll(".card-new").forEach(el => el.classList.remove("card-new"));
  $newItemsBar.classList.add("hidden");
}

// ── 同步进度（新增消息数，含处理中；并入状态指示器胶囊） ──
// 数据源：sync_progress SSE 事件（实时权威）+ /api/status/.syncProgress 快照
// （页面刷新时机突发中途恢复）。处理中：胶囊仅显示「＋N 条新消息 · 处理中 M」
// （源状态文本隐藏、连接圆点保留）；收尾：「✓ 已同步 N 条」约 3s 后恢复
// 「源状态 · 上次同步」。updateStatus 在进度非空闲时不覆盖进度文本。
const SYNC_PROGRESS_DISMISS_MS = 3000;
let syncProgressDismissTimer = null;
let syncProgressPhase = "idle"; // "idle" | "active" | "done"
let lastStatusInfo = null;

function renderSyncProgress(p) {
  if (!p || typeof p.newCount !== "number" || typeof p.pendingCount !== "number") return;
  if (syncProgressDismissTimer) { clearTimeout(syncProgressDismissTimer); syncProgressDismissTimer = null; }
  if (p.pendingCount > 0) {
    syncProgressPhase = "active";
    $statusText.classList.add("hidden");
    $statusProgress.classList.remove("hidden");
    $statusProgress.innerHTML =
      '<span class="sp-dot" aria-hidden="true"></span>＋' + p.newCount
      + " 条新消息 · 处理中 " + p.pendingCount;
    return;
  }
  // 突发收尾（pending 归 0）：短暂展示"已同步 N 条"后恢复源状态文案
  if (p.done && p.newCount > 0) {
    syncProgressPhase = "done";
    $statusText.classList.add("hidden");
    $statusProgress.classList.remove("hidden");
    $statusProgress.textContent = "✓ 已同步 " + p.newCount + " 条新消息";
    syncProgressDismissTimer = setTimeout(restoreStatusTextAfterProgress, SYNC_PROGRESS_DISMISS_MS);
  }
}

function restoreStatusTextAfterProgress() {
  syncProgressPhase = "idle";
  $statusProgress.classList.add("hidden");
  $statusProgress.innerHTML = "";
  $statusText.classList.remove("hidden");
  // 用最近一次状态重渲染源状态文案（进度期间 updateStatus 只缓存未渲染）
  if (lastStatusInfo) updateStatus(lastStatusInfo);
}

function restoreSyncProgress(sp) {
  // 仅当进度区当前未显示且确有处理中消息时恢复（页面刷新中途突发）；
  // 实时事件正在驱动时不覆盖，避免 plain 刷新复活过期"已同步"态
  if (!sp || typeof sp.pendingCount !== "number" || sp.pendingCount <= 0) return;
  if (!$statusProgress.classList.contains("hidden")) return;
  renderSyncProgress(sp);
}

// ── 日期分组 ──
function dayLabel(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const now = new Date();
  const dayStart = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diffDays = Math.round((dayStart(now) - dayStart(d)) / 86400000);
  if (diffDays <= 0) return "今天";
  if (diffDays === 1) return "昨天";
  const year = d.getFullYear() === now.getFullYear() ? "" : d.getFullYear() + "年";
  return year + (d.getMonth() + 1) + "月" + d.getDate() + "日";
}

function renderDayDivider(label) {
  return '<div class="day-divider"><span>' + esc(label) + '</span></div>';
}

function rebuildDayDividers() {
  $itemsContainer.querySelectorAll(".day-divider").forEach(el => el.remove());
  let lastLabel = "";
  for (const child of Array.from($itemsContainer.children)) {
    const ts = parseFloat(child.dataset.msgtime || "0");
    if (!ts) continue;
    const label = dayLabel(ts);
    if (label && label !== lastLabel) {
      lastLabel = label;
      child.insertAdjacentHTML("beforebegin", renderDayDivider(label));
    }
  }
}

function cleanupOrphanDividers() {
  const kids = Array.from($itemsContainer.children);
  if (kids.length === 0) return;
  if (kids.every(c => c.classList.contains("day-divider"))) {
    showEmptyState();
    return;
  }
  let prev = null;
  for (const c of kids) {
    if (c.classList.contains("day-divider") && prev && prev.classList.contains("day-divider")) {
      c.remove();
    }
    prev = c;
  }
}

// ── Collapse Groups ──
function groupVisibleItems(items) {
  const groups = [];
  const singles = [];
  const map = new Map();
  for (const item of items) {
    const subject = (item.subject || "").trim();
    if (!subject) {
      singles.push(item);
      continue;
    }
    const key = subject + "\x01" + (item.category || "");
    if (!map.has(key)) {
      const g = { key, subject, category: item.category || "", members: [] };
      map.set(key, g);
      groups.push(g);
    }
    map.get(key).members.push(item);
  }
  return { groups, singles };
}

function renderCollapsedGroup(group) {
  // items 已按 msg_time DESC 排序，members[0] 即组内最新一张 → 代表卡
  const rep = group.members[0];
  const n = group.members.length;
  // 批量模式：组是不可分的选择单元，初始渲染即还原整组选中态
  const allSelected = batchMode && group.members.every(m => selectedIds.has(String(m.id)));
  const colorStyle = catColor.get(group.category) ? `--cat:${catColor.get(group.category)}` : "";
  return `
    <div class="group-collapsed${allSelected ? " selected" : ""}" data-key="${escAttr(group.key)}" data-msgtime="${itemTime(rep)}" data-subject="${escAttr(group.subject)}" data-category="${escAttr(group.category)}" style="${colorStyle}">
      <button class="group-more-btn group-head" data-subject="${escAttr(group.subject)}" data-cat="${escAttr(group.category)}" title="${batchMode ? "选择 / 取消选择该主体全部卡片（" + n + " 张）" : "查看该主体全部卡片"}">
        <span class="group-subject subject-link" data-subject="${escAttr(group.subject)}" title="查看该主体全部记录">${esc(group.subject)}</span>
        <span class="group-count">${n} 条</span>
        <span class="group-more">${batchMode ? "选择整组" : "查看全部"}<img src="/图标/8-界面/箭头右.svg" class="icon-sm chev" alt=""></span>
      </button>
      ${renderCard(rep, group.key)}
    </div>`;
}

function renderGroupHeader(group) {
  const n = group.members.length;
  const allSelected = batchMode && group.members.every(m => selectedIds.has(String(m.id)));
  const colorStyle = catColor.get(group.category) ? `--cat:${catColor.get(group.category)}` : "";
  return `
    <div class="group-header" data-key="${escAttr(group.key)}" data-msgtime="${itemTime(group.members[0])}" style="${colorStyle}">
      ${batchMode ? `<label class="batch-check" title="选择 / 取消选择该主体全部卡片"><input type="checkbox" ${allSelected ? "checked" : ""}></label>` : ""}
      <span class="group-subject subject-link" data-subject="${escAttr(group.subject)}" title="查看该主体全部记录">${esc(group.subject)}</span>
      <span class="group-count">${n} 条</span>
    </div>`;
}

function updateCollapseToggle() {
  if (!$collapseToggle) return;
  const mode = collapseGroups ? "collapsed" : "expanded";
  $collapseToggle.dataset.active = mode; // 驱动 .seg-pill 滑动
  $collapseToggle.querySelectorAll(".seg-btn").forEach((b) => {
    const sel = b.dataset.mode === mode;
    b.classList.toggle("active", sel);
    b.setAttribute("aria-selected", sel ? "true" : "false");
  });
}

function openGroupOverlay(subject, category) {
  const key = subject + "\x01" + (category || "");
  const members = groupMap.get(key) || [];
  if (members.length < 2) return;
  lastModalFocus = document.activeElement;
  overlayKey = key;
  overlayNeedsSync = false;
  // overlayExpandedIds 跨开关持久：重开浮层时恢复展开行（配合滚动位置记忆）
  $groupOverlayTitle.textContent = subject;
  $groupOverlayTitle.dataset.subject = subject;
  $groupOverlayTitle.classList.add("subject-link");
  renderOverlayList(key);
  $groupOverlay.classList.remove("hidden");
  syncBodyScrollLock();
  const closeBtn = document.getElementById("group-overlay-close");
  if (closeBtn) closeBtn.focus();
  // 恢复上次关闭时的滚动位置
  const saved = overlayScrolls.get(key);
  if ($groupOverlayContent && saved) $groupOverlayContent.scrollTop = saved;
}

function closeGroupOverlay() {
  if (overlayKey && $groupOverlayContent) {
    overlayScrolls.set(overlayKey, $groupOverlayContent.scrollTop || 0); // 记忆滚动位置
  }
  const needSync = overlayNeedsSync;
  overlayNeedsSync = false;
  overlayKey = "";
  $groupOverlay.classList.add("hidden");
  syncBodyScrollLock();
  if (lastModalFocus && document.contains(lastModalFocus)) lastModalFocus.focus();
  lastModalFocus = null;
  if (needSync) fetchData(); // 浮层内处理过卡片 → 收敛主列表（组结构/代表卡）
}

// 渲染浮层行（数据刷新时保留各行展开状态）
function renderOverlayList(key) {
  const members = groupMap.get(key) || [];
  if (members.length === 0) {
    $groupOverlayList.innerHTML = '<div class="ov-empty">已全部处理，关闭后主列表将更新</div>';
    return;
  }
  $groupOverlayList.innerHTML = members.map(item => renderItemRow(item, { showSubscribed: true })).join("");
  // 恢复此前展开的行（含缓存的上下文）
  for (const id of overlayExpandedIds) {
    const row = $groupOverlayList.querySelector('.ov-row[data-id="' + CSS.escape(id) + '"]');
    if (!row) continue;
    const detail = row.querySelector(".ov-detail");
    if (detail) detail.classList.remove("hidden");
    const ctxDiv = detail && detail.querySelector(".card-quote-context");
    if (ctxDiv && ctxDiv.classList.contains("hidden")) {
      ctxDiv.classList.remove("hidden");
      const source = row.dataset.source;
      const sessionId = row.dataset.sessionId;
      const msgTime = row.dataset.msgtime;
      const msgId = row.dataset.msgid;
      if (source && sessionId) {
        fetchContext(ctxDiv, source, sessionId, parseInt(msgTime) || 0, msgId);
      } else {
        ctxDiv.innerHTML = '<p class="text-muted">缺少会话ID，无法加载上下文（旧数据不支持）</p>';
      }
    }
  }
}

// 弹窗打开时锁定背景滚动，防止层级穿透
// 通用判定：任一 .modal（含插件创建的浮层）未隐藏即锁定——插件浮层自动纳入
function syncBodyScrollLock() {
  const anyOpen = !!document.querySelector(".modal:not(.hidden)");
  document.body.classList.toggle("modal-open", anyOpen);
}

// 多来源合并卡片的 source_group 为 ", " 连接的字符串，拆成多个来源 chip 展示
function sourceGroupChips(source_group) {
  return (source_group || "").split(", ").filter(Boolean)
    .map(g => `<span>${esc(g)}</span>`).join("");
}

// ── 统一行渲染：组浮层(overlay) / 日历详情(detail) / 主体时间线(timeline) 共用 ──
// opts: { cls, showSubject, showSubscribed, collapsible, showArticleLink }
function renderItemRow(item, { cls = "", showSubject = false, showSubscribed = false, collapsible = true, showArticleLink = true } = {}) {
  const verifiedClass = item.is_verified === 1 ? "memo" : item.is_verified === -1 ? "ignored" : "";
  const keyPoints = (item.key_info || "")
    .replaceAll("，", ",").split(",").map(s => s.trim()).filter(Boolean);
  const meta = [];
  for (const p of keyPoints) meta.push(`<span>${highlight(p)}</span>`);
  meta.push(sourceGroupChips(item.source_group));

  let imagesHtml = "";
  if (item.image_urls) {
    try {
      const urls = JSON.parse(item.image_urls);
      if (Array.isArray(urls) && urls.length > 0) {
        imagesHtml = '<div class="card-images">' +
          urls.map(url => `<img src="/api/media/${escAttr(item.source || "")}/${escAttr(url)}" loading="lazy" alt="消息图片">`).join("") +
          '</div>';
      }
    } catch { /* ignore parse errors */ }
  }

  const msgTime = item.msg_time
    ? new Date(item.msg_time * 1000).toLocaleString("zh-CN")
    : (item.created_at ? new Date(item.created_at).toLocaleString("zh-CN") : "");
  const catColorStyle = catColor.get(item.category) ? `--cat:${catColor.get(item.category)};` : "";
  // 按钮标签随卡片状态变化，避免"点忽略=取消忽略"的反直觉
  const memoLabel = item.is_verified === 1 ? "移出备忘录" : "加入备忘录";
  const ignoreLabel = item.is_verified === -1 ? "取消忽略" : "忽略";
  const subscribed = isSubscribed(item);

  // 详情块：collapsible 模式下包 .ov-detail（初始收起，展开时加载上下文）；
  // 日历详情（detail 模式）常显且不包外层
  const detailBlock = collapsible
    ? `<div class="ov-detail hidden">${imagesHtml}<div class="quote-meta">${esc(item.sender_name || "未知")} · ${msgTime} · ${sourceGroupChips(item.source_group)}</div>${quoteTextHtml(item)}${showArticleLink ? articleLinkHtml(item.article_url) : ""}<div class="card-quote-context hidden"><p class="text-muted">加载上下文中...</p></div></div>`
    : `${imagesHtml}<div class="quote-meta">${esc(item.sender_name || "未知")} · ${msgTime} · ${sourceGroupChips(item.source_group)}</div>${quoteTextHtml(item)}<div class="card-quote-context"><p class="text-muted">加载上下文中...</p></div>`;

  return `
    <div class="ov-row${cls ? " " + cls : ""} ${verifiedClass}" style="${catColorStyle}" data-id="${item.id}" data-source="${escAttr(item.source || "")}" data-session-id="${escAttr(item.session_id || "")}" data-msgtime="${item.msg_time || ""}" data-msgid="${escAttr(item.source_msg_id || "")}">
      <div class="ov-row-head">
        <span class="card-category" data-cat="${escAttr(item.category)}">${esc(item.category)}</span>
        ${timeBadgeHtml(item)}
        ${showSubject && item.subject ? `<button class="subject-chip subject-link" data-subject="${escAttr(item.subject)}" title="查看该主体全部记录">${esc(item.subject)}</button>` : ""}
        ${showSubscribed && subscribed ? '<span class="subs-badge" title="命中订阅关键词">已订阅</span>' : ""}
        <span class="ov-time">${esc(itemRelativeTime(item))}</span>
        <button class="btn-copy btn-copy-icon" title="复制标题与关键信息" aria-label="复制标题与关键信息"><img src="/图标/10-编辑/复制.svg" class="icon-sm" alt="复制"></button>
      </div>
      <div class="ov-title">${highlight(item.title)}</div>
      ${meta.length ? `<div class="ov-meta">${meta.join(" · ")}</div>` : ""}
      ${detailBlock}
      <div class="ov-actions">
        <span class="btn-more-wrap">
          <button class="btn-more" title="更多操作（复制 / 修改分类）" aria-label="更多操作">⋯</button>
          ${renderMoreMenu(item)}
        </span>
        ${renderItemRowButtons(item)}
        <button class="btn-memo${item.is_verified === 1 ? " active" : ""}" title="${memoLabel}">${memoLabel}</button>
        <button class="btn-ignore${item.is_verified === -1 ? " active" : ""}" title="${ignoreLabel}">${ignoreLabel}</button>
      </div>
      ${renderItemRowMenus(item)}
    </div>`;
}

// ── 统一行内动作处理（分类/复制/提醒/备忘/忽略），四个上下文共用 ──
// ctx: { rowOf(btn), closeBothOnRecat, copyItemOf(row), recatOption(id, cat), verify(id, btn, row) }
// 返回 true 表示已处理按钮（调用方应停止后续行展开等逻辑）。
function handleRowAction(e, ctx) {
  const btn = e.target.closest("button");
  if (!btn) return false;
  const row = ctx.rowOf(btn);
  const id = row ? row.dataset.id : "";
  // 插件行内按钮（提醒等）优先委派
  if (consumeItemRowExtension(e, ctx)) return true;
  // 「⋯」菜单：切换显示，并关闭其它菜单
  if (btn.classList.contains("btn-more")) {
    if (!row) return true;
    const menu = row.querySelector(".card-more-menu");
    if (!menu) return true;
    const wasHidden = menu.classList.contains("hidden");
    document.querySelectorAll(".card-more-menu").forEach(m => m.classList.add("hidden"));
    if (ctx.closeBothOnRecat) closeItemRowMenus(e); // 插件行内菜单（提醒等）
    menu.classList.toggle("hidden", !wasHidden);
    return true;
  }
  if (btn.classList.contains("more-copy")) {
    if (!row) return true;
    const it = ctx.copyItemOf(row);
    if (it) copyItem(it);
    return true;
  }
  if (btn.classList.contains("more-recat")) {
    if (!row) return true;
    ctx.recatOption(id, btn.dataset.cat);
    return true;
  }
  if (btn.classList.contains("btn-copy")) {
    if (!row) return true;
    const it = ctx.copyItemOf(row);
    if (it) copyItem(it);
    return true;
  }
  if (btn.classList.contains("btn-recat")) {
    if (!row) return true;
    const menu = row.querySelector(".card-recat-menu");
    if (!menu) return true;
    const wasHidden = menu.classList.contains("hidden");
    document.querySelectorAll(".card-recat-menu").forEach(m => m.classList.add("hidden"));
    if (ctx.closeBothOnRecat) closeItemRowMenus(e); // 插件行内菜单（提醒等）
    menu.classList.toggle("hidden", !wasHidden);
    return true;
  }
  if (btn.classList.contains("recat-option")) {
    if (!row) return true;
    ctx.recatOption(id, btn.dataset.cat);
    return true;
  }
  if (btn.classList.contains("btn-memo") || btn.classList.contains("btn-ignore")) {
    if (!row) return true;
    ctx.verify(id, btn, row);
    return true;
  }
  return false;
}

// 行主体区展开/收起完整内容（原文引用/图片/上下文），组浮层与时间线共用
function toggleRowDetail(row, expandedIds) {
  const detail = row.querySelector(".ov-detail");
  if (!detail) return;
  const isOpen = !detail.classList.contains("hidden");
  detail.classList.toggle("hidden");
  if (!isOpen) expandedIds.add(row.dataset.id);
  else expandedIds.delete(row.dataset.id);
  if (!isOpen) {
    const ctxDiv = detail.querySelector(".card-quote-context");
    if (ctxDiv && ctxDiv.classList.contains("hidden")) {
      ctxDiv.classList.remove("hidden");
      const source = row.dataset.source;
      const sessionId = row.dataset.sessionId;
      const msgTime = row.dataset.msgtime;
      const msgId = row.dataset.msgid;
      if (source && sessionId) {
        fetchContext(ctxDiv, source, sessionId, parseInt(msgTime) || 0, msgId);
      } else {
        ctxDiv.innerHTML = '<p class="text-muted">缺少会话ID，无法加载上下文（旧数据不支持）</p>';
      }
    }
  }
}

// 文章卡片原文链接：仅渲染合法 http(s) 链接，新窗口打开（url 来自 items/raw_messages.article_url）
function articleLinkHtml(url, extraCls = "") {
  const safe = typeof url === "string" ? url.trim() : "";
  if (!/^https?:\/\//i.test(safe)) return "";
  return `<a class="article-link${extraCls ? " " + extraCls : ""}" href="${escAttr(safe)}" target="_blank" rel="noopener noreferrer" title="打开原文">原文链接 ↗</a>`;
}

// 原文引用折叠：消息原文超过 _QUOTE_CLAMP_LINES 行（或单行超长文本估算超行）
// 时默认折叠——按整行截断显示开头部分、末尾以省略号收尾（不拦腰断句），
// 按钮「更多[N行]」/「查看全部内容」；点击展开显示全部原文（含省略号消失），
// 再次点击收起。主列表/浮层/时间线共用。
const _QUOTE_CLAMP_LINES = 6;
const _QUOTE_CLAMP_CHARS = 240; // 无换行长文本估算：卡片宽度下约 6 行的字符量

function quoteTextHtml(item) {
  const raw = item.source_quote || "";
  if (!raw.trim()) return ""; // 无原文：不渲染空引用框（按钮仍可加载上下文）
  const lines = raw.split("\n");
  const hiddenLines = lines.length - _QUOTE_CLAMP_LINES;
  const longSingle = hiddenLines <= 0 && raw.length > _QUOTE_CLAMP_CHARS;
  const needCollapse = hiddenLines > 0 || longSingle;
  const collapsed = needCollapse && !quoteExpandedIds.has(String(item.id));
  const fullHtml = `<span class="quote-full">${highlight(raw)}</span>`;
  if (!needCollapse) {
    return `<div class="quote-text">${fullHtml}</div>`;
  }
  // 折叠预览：整行截断（保留完整行），最后一行以省略号收尾
  let preview = "";
  if (hiddenLines > 0) {
    preview = lines.slice(0, _QUOTE_CLAMP_LINES).join("\n") + "\n…";
  } else {
    preview = raw.slice(0, _QUOTE_CLAMP_CHARS) + "…";
  }
  const label = collapsed
    ? (hiddenLines > 0 ? "更多[" + hiddenLines + "行]" : "查看全部内容")
    : "收起";
  const btn = `<button type="button" class="quote-expand-btn" data-id="${escAttr(item.id)}" data-label-collapsed="${escAttr(label)}" data-label-expanded="收起">${label}</button>`;
  return `<div class="quote-text${collapsed ? " is-collapsed" : ""}">` +
    `<span class="quote-preview">${highlight(preview)}</span>` +
    fullHtml +
    `</div>` + btn;
}

function renderCard(item, groupKey = "") {
  const verifiedClass = item.is_verified === 1 ? "memo" : item.is_verified === -1 ? "ignored" : "";
  const subscribed = isSubscribed(item);
  const batchSel = batchMode && selectedIds.has(String(item.id));
  const badge = timeBadgeInfo(item);
  const metaParts = [];
  const keyInfoRaw = item.key_info || "";
  if (keyInfoRaw) {
    const keyPoints = keyInfoRaw
      .replaceAll("，", ",")
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
    for (const p of keyPoints) {
      metaParts.push(`<span>${highlight(p)}</span>`);
    }
  }
  metaParts.push(sourceGroupChips(item.source_group));

  const msgTime = item.msg_time
    ? new Date(item.msg_time * 1000).toLocaleString("zh-CN")
    : (item.created_at ? new Date(item.created_at).toLocaleString("zh-CN") : "");

  // 渲染图片
  let imagesHtml = "";
  if (item.image_urls) {
    try {
      const urls = JSON.parse(item.image_urls);
      if (Array.isArray(urls) && urls.length > 0) {
        imagesHtml = '<div class="card-images">' +
          urls.map(url => `<img src="/api/media/${escAttr(item.source || "")}/${escAttr(url)}" loading="lazy" alt="消息图片">`).join("") +
          '</div>';
      }
    } catch { /* ignore parse errors */ }
  }

  const catColorStyle = catColor.get(item.category) ? `--cat:${catColor.get(item.category)};` : "";
  // 按钮标签随卡片状态变化，避免"点忽略=取消忽略"的反直觉
  const memoLabel = item.is_verified === 1 ? "移出备忘录" : "加入备忘录";
  const ignoreLabel = item.is_verified === -1 ? "取消忽略" : "忽略";
  // 无原文时按钮承担「查看上下文」职能，避免空引用框
  const hasQuote = !!(item.source_quote && item.source_quote.trim());

  return `
    <div class="item-card ${verifiedClass}${batchMode ? " batch-selectable" : ""}${batchSel ? " selected" : ""}${subscribed ? " card-subscribed" : ""}${badge && badge.expired ? " card-expired" : ""}" style="${catColorStyle}" data-id="${item.id}" data-key="${escAttr(groupKey)}" data-category="${escAttr(item.category)}" data-source="${escAttr(item.source || "")}" data-session-id="${escAttr(item.session_id || "")}" data-msgtime="${item.msg_time || (item.created_at ? Math.floor(new Date(item.created_at).getTime() / 1000) : "")}" data-msgid="${escAttr(item.source_msg_id || "")}">
      <div class="card-header">
        ${batchMode ? `<label class="batch-check"><input type="checkbox" ${batchSel ? "checked" : ""}></label>` : ""}
        <span class="card-category" data-cat="${escAttr(item.category)}">${esc(item.category)}</span>
        ${timeBadgeHtml(item)}
        ${item.subject && !groupKey ? `<button class="subject-chip subject-link" data-subject="${escAttr(item.subject)}" title="查看该主体全部记录">${esc(item.subject)}</button>` : ""}
        ${subscribed ? '<span class="subs-badge" title="命中订阅关键词">已订阅</span>' : ""}
        <span class="card-time"${msgTime ? ` data-tooltip="${escAttr(msgTime)}"` : ""}>${esc(itemRelativeTime(item))}</span>
        <button class="btn-copy btn-copy-icon" title="复制标题与关键信息" aria-label="复制标题与关键信息"><img src="/图标/10-编辑/复制.svg" class="icon-sm" alt="复制"></button>
      </div>
      <div class="card-title">${highlight(item.title)}</div>
      ${metaParts.length ? `<div class="card-meta">${metaParts.join(" · ")}</div>` : ""}
      ${imagesHtml}
      <div class="card-quote-toggle"><img src="/图标/8-界面/箭头下.svg" class="icon-sm" alt="">${hasQuote ? "原文引用" : "查看上下文"}</div>
      <div class="card-quote">
        <div class="card-quote-main">
          <div class="quote-meta">${esc(item.sender_name || "未知")} · ${msgTime} · ${sourceGroupChips(item.source_group)}</div>
          ${quoteTextHtml(item)}
          ${articleLinkHtml(item.article_url)}
        </div>
        <div class="card-quote-context hidden">
          <p class="text-muted">加载上下文中...</p>
        </div>
      </div>
      <div class="card-actions">
        <span class="btn-more-wrap">
          <button class="btn-more" title="更多操作（复制 / 修改分类）" aria-label="更多操作">⋯</button>
          ${renderMoreMenu(item)}
        </span>
        ${renderItemRowButtons(item)}
        <button class="btn-memo${item.is_verified === 1 ? " active" : ""}" title="${memoLabel}">${memoLabel}</button>
        <button class="btn-ignore${item.is_verified === -1 ? " active" : ""}" title="${ignoreLabel}">${ignoreLabel}</button>
      </div>
      ${renderItemRowMenus(item)}
    </div>`;
}

// ── Lightbox ──
function openLightbox(srcs, index) {
  lightboxSrcs = srcs;
  lightboxIndex = index;
  lightboxScale = 1; lightboxTx = 0; lightboxTy = 0;
  updateLightbox();
  $lightbox.classList.remove("hidden");
  syncBodyScrollLock();
  document.addEventListener("keydown", onLightboxKeydown);
}

function closeLightbox() {
  $lightbox.classList.add("hidden");
  lightboxSrcs = [];
  syncBodyScrollLock();
  document.removeEventListener("keydown", onLightboxKeydown);
}

function updateLightbox() {
  $lightboxImg.src = lightboxSrcs[lightboxIndex];
  lightboxScale = 1; lightboxTx = 0; lightboxTy = 0; // 换图复位缩放
  applyLightboxTransform();
  const multi = lightboxSrcs.length > 1;
  $lightboxPrev.classList.toggle("hidden", !multi);
  $lightboxNext.classList.toggle("hidden", !multi);
  $lightboxCounter.textContent = multi ? `${lightboxIndex + 1} / ${lightboxSrcs.length}` : "";
}

function applyLightboxTransform() {
  $lightboxImg.style.transform = "translate(" + lightboxTx + "px, " + lightboxTy + "px) scale(" + lightboxScale + ")";
}

function lightboxStep(delta) {
  lightboxIndex = (lightboxIndex + delta + lightboxSrcs.length) % lightboxSrcs.length;
  updateLightbox();
}

function onLightboxKeydown(e) {
  if (e.key === "Escape") {
    closeLightbox();
  } else if (e.key === "ArrowLeft" && lightboxSrcs.length > 1) {
    e.preventDefault();
    lightboxStep(-1);
  } else if (e.key === "ArrowRight" && lightboxSrcs.length > 1) {
    e.preventDefault();
    lightboxStep(1);
  }
}

// ── Verify ──
async function verifyItem(id, value, cardEl, { refresh = false, overlay = false } = {}) {
  const prev = cardEl.classList.contains("memo") ? 1 : cardEl.classList.contains("ignored") ? -1 : 0;
  if (prev === value) return;
  try {
    const res = await fetch(`/api/items/${id}/verify`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ verified: value }),
    });
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = await res.json();

    // 自动提醒：加入备忘录后委派插件行内扩展（reminders 插件按需自动设提醒）
    notifyItemRowVerify(id, value, { overlay: overlay });

    // 操作使卡片离开当前视图 → 提供撤销入口（点击撤销恢复原状态）
    const leaving = (currentVerified === "memo" && value !== 1)
      || (currentVerified === "ignored" && value !== -1)
      || (currentVerified === "unverified" && value !== 0);
    if (leaving) {
      const label = value === 1 ? "加入备忘录"
        : value === -1 ? "忽略"
        : (prev === 1 ? "移出备忘录" : "取消忽略");
      showUndoToast(id, prev, label);
    }

    // 刷新边栏徽章数字
    if (data.categories) {
      renderNav(data.categories, data.ignoredCount, data.memoCount);
      updateActiveNav();
    }

    // 浮层内连续处理：仅本地更新该行（淡出移除），不关浮层、不整页重拉；
    // 主列表在浮层关闭时一次性收敛（closeGroupOverlay → fetchData）
    if (overlay) {
      overlayNeedsSync = true;
      cardEl.style.transition = "opacity 0.25s";
      cardEl.style.opacity = "0";
      setTimeout(() => {
        cardEl.remove();
        if (!$groupOverlayList.querySelector(".ov-row")) {
          $groupOverlayList.innerHTML = '<div class="ov-empty">已全部处理，关闭后主列表将更新</div>';
        }
      }, 250);
      return;
    }

    // 折叠组内的代表卡：组结构（成员数、代表卡）会变，重拉整组
    if (refresh) {
      fetchData();
      return;
    }

    const btnMemo = cardEl.querySelector(".btn-memo");
    const btnIgnore = cardEl.querySelector(".btn-ignore");

    if (value === 0) {
      // Deactivated — remove all styling
      cardEl.classList.remove("memo", "ignored");
      if (btnMemo) btnMemo.classList.remove("active");
      if (btnIgnore) btnIgnore.classList.remove("active");
      // If on memo or ignored view, card no longer matches filter
      if (currentVerified === "memo" || currentVerified === "ignored") {
        cardEl.style.transition = "opacity 0.3s";
        cardEl.style.opacity = "0";
        setTimeout(() => { cardEl.remove(); cleanupOrphanDividers(); }, 300);
      }
    } else if (value === 1) {
      // Marked as memo
      if (btnMemo) btnMemo.classList.add("active");
      if (btnIgnore) btnIgnore.classList.remove("active");
      if (currentVerified === "memo") {
        cardEl.classList.add("memo");
        cardEl.classList.remove("ignored");
      } else {
        // On unverified or ignored view — card no longer matches filter
        cardEl.style.transition = "opacity 0.3s";
        cardEl.style.opacity = "0";
        setTimeout(() => { cardEl.remove(); cleanupOrphanDividers(); }, 300);
      }
    } else if (value === -1) {
      // Marked as ignored
      if (btnIgnore) btnIgnore.classList.add("active");
      if (btnMemo) btnMemo.classList.remove("active");
      if (currentVerified === "ignored") {
        cardEl.classList.add("ignored");
        cardEl.classList.remove("memo");
      } else {
        // On unverified or memo view — card no longer matches filter
        cardEl.style.transition = "opacity 0.3s";
        cardEl.style.opacity = "0";
        setTimeout(() => { cardEl.remove(); cleanupOrphanDividers(); }, 300);
      }
    }

    // 状态变更可能改变当前查询的成员数；重新拉取当前已加载页数，
    // 让卡片、总数和组数重新使用同一套服务端筛选结果。
    fetchData();
  } catch (err) {
    console.error("Verify error:", err);
    showToast("操作失败，请重试", { type: "error", duration: 4000 });
  }
}

// ── Status ──
const _STATUS_ICONS = {
  online: "/图标/8-界面/对勾.svg",
  reconnecting: "/图标/8-界面/刷新.svg",
  offline: "/图标/8-界面/叉号.svg",
  syncing: "/图标/12-杂项/加载中圆环.svg",
};
const _STATUS_LABELS = { online: "在线", reconnecting: "重连中", offline: "离线" };

// 各消息源在线状态 → { overall, partsHtml }（指示器圆点颜色与文字共用）
function _statusParts(status) {
  const sources = Object.entries(status.sources || {});
  const states = sources.map(([, s]) => s.status || "offline");
  const overall = states.length === 0 ? "offline"
    : states.every(st => st === "online") ? "online"
    : states.some(st => st === "reconnecting") ? "reconnecting"
    : "offline";
  const parts = sources.map(([name, s]) => {
    const st = s.status || "offline";
    return `<img src="${_STATUS_ICONS[st]}" class="icon" alt="">${esc(name)} ${_STATUS_LABELS[st]}`;
  });
  return { overall, parts };
}

function updateStatus(status) {
  // 记录同步状态：保存设置时据此决定立即应用或延迟到同步完成后
  lastStatusInfo = status;
  isSyncing = !!status.syncing;
  const { overall, parts } = _statusParts(status);
  $statusIndicator.className = `status ${overall}`;
  if (syncProgressPhase !== "idle") {
    // 同步进度展示中：不覆盖进度文本（源状态文案等收尾后由
    // restoreStatusTextAfterProgress 用 lastStatusInfo 重渲染）；
    // 按钮态照常维护，圆点颜色仍反映连接状态
    setSyncButton(status.syncing || manualSyncWait);
    return;
  }
  $statusText.classList.remove("hidden");
  if (status.syncing) {
    // 同步中不隐藏源在线状态：各源状态照常显示，末尾追加"正在同步"
    $statusText.innerHTML = (parts.length ? parts.join(" · ") : "未连接")
      + ` · <img src="${_STATUS_ICONS.syncing}" class="icon icon-spin" alt="">正在同步`;
    setSyncButton(true);
    return;
  }
  $statusText.innerHTML = (parts.length ? parts.join(" · ") : "未连接") + ` · 上次同步 ${syncRelativeText(status.lastSync)}`;
  setSyncButton(manualSyncWait);
}

// ── 错误/警告横幅：同步失败（lastError）、阶段缺失/无类别（lastWarning）显式提示 ──
// 关闭仅当前会话生效（下次 fetchData 若状态仍在会重现）；重试按钮复用同步入口。
function renderStatusBanner(status) {
  const banner = document.getElementById("error-banner");
  if (!banner) return;
  const err = status && status.lastError;
  const warn = status && status.lastWarning;
  banner.classList.toggle("hidden", !err && !warn);
  if (!err && !warn) {
    banner.innerHTML = "";
    return;
  }
  const isErr = !!err;
  const text = isErr ? err : warn;
  const action = isErr
    ? '<button type="button" class="error-banner-btn" data-action="retry">重试同步</button>'
    : '<button type="button" class="error-banner-btn" data-action="settings">去设置</button>';
  banner.className = "error-banner " + (isErr ? "error" : "warning");
  banner.innerHTML =
    '<span class="error-banner-text">' + esc(text) + '</span>' + action +
    '<button type="button" class="error-banner-close" title="关闭" aria-label="关闭">×</button>';
  banner.querySelector(".error-banner-close").addEventListener("click", () => {
    banner.classList.add("hidden");
    banner.innerHTML = "";
  });
  banner.querySelector(".error-banner-btn").addEventListener("click", () => {
    const btn = banner.querySelector(".error-banner-btn");
    if (btn.dataset.action === "retry") {
      const syncBtn = document.getElementById("sync-btn");
      if (syncBtn) syncBtn.click();
    } else {
      const link = document.getElementById("settings-link-top");
      if (link) link.click();
    }
  });
}

// ── Timer ──
function startRefreshTimer() {
  if (refreshTimer) clearInterval(refreshTimer);
  refreshTimer = setInterval(fetchData, refreshIntervalSec * 1000);
}

// ── Context ──

function renderContextHtml(msgs, sourceMsgId) {
  if (msgs.length === 0) {
    return '<p class="text-muted">无上下文</p>';
  }
  return '<div class="ctx-label">附近消息</div>' + msgs.map(m => {
    const t = new Date(m.time * 1000).toLocaleString("zh-CN");
    const text = (m.content || "").replace(/<[^>]*>/g, "").split("\n")[0].trim();
    if (!text) return "";
    const isTarget = !!sourceMsgId && m.msg_id === sourceMsgId;
    const cls = isTarget ? "ctx-msg ctx-target" : "ctx-msg";
    const link = articleLinkHtml(m.article_url, "ctx-article-link");
    // 链接位于消息内容（文本行）末尾，而非发送者/时间行
    return `<div class="${cls}"><span class="ctx-sender">${esc(m.sender)}</span> <span class="ctx-time">${t}</span><br>${esc(text)}${link}</div>`;
  }).join("");
}

async function fetchContext(ctxDiv, source, sessionId, aroundTime, sourceMsgId) {
  const cacheKey = source + "|" + sessionId + "|" + (aroundTime || 0);
  // 缓存只存与卡片无关的原始消息列表；高亮目标按当前卡片的 sourceMsgId 现渲染，
  // 避免同秒多条消息命中同一份已渲染 HTML 而互相串高亮。
  // 后端以 t 为锚点双向取数并接收 msgId 兜底，保证目标消息必达（高活跃会话
  // 中窗口内最早 30 条会截掉目标，见 get_context_messages）。
  let msgs = contextCache.get(cacheKey);
  if (msgs === undefined) {
    try {
      const res = await fetch(`/api/context?source=${encodeURIComponent(source)}&session_id=${encodeURIComponent(sessionId)}&t=${aroundTime || 0}&msgId=${encodeURIComponent(sourceMsgId || "")}`);
      const data = await res.json();
      msgs = data.messages || [];
      if (contextCache.size > 50) contextCache.delete(contextCache.keys().next().value);
      contextCache.set(cacheKey, msgs);
    } catch {
      ctxDiv.innerHTML = '<p class="text-muted">加载失败</p>';
      return;
    }
  }
  ctxDiv.innerHTML = renderContextHtml(msgs, sourceMsgId);
}

// ── Sessions ──

// 按时间过滤工具：值规范化 / 初始值 / 控件同步 / 设置
function normalizeSessionTimeFilter(v) {
  if (v === "all") return "all";
  const n = Number(v);
  if (!Number.isFinite(n) || n <= 0) return "all";
  return n;
}

// 初始值：localStorage 存值优先，否则服务端默认（BACKFILL_HOURS；<=0 视为“全部”）
function initSessionTimeFilter() {
  const saved = localStorage.getItem("briefdesk.sessionTimeFilter");
  const raw = saved !== null ? saved : (sessionDefaultBackfill > 0 ? sessionDefaultBackfill : "all");
  sessionTimeFilter = normalizeSessionTimeFilter(raw);
  syncSessionTimeControls();
}

// 控件状态与 sessionTimeFilter 双向同步：两框常驻共存——
// 输入框是值的唯一事实来源，下拉是快捷设置 + 档位状态指示；
// 命中预设则下拉选中该档并回填输入框，否则下拉显示“自定义”
function syncSessionTimeControls() {
  if (sessionTimeFilter === "all") {
    $sessionTimePreset.value = "all";
    $sessionTimeCustom.value = "";
  } else if (SESSION_TIME_PRESETS[String(sessionTimeFilter)] !== undefined) {
    $sessionTimePreset.value = String(sessionTimeFilter);
    $sessionTimeCustom.value = String(sessionTimeFilter);
  } else {
    $sessionTimePreset.value = "custom";
    $sessionTimeCustom.value = String(sessionTimeFilter);
  }
}

function setSessionTimeFilter(v) {
  sessionTimeFilter = normalizeSessionTimeFilter(v);
  try { localStorage.setItem("briefdesk.sessionTimeFilter", sessionTimeFilter); } catch { /* ignore */ }
  syncSessionTimeControls();
  applySessionFilters();
}

async function loadSessions() {
  try {
    const res = await fetch("/api/sessions");
    const data = await res.json();
    if (typeof data.backfillHours === "number" && Number.isFinite(data.backfillHours)) {
      sessionDefaultBackfill = data.backfillHours;
    }
    initSessionTimeFilter();
    renderSessions(data.sessions || []);
  } catch (err) {
    $sessionList.innerHTML = '<p class="text-muted">加载失败</p>';
  }
}

function renderSessions(sessions) {
  // 快照服务端启用状态，作为保存时 diff 基准（草稿模式）
  sessionOriginal = sessions.map(s => ({ source: s.source, session_id: s.session_id, enabled: s.enabled }));
  if (sessions.length === 0) {
    $sessionList.innerHTML = '<p class="text-muted">暂无群聊，等待同步后自动发现</p>';
    return;
  }
  const allEnabled = sessions.every(s => s.enabled);
  const html = [
    `<label class="session-row session-row-all">
      <input type="checkbox" id="session-select-all" ${allEnabled ? "checked" : ""}>
      <span>全选</span>
    </label>`,
    ...sessions.map(s => {
      const kindTag = s.is_official ? '公' : (s.is_group ? '群' : '私');
      return `
      <label class="session-row" data-is-group="${s.is_group ? "1" : "0"}" data-is-official="${s.is_official ? "1" : "0"}" data-source="${escAttr(s.source)}" data-last-active="${s.last_active || ""}">
        <input type="checkbox" data-source="${escAttr(s.source)}" data-session-id="${escAttr(s.session_id)}" ${s.enabled ? "checked" : ""}>
        <span class="session-name">${esc(s.name)}</span>
        <span class="text-muted" style="font-size:11px">${esc(kindTag)} · ${esc(s.source)} · ${esc(s.session_id.substring(0, 15))}...</span>
      </label>
    `;
    })
  ].join("");
  $sessionList.innerHTML = html;

  // Select all (draft mode): 只作用于当前可见行，不调 API；保存时统一应用
  document.getElementById("session-select-all").addEventListener("change", (e) => {
    if (selectAllBusy) return;
    selectAllBusy = true;
    try {
      const check = e.target.checked;
      $sessionList.querySelectorAll("input[data-session-id]").forEach(cb => {
        const row = cb.closest(".session-row");
        if (row && row.style.display !== "none") cb.checked = check;
      });
    } finally {
      selectAllBusy = false;
      updateSessionSelectAll();
    }
  });
  applySessionFilters();
  updateSessionSelectAll();
}

// 全选框三态：全选 / 部分勾选（半选）/ 未选（仅统计当前可见行）
function updateSessionSelectAll() {
  const all = document.getElementById("session-select-all");
  if (!all) return;
  const boxes = Array.from($sessionList.querySelectorAll("input[data-session-id]"))
    .filter(b => {
      const row = b.closest(".session-row");
      return row && row.style.display !== "none";
    });
  const checked = boxes.filter(b => b.checked).length;
  all.checked = boxes.length > 0 && checked === boxes.length;
  all.indeterminate = checked > 0 && checked < boxes.length;
}

// 会话行过滤判定：设置「群聊筛选」与首次使用向导 step2 共用同一规则。
// 类型多选（空集=全部，类型内 OR）+ 消息源多选（空集=全部，源内 OR）+
// 名称搜索 + 时间过滤（last_active 窗口），四者叠加 AND。
function sessionRowMatches(row, { types, sources, query, cutoff }) {
  const isGroup = row.dataset.isGroup === "1";
  const isOfficial = row.dataset.isOfficial === "1";
  const rowKind = isOfficial ? "official" : (isGroup ? "group" : "private");
  const typeOk = types.size === 0 || types.has(rowKind);
  const sourceOk = sources.size === 0 || sources.has(row.dataset.source);
  const text = row.textContent?.toLowerCase() || "";
  const textOk = !query || text.includes(query);
  const lastActive = parseInt(row.dataset.lastActive || "", 10);
  const timeOk = cutoff === 0 || (lastActive > 0 && lastActive >= cutoff);
  return typeOk && sourceOk && textOk && timeOk;
}

// 会话列表统一过滤：类型（多选，全部/群聊/私聊/公众号）+ 消息源（多选）+ 名称搜索，叠加生效
function applySessionFilters() {
  const query = $sessionSearch.value.toLowerCase().trim();
  // 按时间过滤的窗口起点（秒）：会话最近消息时间（last_active）>= 起点才显示
  const cutoff = (sessionTimeFilter === "all") ? 0 : (Date.now() / 1000) - sessionTimeFilter * 3600;
  $sessionList.querySelectorAll(".session-row:not(.session-row-all)").forEach(row => {
    row.style.display = sessionRowMatches(row, {
      types: selectedTypes,
      sources: selectedSources,
      query,
      cutoff,
    }) ? "" : "none";
  });
  updateSessionSelectAll();
}

function updateSessionTypeChips() {
  $sessionTypeFilter.querySelectorAll(".filter-chip").forEach(c => {
    const t = c.dataset.type;
    const active = (!t || t === "all") ? selectedTypes.size === 0 : selectedTypes.has(t);
    c.classList.toggle("active", active);
  });
}

// ── 消息源筛选（多选）──
// 芯片 = 实际启用源（/api/status 的 sources keys，即 .env 启用且配置完整的源），
// 与类型筛选/搜索 AND 叠加；仅显示层过滤，不参与保存 diff。

// 拉取启用源列表（设置与向导 step2 共用）：失败时退化为空列表（不阻断）
async function loadEnabledSources() {
  try {
    const res = await fetch("/api/status");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const status = await res.json();
    enabledSources = Object.keys(status.sources || {}).sort();
  } catch (err) {
    console.error("Load source filter chips error:", err);
    enabledSources = [];
  }
}

// 拉取启用源列表并渲染设置面板芯片；失败时退化为仅"全部"（不阻断设置弹窗）
async function loadSourceFilterChips() {
  await loadEnabledSources();
  renderSourceFilterChips();
}

function renderSourceFilterChips() {
  // 仅 1 个启用源时筛选无意义，标签与芯片整组隐藏
  const show = enabledSources.length >= 2;
  $sessionSourceGroup.classList.toggle("hidden", !show);
  $sessionSourceFilter.innerHTML =
    `<button type="button" class="filter-chip" data-source="all">全部</button>` +
    enabledSources.map(s =>
      `<button type="button" class="filter-chip" data-source="${escAttr(s)}">${esc(s)}</button>`
    ).join("");
  // 源列表变化（异常场景）时清理残留选中项，避免选中已不存在的源
  for (const s of selectedSources) {
    if (!enabledSources.includes(s)) selectedSources.delete(s);
  }
  updateSessionSourceChips();
}

function updateSessionSourceChips() {
  $sessionSourceFilter.querySelectorAll(".filter-chip").forEach(c => {
    const active = c.dataset.source === "all"
      ? selectedSources.size === 0
      : selectedSources.has(c.dataset.source);
    c.classList.toggle("active", active);
  });
}

// ── Categories ──

// 预设色板（颜色 + 图标组合）：默认五类沿用原颜色与图标（视觉不变）；
// 补充色块从 /图标/ 目录挑选语义合理的通用图标；默认灰配通用"文件"图标
const _CAT_PALETTE = [
  { color: "#2563EB", icon: "/图标/9-媒体/日历.svg" },
  { color: "#7C3AED", icon: "/图标/8-界面/用户组.svg" },
  { color: "#059669", icon: "/图标/2-物品/书.svg" },
  { color: "#D97706", icon: "/图标/8-界面/更改.svg" },
  { color: "#DB2777", icon: "/图标/9-媒体/时间.svg" },
  { color: "#0EA5E9", icon: "/图标/8-界面/信息.svg" },
  { color: "#F59E0B", icon: "/图标/9-媒体/标签.svg" },
  { color: "#10B981", icon: "/图标/9-媒体/文档.svg" },
  { color: "#EF4444", icon: "/图标/9-媒体/通知.svg" },
  { color: "#8B5CF6", icon: "/图标/9-媒体/书签.svg" },
  { color: "#6B7280", icon: "/图标/9-媒体/文件.svg" },
];
const _CAT_PALETTE_DEFAULT_COLOR = "#6B7280";

// 类别图标与预设颜色映射：图标不入库，渲染时按类别的 color 从色板派生
function _paletteIcon(color) {
  const p = _CAT_PALETTE.find(c => c.color === color);
  return p ? p.icon : "";
}

async function loadCategories() {
  try {
    const res = await fetch("/api/categories");
    const data = await res.json();
    const cats = data.categories || [];
    catOriginal = cats;
    catDraft = cats.map(c => ({ ...c, key: `s${c.id}` }));
    catDeleted = [];
    renderCategoryToggles();
  } catch (err) {
    $categoryToggles.innerHTML = '<p class="text-muted">加载失败</p>';
  }
}

function renderCategoryToggles() {
  if (!catDraft) return;
  const draftKeys = new Set(catDraft.map(c => c.key));
  // 待删除行已从草稿移除，追加在列表末尾展示（灰化 + 撤销）
  const pendingDel = catDeleted.filter(d => !draftKeys.has(d.key));
  const allRows = [...catDraft, ...pendingDel.map(d => d.row)];
  if (allRows.length === 0) {
    $categoryToggles.innerHTML = '<p class="text-muted">暂无类别，点击上方"添加类别"</p>';
    return;
  }
  $categoryToggles.innerHTML = allRows.map(c => {
    const isNew = c.id === null;
    const delRec = catDeleted.find(d => d.key === c.key);
    const isPendingDel = !!delRec;
    const delMarkup = isPendingDel ? `
      <span class="cat-pending-tag">保存后删除${delRec.purgeItems ? "（含卡片）" : ""}</span>
      <button class="cat-undo">撤销</button>` : `
      <label class="cat-toggle-label">
        <input type="checkbox" data-cat-id="${c.key}" ${c.enabled ? "checked" : ""}>
        <span class="cat-name">${esc(c.name)}${isNew ? ' <em class="cat-new-tag">新增</em>' : ""}</span>
        <span class="text-muted" style="font-size:11px">(${c.item_count})</span>
      </label>
      <button class="cat-edit">编辑</button>
      <button class="cat-del">删除</button>`;
    const rowIcon = _paletteIcon(c.color);
    return `
    <div class="cat-row${isPendingDel ? " cat-row-pending-del" : ""}" data-cat-id="${c.key}" data-is-new="${isNew ? "1" : ""}" data-color="${escAttr(c.color || "")}" style="--cat:${escAttr(c.color || "")}">
      <div class="cat-row-main">
        ${rowIcon ? `<img class="icon-sm cat-icon" src="${rowIcon}" alt="">` : ""}
        ${delMarkup}
      </div>
      ${isPendingDel ? "" : `
      <div class="cat-edit-form hidden">
        <input type="text" class="cat-edit-name" value="${escAttr(c.name)}" maxlength="20">
        <textarea class="cat-edit-prompt" rows="3" maxlength="50">${esc(c.prompt)}</textarea>
        <div class="cat-palette"></div>
        <div class="cat-edit-actions">
          <button class="cat-edit-save">确认</button>
          <button class="cat-edit-cancel">取消</button>
        </div>
      </div>
      <div class="cat-del-confirm hidden">
        <span class="text-muted">${isNew ? "该类别尚未保存到服务器" : `该类别有 ${c.item_count} 张卡片`}（删除将在保存后生效）</span>
        <div class="cat-del-actions">
          ${isNew ? `<button class="cat-del-keep">移除</button>` : `
          <button class="cat-del-keep">仅删除类别</button>
          <button class="cat-del-purge">删除类别及卡片</button>`}
          <button class="cat-del-cancel">取消</button>
        </div>
      </div>`}
    </div>`;
  }).join("");
}

// 色板：每个色块 = 颜色 + 图标 组合，点击选中；按颜色匹配选中态
function renderPalette(el, selectedColor) {
  el.innerHTML = _CAT_PALETTE.map(c =>
    `<span class="cat-swatch${c.color === selectedColor ? " selected" : ""}" data-color="${c.color}" data-icon="${c.icon}" style="background:${c.color}" title="${c.color}">
      <img class="cat-swatch-icon" src="${c.icon}" alt="">
    </span>`
  ).join("");
}

function getPaletteColor(el) {
  const sel = el.querySelector(".cat-swatch.selected");
  return sel ? sel.dataset.color : _CAT_PALETTE_DEFAULT_COLOR;
}

function enterCategoryEdit(row, btn) {
  btn.closest(".cat-row-main").classList.add("hidden");
  const form = row.querySelector(".cat-edit-form");
  form.classList.remove("hidden");
  renderPalette(form.querySelector(".cat-palette"), row.dataset.color || "#6B7280");
}

// 添加表单"确认"：暂存为新草稿行（id=null），点设置"保存"后才创建
function confirmAddCategory() {
  const name = $catAddName.value.trim();
  if (!name) {
    showToast("类别名称不能为空", { type: "error", duration: 4000 });
    return;
  }
  if (!catDraft) {
    showToast("类别列表尚未加载，请稍后重试", { type: "error", duration: 4000 });
    return;
  }
  catDraft.push({
    key: `n${++catSeq}`,
    id: null,
    name,
    prompt: $catAddPrompt.value.trim(),
    color: getPaletteColor($catAddPalette),
    enabled: 1,
    item_count: 0,
  });
  $catAddForm.classList.add("hidden");
  $categoryAdd.classList.remove("hidden");
  renderCategoryToggles();
}

// 行内编辑"确认"：把表单值写回草稿行，点设置"保存"后才更新
function confirmEditCategory(row) {
  const name = row.querySelector(".cat-edit-name").value.trim();
  if (!name) {
    showToast("类别名称不能为空", { type: "error", duration: 4000 });
    return;
  }
  const item = catDraft && catDraft.find(c => c.key === row.dataset.catId);
  if (item) {
    item.name = name;
    item.prompt = row.querySelector(".cat-edit-prompt").value.trim();
    item.color = getPaletteColor(row.querySelector(".cat-palette"));
  }
  renderCategoryToggles();
}

// 删除确认：从草稿移除（id=null 的新行直接丢弃；已有类别记入 catDeleted 待保存时删除）
function markDelete(key, purge) {
  if (!catDraft) return; // 草稿未加载时行渲染不出来，纯防御
  const idx = catDraft.findIndex(c => c.key === key);
  if (idx === -1) return;
  const [c] = catDraft.splice(idx, 1);
  if (c.id !== null) {
    catDeleted.push({ key: c.key, row: c, purgeItems: purge });
  }
  renderCategoryToggles();
}

// 撤销待删除：恢复完整草稿行（含未保存的编辑）
function undoDelete(key) {
  if (!catDraft) return; // 草稿未加载时撤销按钮不可达，纯防御
  const idx = catDeleted.findIndex(d => d.key === key);
  if (idx === -1) return;
  const [d] = catDeleted.splice(idx, 1);
  catDraft.push(d.row);
  renderCategoryToggles();
}

// 收集全部设置变更操作（类别 diff + 会话 diff），返回描述性操作列表；
// 类别草稿未加载或最终名称集合冲突时返回 null（已弹窗说明）。
// 在保存时刻快照——即使同步中挂起、之后弹窗重开重载草稿，操作依然有效。
function collectAllOps() {
  if (!catDraft) {
    showToast("类别列表尚未加载，请关闭设置窗口后重新打开", { type: "error", duration: 5000 });
    return null;
  }
  const ops = [];
  // 类别顺序：先删、再改、后增（改名先于同名新建执行，释放旧名避免 UNIQUE 冲突；
  // 会话开关与类别无关，放最后）
  for (const d of catDeleted) {
    // name 用服务端原名称：行可能先被改名再删除，草稿名不是服务端名，
    // currentCategory 跟随与最终名称校验都需要原名
    const orig = catOriginal.find(o => o.id === d.row.id);
    ops.push({ type: "delete", id: d.row.id, name: orig ? orig.name : d.row.name, purgeItems: d.purgeItems });
  }
  for (const c of catDraft) {
    if (c.id === null) continue;
    const orig = catOriginal.find(o => o.id === c.id);
    if (!orig) continue;
    const body = {};
    if (c.name !== orig.name) body.name = c.name;
    if (c.prompt !== orig.prompt) body.prompt = c.prompt;
    if (c.color !== orig.color) body.color = c.color;
    if (Object.keys(body).length) {
      ops.push({ type: "update", id: c.id, origName: orig.name, ...body });
    }
    if (c.enabled !== orig.enabled) {
      ops.push({ type: "toggle", id: c.id });
    }
  }
  for (const c of catDraft) {
    if (c.id !== null) continue;
    ops.push({ type: "create", name: c.name, prompt: c.prompt, color: c.color, enabled: c.enabled });
  }
  // 会话：对比 sessionOriginal 快照，翻转变更项
  $sessionList.querySelectorAll("input[data-session-id]").forEach(cb => {
    const orig = sessionOriginal.find(s => s.source === cb.dataset.source && s.session_id === cb.dataset.sessionId);
    if (!orig) return;
    if (orig.enabled !== (cb.checked ? 1 : 0)) {
      ops.push({ type: "sessionToggle", source: cb.dataset.source, sessionId: cb.dataset.sessionId });
    }
  });
  // 最终名称集合校验：改名 A→B 且新建 B、两类别改名为同一新名等在服务端必然
  // 409，提前拦截并指明冲突名，避免操作执行到一半才失败。
  // 新建名撞"正在被删除/改名的旧名"不算冲突（先删后建/先改后建可成功）。
  const nameCounts = new Map(); // name → 最终出现次数（现有类别各计 1）
  for (const c of catOriginal) nameCounts.set(c.name, 1);
  const addCount = (name, delta) => nameCounts.set(name, (nameCounts.get(name) || 0) + delta);
  for (const op of ops) {
    if (op.type === "delete") addCount(op.name, -1);
    else if (op.type === "update" && op.name) {
      addCount(op.origName, -1);
      addCount(op.name, +1);
    } else if (op.type === "create") addCount(op.name, +1);
  }
  for (const [name, cnt] of nameCounts) {
    if (cnt > 1) {
      showToast("类别名称冲突：\"" + name + "\" 会被多个类别占用，请先调整", { type: "error", duration: 6000 });
      return null;
    }
  }
  return ops;
}

// 通用 POST JSON 辅助：body 为 undefined 时不带请求体；非 2xx 抛错
async function postJson(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

// 执行设置变更操作列表（保存时或同步完成后共用）；失败抛错由调用方处理
async function runSettingsOps(ops) {
  for (const op of ops) {
    switch (op.type) {
      case "delete":
        await postJson(`/api/categories/${encodeURIComponent(op.id)}/delete`, { purgeItems: op.purgeItems });
        if (currentCategory === op.name) currentCategory = "全部";
        break;
      case "create":
        await postJson("/api/categories", { name: op.name, prompt: op.prompt, color: op.color, enabled: op.enabled });
        break;
      case "update":
        await postJson(`/api/categories/${encodeURIComponent(op.id)}/update`, { name: op.name, prompt: op.prompt, color: op.color });
        // 当前选中的就是被改名的类别 → 跟随新名，避免死选中
        if (currentCategory === op.origName) currentCategory = op.name;
        break;
      case "toggle":
        await postJson(`/api/categories/${encodeURIComponent(op.id)}/toggle`);
        break;
      case "sessionToggle":
        await postJson(`/api/sessions/${encodeURIComponent(op.source)}/${encodeURIComponent(op.sessionId)}/toggle`);
        break;
    }
  }
}

// ── Settings ──
// 类别启用/停用已由后端持久化（categories.enabled），localStorage 只存刷新间隔
function loadSettings() {
  try {
    const saved = JSON.parse(localStorage.getItem("briefdesk-settings") || "{}");
    refreshIntervalSec = Math.max(30, parseInt(saved.refreshInterval, 10) || 300);
  } catch { /* ignore */ }
  $refreshInterval.value = refreshIntervalSec;
  notifyMode = localStorage.getItem("briefdesk.notifyMode") || "off";
  if ($notifyMode) $notifyMode.value = notifyMode;
  hideExpired = localStorage.getItem("briefdesk.hideExpired") === "1";
  if ($hideExpiredToggle) $hideExpiredToggle.classList.toggle("active", hideExpired);
}

function saveSettings() {
  refreshIntervalSec = Math.max(30, parseInt($refreshInterval.value) || 300);
  $refreshInterval.value = refreshIntervalSec;
  localStorage.setItem("briefdesk-settings", JSON.stringify({
    refreshInterval: refreshIntervalSec,
  }));
}

// ── Toast ──
function showToast(message, { type = "info", duration = 3500, actionLabel = "", actionFn = null } = {}) {
  const el = document.createElement("div");
  el.className = "toast " + type;
  const msg = document.createElement("span");
  msg.className = "toast-msg";
  msg.textContent = message;
  el.appendChild(msg);
  if (actionLabel && actionFn) {
    const btn = document.createElement("button");
    btn.className = "toast-action";
    btn.type = "button";
    btn.textContent = actionLabel;
    btn.addEventListener("click", () => { dismissToast(el); actionFn(); });
    el.appendChild(btn);
  }
  const close = document.createElement("button");
  close.className = "toast-close";
  close.type = "button";
  close.setAttribute("aria-label", "关闭");
  close.textContent = "×";
  close.addEventListener("click", () => dismissToast(el));
  el.appendChild(close);
  $toastContainer.appendChild(el);
  while ($toastContainer.children.length > 5) dismissToast($toastContainer.firstElementChild, true);
  el._timer = setTimeout(() => dismissToast(el), duration);
  return el;
}

function dismissToast(el, immediate = false) {
  if (!el || !el.isConnected) return;
  clearTimeout(el._timer);
  if (immediate) { el.remove(); return; }
  el.classList.add("toast-out");
  setTimeout(() => el.remove(), 260);
}

// ── 撤销（忽略/备忘录）──
function showUndoToast(id, prevValue, label) {
  const undo = { id, prevValue };
  lastUndo = undo;
  showToast("已" + label, {
    type: "info",
    duration: 6000,
    actionLabel: "撤销",
    actionFn: () => {
      if (lastUndo !== undo) return; // 已有更新的操作，本次撤销失效
      lastUndo = null;
      undoVerify(undo);
    },
  });
}

async function undoVerify(u) {
  try {
    const res = await fetch("/api/items/" + u.id + "/verify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ verified: u.prevValue }),
    });
    if (!res.ok) throw new Error("HTTP " + res.status);
    showToast("已撤销", { type: "success", duration: 2000 });
    fetchData();
  } catch {
    showToast("撤销失败，请重试", { type: "error", duration: 4000 });
  }
}

// ── 插件视图钩子（P5）：核心视图路由的可扩展点 ──
// 插件（如 calendar）注册视图后，核心的 hash 路由 / fetchData / Esc / 侧边栏
// 数据联动自动委派；视图前端（DOM/样式/交互）全部由插件包提供（ui/ui.js）。
// 视图对象约定：
//   { name, matches(v), hash(v), isActive(), refresh(), onEsc(), buildHash(), sidebarReady() }
// - matches(v)：v 是否属于本视图（parseHash 产出的视图字面量 { view: "..." }）
// - hash(v)：hash 变化委派——匹配本视图时进入，否则退出（v 可能为 null）
// - isActive()：当前是否处于该视图（fetchData 据此委派 refresh）
// - onEsc()：Esc 优先于设置弹窗消费；返回 true 表示已处理
// - buildHash()：返回本视图的 hash（如 "#calendar"）或 null
// - sidebarReady()：侧边栏/颜色数据就绪（fetchSidebarData 末尾）通知
const pluginViews = [];
function registerPluginView(view) { pluginViews.push(view); }
function exitPluginViews() { pluginViews.forEach(v => v.hash && v.hash(null)); }
function notifyPluginViews(method, arg) {
  pluginViews.forEach(v => { const f = v[method]; if (f) f(arg); });
}
function consumePluginEsc() {
  for (const v of pluginViews) if (v.onEsc && v.onEsc()) return true;
  return false;
}
function consumePluginRefresh() {
  for (const v of pluginViews) {
    if (v.isActive && v.isActive()) {
      if (v.refresh) v.refresh();
      return true;
    }
  }
  return false;
}

// ── 插件行内扩展（P5）：卡片行动作区按钮由插件注册 ──
// 插件（如 reminders）注册后，renderItemRow / renderCard 会在动作区末尾渲染
// 其按钮（renderButton）与行末菜单（renderMenu），handleRowAction 先委派插件
// 处理（handle 返回 true = 已消费），文档级点击关闭行内菜单时一并通知
// （closeMenus），核心 verifyItem 成功后通知 onVerify。
// 扩展对象约定：
//   { name, renderButton(item), renderMenu(item), handle(e, ctx), closeMenus(e), onVerify(id, value, opts) }
const itemRowExtensions = [];
function registerItemRowExtension(ext) { itemRowExtensions.push(ext); }
function renderItemRowButtons(item) {
  return itemRowExtensions.map(ext => (ext.renderButton ? ext.renderButton(item) : "")).join("");
}
function renderItemRowMenus(item) {
  return itemRowExtensions.map(ext => (ext.renderMenu ? ext.renderMenu(item) : "")).join("");
}
function consumeItemRowExtension(e, ctx) {
  for (const ext of itemRowExtensions) {
    if (ext.handle && ext.handle(e, ctx)) return true;
  }
  return false;
}
function closeItemRowMenus(e) {
  itemRowExtensions.forEach(ext => ext.closeMenus && ext.closeMenus(e));
}
function notifyItemRowVerify(id, value, opts) {
  itemRowExtensions.forEach(ext => ext.onVerify && ext.onVerify(id, value, opts));
}

// ── Hash 路由（视图可分享 / 刷新保留）──
function parseHash() {
  let h = "";
  try {
    h = decodeURIComponent((location.hash || "").replace(/^#/, "")).trim();
  } catch { return null; }
  if (!h) return null;
  if (h === "memo") return { category: "全部", verified: "memo", q: "" };
  if (h === "ignored") return { category: "全部", verified: "ignored", q: "" };
  if (h === "calendar") return { view: "calendar" }; // 插件视图字面量（calendar 等）
  if (h.startsWith("subject=")) return { subject: h.slice(8) };
  if (h.startsWith("cat=")) return { category: h.slice(4), verified: "unverified", q: "" };
  if (h.startsWith("q=")) return { category: "全部", verified: "unverified", q: h.slice(2) };
  return null;
}

function buildHash() {
  for (const v of pluginViews) { // 插件视图优先（calendar 等）
    const h = v.buildHash && v.buildHash();
    if (h) return h;
  }
  if (currentSearch) return "#q=" + encodeURIComponent(currentSearch);
  if (currentVerified === "memo") return "#memo";
  if (currentVerified === "ignored") return "#ignored";
  if (currentCategory !== "全部") return "#cat=" + encodeURIComponent(currentCategory);
  return "";
}

function syncHash(mode = "push") {
  // push：视图切换进入历史，浏览器后退可逐步回到上一视图；
  // replace：仅初始化/守卫复位时使用，不产生历史条目
  const h = timeline ? "#subject=" + encodeURIComponent(timeline.subject) : buildHash();
  history[mode + "State"](null, "", location.pathname + location.search + h);
  if (!currentSearch) {
    try {
      localStorage.setItem("briefdesk.lastView", JSON.stringify({ category: currentCategory, verified: currentVerified }));
    } catch { /* ignore */ }
  }
}

function applyHashView(v) {
  if (searchDebounceTimer) { clearTimeout(searchDebounceTimer); searchDebounceTimer = null; }
  currentCategory = v.category;
  currentVerified = v.verified;
  currentSearch = v.q;
  preSearchCategory = "";
  searchFilterCat = "";
  searchFilterRange = "";
  searchFilterGroup = "";
  $itemSearch.value = v.q;
  $itemSearchClear.classList.toggle("hidden", !v.q);
  updateActiveNav();
}

function initViewFromHash() {
  const v = parseHash();
  if (v) {
    if (v.view) { notifyPluginViews("hash", v); return; } // 插件视图（calendar 等）
    if (v.subject) { openSubjectTimeline(v.subject, { syncHash: false }); return; }
    applyHashView(v);
    return;
  }
  // 无 hash：回退 localStorage 记忆的上次视图
  try {
    const last = JSON.parse(localStorage.getItem("briefdesk.lastView") || "null");
    if (last) {
      if (last.verified === "memo" || last.verified === "ignored") {
        currentCategory = "全部";
        currentVerified = last.verified;
      } else if (last.category && last.category !== "全部") {
        currentCategory = last.category;
        currentVerified = "unverified";
      }
    }
  } catch { /* ignore */ }
  syncHash("replace");
}

// ── 相对时间（前端计算，P5 起后端不再附加）──
function relativeTimeStr(pastTs, nowTs) {
  const diff = Math.floor(nowTs - pastTs);
  if (diff < 60) return "刚刚";
  if (diff < 3600) return Math.floor(diff / 60) + "分钟前";
  if (diff < 86400) return Math.floor(diff / 3600) + "小时前";
  return Math.floor(diff / 86400) + "天前";
}

// 卡片行的相对时间：msg_time（UNIX 秒）优先，回退 created_at（ISO）；
// 脏值统一兜底"刚刚"（与后端原 with_relative 语义一致，避免渲染异常）
function itemRelativeTime(item) {
  let ts = null;
  if (item.msg_time) {
    const n = parseFloat(item.msg_time);
    if (!isNaN(n)) ts = n;
  }
  if (ts === null) {
    const d = new Date(String(item.created_at || "").replace(" ", "T"));
    ts = isNaN(d.getTime()) ? Date.now() / 1000 : d.getTime() / 1000;
  }
  return relativeTimeStr(ts, Date.now() / 1000);
}

// 上次同步的相对时间（/api/status 只下发 lastSync ISO 串）
function syncRelativeText(lastSync) {
  if (!lastSync) return "从未同步";
  const d = new Date(String(lastSync).replace(" ", "T"));
  if (isNaN(d.getTime())) return "从未同步";
  return relativeTimeStr(d.getTime() / 1000, Date.now() / 1000);
}

function startTimeTicker() {
  if (timeTicker) clearInterval(timeTicker);
  timeTicker = setInterval(() => {
    const now = Date.now() / 1000;
    document.querySelectorAll("[data-msgtime]").forEach(el => {
      const ts = parseFloat(el.getAttribute("data-msgtime"));
      if (!ts) return;
      const span = el.querySelector(".card-time, .ov-time");
      if (span) span.textContent = relativeTimeStr(ts, now);
    });
  }, 60000);
}

// ── 搜索过滤条 ──
function renderFilterBar() {
  const show = !!currentSearch;
  $filterBar.classList.toggle("hidden", !show);
  if (!show) return;
  const cats = [{ name: "全部类别", color: "" }].concat((searchCats || []).map(c => ({ name: c.name, color: c.color })));
  let chips = "";
  for (const c of cats) {
    const active = c.name === "全部类别" ? !searchFilterCat : searchFilterCat === c.name;
    let dot = "";
    if (c.color) dot = '<span class="filter-dot" style="background:' + escAttr(c.color) + '"></span>';
    chips += '<button type="button" class="filter-chip' + (active ? " active" : "") + '" data-cat="' + escAttr(c.name) + '">' + dot + esc(c.name) + '</button>';
  }
  let group = '<select class="filter-range filter-group" aria-label="按来源群过滤">';
  group += '<option value="">全部来源</option>';
  for (const g of searchGroups) {
    group += '<option value="' + escAttr(g) + '"' + (searchFilterGroup === g ? " selected" : "") + '>' + esc(g) + '</option>';
  }
  group += '</select>';
  let range = '<select class="filter-range" aria-label="按时间范围过滤">';
  range += '<option value="">全部时间</option>';
  const ranges = [["1d", "24 小时内"], ["3d", "3 天内"], ["7d", "7 天内"]];
  for (const [v, label] of ranges) {
    range += '<option value="' + v + '"' + (searchFilterRange === v ? " selected" : "") + '>' + label + '</option>';
  }
  range += '</select>';
  $filterBar.innerHTML = '<div class="filter-chips" role="group" aria-label="按类别过滤">' + chips + '</div>' + group + range;
}

// ── 弹窗与同步按钮 ──
function setSettingsPanel(name) {
  // 二级菜单切换：菜单项高亮 + 面板显隐
  $settingsMenu.querySelectorAll(".settings-menu-item").forEach(b => {
    b.classList.toggle("active", b.dataset.panel === name);
  });
  $settingsModal.querySelectorAll(".settings-panel").forEach(p => {
    p.classList.toggle("hidden", p.dataset.panel !== name);
  });
}

async function loadAboutSources() {
  // 「关于」页启用消息源：复用 /api/status，失败不阻塞弹窗
  const el = document.getElementById("about-sources");
  if (!el) return;
  try {
    const res = await fetch("/api/status");
    if (!res.ok) throw new Error("HTTP " + res.status);
    const status = await res.json();
    const srcs = Object.keys(status.sources || {}).sort();
    el.innerHTML = srcs.length
      ? srcs.map(s => '<span class="about-source-chip">' + esc(s) + "</span>").join("")
      : '<p class="text-muted">未启用任何消息源</p>';
  } catch {
    el.innerHTML = '<p class="text-muted">加载失败</p>';
  }
}

async function loadPlugins() {
  // 「插件」页：/api/plugins 元数据（名称/版本/状态/原因），失败不阻塞弹窗
  if (!$pluginsList) return;
  try {
    const res = await fetch("/api/plugins");
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = await res.json();
    const plugins = Array.isArray(data.plugins) ? data.plugins : [];
    if (!plugins.length) {
      $pluginsList.innerHTML = '<p class="text-muted">未发现任何插件</p>';
      return;
    }
    $pluginsList.innerHTML = plugins.map(p => {
      const statusCls = p.status === "loaded"
        ? "plugin-status-ok"
        : (p.status === "disabled" ? "plugin-status-warn" : "plugin-status-err");
      const reason = p.reason ? '<span class="text-muted"> — ' + esc(p.reason) + "</span>" : "";
      return '<div class="plugin-row">'
        + '<span class="plugin-name">' + esc(p.name) + "</span>"
        + '<span class="plugin-version">v' + esc(p.version) + "</span>"
        + '<span class="plugin-status ' + statusCls + '">' + esc(p.status) + "</span>"
        + reason
        + "</div>";
    }).join("");
  } catch {
    $pluginsList.innerHTML = '<p class="text-muted">加载失败</p>';
  }
}

async function loadPluginFrontends() {
  // 插件前端加载器：插件的前端部分（ui.js / ui.css）随插件包分发、
  // 由本加载器统一装配。核心不写死任何插件功能入口。
  // 1) 读取 /api/plugins 元数据，取已加载（loaded）插件名单；
  // 2) 声明式入口：index.html 中带 [data-plugin-entry="<name>"] 的元素默认隐藏，
  //    显隐由对应插件 ui.js 决定；
  // 3) 只对声明了前端资源的插件（has_frontend=true）注入
  //    /plugin-assets/<name>/ui.css（样式）与 ui.js（脚本）——无前端资源的
  //    插件（如 weflow/ai_provider）不请求，避免 404 触发浏览器严格 MIME
  //    检查告警；has_frontend 缺失（旧版元数据）时保守注入，404 静默；
  // 4) 注入完成后调用 window.briefdeskPlugins.<name>.init({ isLoaded }),
  //    init 抛错只 console.warn，不阻断页面其余初始化。
  let data = null;
  try {
    const res = await fetch("/api/plugins");
    if (res.ok) data = await res.json();
  } catch { /* 元数据不可用 */ }
  if (!data) return; // 拿不到插件状态：不干预入口可见性（保持默认）
  const loadedPlugins = (Array.isArray(data.plugins) ? data.plugins : [])
    .filter(p => p.status === "loaded");
  const loaded = new Set(loadedPlugins.map(p => p.name));

  // 声明式入口默认隐藏：未加载插件的入口不显示
  document.querySelectorAll("[data-plugin-entry]").forEach(el => el.classList.add("hidden"));

  for (const p of loadedPlugins) {
    if (p.has_frontend === false) continue; // 插件未声明前端资源：跳过注入
    await injectPluginStyle(p.name);
    await injectPluginScript(p.name);
  }

  const plugins = window.briefdeskPlugins || {};
  Object.keys(plugins).forEach(name => {
    const p = plugins[name];
    if (!p || typeof p.init !== "function") return;
    try {
      p.init({ isLoaded: n => loaded.has(n) });
    } catch (e) {
      console.warn("插件前端初始化失败: " + name, e);
    }
  });

  // 行内扩展（提醒按钮等）就绪后补渲染：hash 直达（如 #subject=）时时间线浮层
  // 可能在插件装配前已用 renderItemRow 渲染，需重渲染以补齐插件按钮
  if (timeline) renderTimelineList();
}

function injectPluginStyle(name) {
  // 注入插件前端样式 /plugin-assets/<name>/ui.css；404/加载失败静默跳过
  return new Promise(resolve => {
    const l = document.createElement("link");
    l.rel = "stylesheet";
    l.href = "/plugin-assets/" + encodeURIComponent(name) + "/ui.css";
    l.onload = resolve;
    l.onerror = resolve;
    document.head.appendChild(l);
  });
}

function injectPluginScript(name) {
  // 注入插件前端脚本 /plugin-assets/<name>/ui.js；404/加载失败静默跳过
  // （同源注入，CSP script-src 'self' 允许；不阻塞后续插件装配）
  return new Promise(resolve => {
    const s = document.createElement("script");
    s.src = "/plugin-assets/" + encodeURIComponent(name) + "/ui.js";
    s.onload = resolve;
    s.onerror = resolve;
    document.head.appendChild(s);
  });
}

function closeSettingsModal() {
  $settingsModal.classList.add("hidden");
  syncBodyScrollLock();
  if (lastModalFocus && document.contains(lastModalFocus)) lastModalFocus.focus();
  lastModalFocus = null;
}

function setSyncButton(busy) {
  const want = busy ? "busy" : "idle";
  if ($syncBtn.dataset.state === want) return;
  $syncBtn.dataset.state = want;
  $syncBtn.disabled = busy;
  $syncBtn.innerHTML = busy
    ? '<img src="/图标/12-杂项/加载中圆环.svg" class="icon icon-spin" alt="">同步中...'
    : '<img src="/图标/8-界面/刷新.svg" class="icon" alt="">同步消息';
}

// ── Helpers ──
// 在已转义文本中高亮全部匹配的搜索词（多词 OR：每个词的所有出现都高亮）。
// 搜索词同样先经 esc() 转义再匹配：转义后文本中的 & < > 等实体（&amp;、&lt;）
// 不会被搜索词命中破坏；正则元字符再单独转义。
function highlight(str) {
  const escaped = esc(str);
  if (!currentSearch) return escaped;
  const terms = currentSearch.toLowerCase().split(/\s+/).filter(Boolean);
  if (!terms.length) return escaped;
  let out = escaped;
  for (const term of terms) {
    const escapedTerm = esc(term).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    if (!escapedTerm) continue;
    const re = new RegExp(escapedTerm, "gi");
    out = out.replace(re, (m) => "<mark>" + m + "</mark>");
  }
  return out;
}

function esc(str) {
  if (!str) return "";
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

// 属性专用转义：esc() 基于 textContent→innerHTML，不会转义双/单引号，
// 用于带引号属性时需追加引号转义，防止属性闭合注入。
function escAttr(str) {
  return esc(str)
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// ── 深色模式 ──
function initTheme() {
  applyTheme();
}

// ── 网页图标（favicon）：素材库图标 + 随机主题色 ──
// 直接使用 ui/图标/ 素材库的 SVG（fill="currentColor"），加载后把填充色
// 替换为随机主题色并以内联 data URI 呈现；加载失败保留默认 favicon。
const _FAVICON_ICON = "/图标/8-界面/网格.svg";
const _FAVICON_COLORS = [
  "#2563EB", "#7C3AED", "#059669", "#D97706", "#DB2777",
  "#0EA5E9", "#F59E0B", "#10B981", "#EF4444", "#8B5CF6", "#1B5E41",
];

async function applyRandomFavicon() {
  const color = _FAVICON_COLORS[Math.floor(Math.random() * _FAVICON_COLORS.length)];
  try {
    const res = await fetch(_FAVICON_ICON);
    if (!res.ok) return;
    let svg = await res.text();
    if (!svg.trim().startsWith("<svg")) return; // SPA fallback 防御
    // 素材库图标统一 fill="currentColor"：替换为随机主题色；
    // 无 fill 属性时兜底注入内联 style
    if (svg.includes('fill="currentColor"')) {
      svg = svg.split('fill="currentColor"').join('fill="' + color + '"');
    } else {
      svg = svg.replace(/<svg/, '<svg style="fill:' + color + '"');
    }
    let link = document.querySelector('link[rel="icon"]');
    if (!link) {
      link = document.createElement("link");
      link.rel = "icon";
      document.head.appendChild(link);
    }
    link.type = "image/svg+xml";
    link.href = "data:image/svg+xml;utf8," + encodeURIComponent(svg);
  } catch {
    /* 保留 index.html 默认 favicon */
  }
}

function resolveTheme(mode) {
  if (mode === "system") {
    return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  return mode === "dark" ? "dark" : "light";
}

function applyTheme() {
  const mode = localStorage.getItem("briefdesk.theme") || "light";
  document.documentElement.dataset.theme = resolveTheme(mode);
  updateThemeChips();
}

function updateThemeChips() {
  if (!$themeToggle) return;
  const mode = localStorage.getItem("briefdesk.theme") || "light";
  $themeToggle.querySelectorAll(".filter-chip").forEach(c => {
    c.classList.toggle("active", c.dataset.mode === mode);
  });
}

// ── 桌面通知 ──
function notifyNewItems(items) {
  if (!document.hidden || !items.length) return;
  if (!("Notification" in window) || Notification.permission !== "granted") return;
  if (notifyMode === "off") return;
  if (notifyMode === "keywords") {
    items = items.filter(isSubscribed);
    if (!items.length) return;
  }
  items = items.filter(it => !isBlocked(it)); // 黑名单优先：命中不通知
  if (!items.length) return;
  const now = Date.now();
  if (now - lastNotifyAt < 30000) return; // 30 秒节流，避免批量回填刷屏
  lastNotifyAt = now;
  const first = items[0];
  const body = (first.category ? first.category + " · " : "") + (first.title || "");
  try {
    const n = new Notification("收到 " + items.length + " 条新信息", {
      body: body.slice(0, 120),
      tag: "briefdesk-new-items",
    });
    n.onclick = () => { window.focus(); n.close(); };
  } catch { /* 忽略通知失败 */ }
}

// ── 关键词订阅 ──
function loadSubscriptions() {
  try {
    const saved = JSON.parse(localStorage.getItem("briefdesk.subscriptions") || "[]");
    subscriptions = Array.isArray(saved)
      ? saved.filter(s => s && typeof s.keywords === "string")
      : [];
  } catch { subscriptions = []; }
  renderSubsList();
}

function saveSubscriptions() {
  try { localStorage.setItem("briefdesk.subscriptions", JSON.stringify(subscriptions)); } catch { }
}

function enabledSubKeywords() {
  return subscriptions
    .filter(s => s.enabled && (s.keywords || "").trim())
    .map(s => s.keywords.trim())
    .join(" ");
}

function isSubscribed(item) {
  if (!subscriptions.some(s => s.enabled)) return false;
  const text = [item.title, item.key_info, item.subject, item.source_quote]
    .join("\n").toLowerCase();
  for (const s of subscriptions) {
    if (!s.enabled) continue;
    const kws = (s.keywords || "").toLowerCase().split(/\s+/).filter(Boolean);
    if (kws.some(k => text.includes(k))) return true;
  }
  return false;
}

function renderSubsList() {
  if (!$subsList) return;
  if (!subscriptions.length) {
    $subsList.innerHTML = '<p class="text-muted">暂无订阅，点击上方添加</p>';
    return;
  }
  $subsList.innerHTML = subscriptions.map(s => `
    <div class="subs-row" data-id="${escAttr(s.id)}">
      <label><input type="checkbox" class="subs-enabled" ${s.enabled ? "checked" : ""}> <span>${esc(s.keywords)}</span></label>
      <button class="subs-del">删除</button>
    </div>`).join("");
}

// 订阅徽章：当前列表内命中订阅的卡片数
function updateSubsBadge() {
  const $c = document.getElementById("subs-count");
  if (!$c) return;
  $c.textContent = currentItems.filter(isSubscribed).length;
}

// ── 降噪黑名单（与订阅同构；命中卡片在渲染层隐藏，不触碰后端）──
function loadBlocklist() {
  try {
    const saved = JSON.parse(localStorage.getItem("briefdesk.blocklist") || "[]");
    blocklist = Array.isArray(saved)
      ? saved.filter(s => s && typeof s.keywords === "string")
      : [];
  } catch { blocklist = []; }
  renderBlocklist();
}

function saveBlocklist() {
  try { localStorage.setItem("briefdesk.blocklist", JSON.stringify(blocklist)); } catch { }
}

function isBlocked(item) {
  if (!blocklist.some(s => s.enabled)) return false;
  const text = [item.title, item.key_info, item.subject, item.source_quote, item.sender_name]
    .join("\n").toLowerCase();
  for (const s of blocklist) {
    if (!s.enabled) continue;
    const kws = (s.keywords || "").toLowerCase().split(/\s+/).filter(Boolean);
    if (kws.some(k => text.includes(k))) return true;
  }
  return false;
}

function renderBlocklist() {
  if (!$blockList) return;
  if (!blocklist.length) {
    $blockList.innerHTML = '<p class="text-muted">暂无黑名单，命中关键词的卡片将在列表中隐藏</p>';
    return;
  }
  $blockList.innerHTML = blocklist.map(s => `
    <div class="subs-row" data-id="${escAttr(s.id)}">
      <label><input type="checkbox" class="block-enabled" ${s.enabled ? "checked" : ""}> <span>${esc(s.keywords)}</span></label>
      <button class="block-del">删除</button>
    </div>`).join("");
}

// ── 数据导出下载（Content-Disposition 文件名）──
async function downloadExport(url) {
  try {
    const res = await fetch(url);
    if (!res.ok) throw new Error("HTTP " + res.status);
    const blob = await res.blob();
    const cd = res.headers.get("Content-Disposition") || "";
    const m = cd.match(/filename="?([^";]+)"?/i);
    const filename = m ? m[1] : "briefdesk-export.csv";
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    showToast("导出已开始", { type: "success", duration: 2000 });
  } catch (err) {
    console.error("Export error:", err);
    showToast("导出失败，请重试", { type: "error", duration: 4000 });
  }
}

// ── 一键复制 ──
async function copyItem(item) {  const lines = [];
  if (item.title) lines.push(item.title);
  if (item.key_info) lines.push(item.key_info);
  if (item.source_quote) lines.push(item.source_quote);
  const text = lines.join("\n");
  if (!text) return;
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
    } else {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      ta.remove();
    }
    showToast("已复制", { type: "success", duration: 2000 });
  } catch {
    showToast("复制失败，请手动选择文本", { type: "error", duration: 3000 });
  }
}

// ── 手动修正分类 ──
function renderRecatMenu(item) {
  const cats = Array.from(catColor.keys());
  if (!cats.length) return "";
  return '<div class="card-recat-menu hidden">' +
    cats.map(c => `<button class="recat-option${c === item.category ? " current" : ""}" data-cat="${escAttr(c)}">${esc(c)}</button>`).join("") +
    '</div>';
}

// 操作区收纳：「⋯」菜单内容 = 复制 + 分类列表（主操作备忘录/忽略仍在动作区直显）
function renderMoreMenu(item) {
  const cats = Array.from(catColor.keys());
  const catItems = cats.map(c =>
    `<button type="button" class="more-option more-recat" data-cat="${escAttr(c)}">` +
    `<span class="more-dot" style="background:${escAttr(catColor.get(c) || "#999")}"></span>${esc(c)}` +
    (c === item.category ? '<span class="more-current">✓</span>' : "") +
    '</button>'
  ).join("");
  return `
    <div class="card-more-menu hidden">
      <div class="more-group-label">更换类别</div>
      ${cats.length ? catItems : '<div class="more-empty">暂无类别</div>'}
    </div>`;
}

async function doRecategorize(id, category) {
  try {
    const res = await fetch("/api/items/" + encodeURIComponent(id) + "/recategorize", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ category }),
    });
    if (!res.ok) throw new Error("HTTP " + res.status);
    showToast("已改为「" + category + "」", { type: "success", duration: 2500 });
    lastQueryKey = ""; // 分组键 (subject+category) 变化 → 强制全量渲染
    fetchData();
  } catch (err) {
    console.error("Recategorize error:", err);
    showToast("分类修改失败，请重试", { type: "error", duration: 4000 });
  }
}

// ── 批量操作 ──
function enterBatchMode() {
  batchMode = true;
  selectedIds.clear();
  $batchBar.classList.remove("hidden");
  updateBatchToggle();
  updateBatchBar();
  renderItems(viewSourceItems, { full: true });
}

function exitBatchMode() {
  batchMode = false;
  selectedIds.clear();
  $batchBar.classList.add("hidden");
  updateBatchToggle();
  renderItems(viewSourceItems, { full: true });
}

function updateBatchToggle() {
  $batchToggle.classList.toggle("active", batchMode);
}

function toggleSelect(id, card) {
  if (selectedIds.has(id)) selectedIds.delete(id); else selectedIds.add(id);
  const chk = card && card.querySelector(".batch-check input");
  if (chk) chk.checked = selectedIds.has(id);
  if (card) card.classList.toggle("selected", selectedIds.has(id));
  syncBatchGroupStates(); // 平铺模式：成员变化联动组头半选态
  updateBatchBar();
}

// 折叠组/平铺组头 = 组级选择单元：全选 ⇄ 全不选整组成员
function toggleGroupSelect(key, containerEl) {
  const members = groupMap.get(key) || [];
  if (!members.length) return;
  const ids = members.map(m => String(m.id));
  const allSelected = ids.every(id => selectedIds.has(id));
  if (allSelected) ids.forEach(id => selectedIds.delete(id));
  else ids.forEach(id => selectedIds.add(id));
  syncBatchGroupStates();
  updateBatchBar();
}

// 统一组态同步：折叠组整组高亮 + 勾选框全选/半选；平铺组头半选；单卡勾选
function syncBatchGroupStates() {
  if (!batchMode) return;
  $itemsContainer.querySelectorAll(".group-collapsed[data-key]").forEach(el => {
    const members = groupMap.get(el.dataset.key) || [];
    const sel = members.filter(m => selectedIds.has(String(m.id))).length;
    const all = members.length > 0 && sel === members.length;
    const partial = sel > 0 && !all;
    el.classList.toggle("selected", all);
    el.querySelectorAll(".batch-check input").forEach(chk => {
      chk.checked = all;
      chk.indeterminate = partial;
    });
  });
  $itemsContainer.querySelectorAll(".group-header[data-key]").forEach(el => {
    const members = groupMap.get(el.dataset.key) || [];
    const sel = members.filter(m => selectedIds.has(String(m.id))).length;
    const all = members.length > 0 && sel === members.length;
    const partial = sel > 0 && !all;
    const chk = el.querySelector(".batch-check input");
    if (chk) { chk.checked = all; chk.indeterminate = partial; }
    el.classList.toggle("selected", all); // 整组选中 → 组头高亮（与折叠组一致）
  });
  $itemsContainer.querySelectorAll(".item-card[data-id]").forEach(el => {
    if (el.closest(".group-collapsed")) return; // 折叠组内代表卡已按组态处理
    const sel = selectedIds.has(String(el.dataset.id));
    const chk = el.querySelector(".batch-check input");
    if (chk) chk.checked = sel;
    el.classList.toggle("selected", sel); // 整组勾选/半选联动时同步卡片绿框
  });
}

function toggleSelectAll() {
  const all = lastVisibleItems.map(i => String(i.id));
  if (all.length && selectedIds.size === all.length) selectedIds.clear();
  else selectedIds = new Set(all);
  syncBatchGroupStates();
  updateBatchBar();
}

function updateBatchBar() {
  const n = selectedIds.size;
  $batchCount.textContent = "已选 " + n + " 条 / 共 " + lastVisibleItems.length + " 条";
  $batchSelectAll.textContent = (lastVisibleItems.length && n === lastVisibleItems.length)
    ? "取消全选" : "全选当前";
}

function openBatchConfirm() {
  if (!selectedIds.size) { showToast("请先勾选卡片", { type: "info", duration: 3000 }); return; }
  $batchConfirmN.textContent = selectedIds.size;
  $batchConfirmModal.classList.remove("hidden");
  syncBodyScrollLock();
}

function closeBatchConfirm() {
  $batchConfirmModal.classList.add("hidden");
  syncBodyScrollLock();
}

async function batchApply(action) {
  if (!selectedIds.size) { showToast("请先勾选卡片", { type: "info", duration: 3000 }); return; }
  closeBatchConfirm();
  try {
    const res = await postJson("/api/items/batch", { ids: [...selectedIds], action });
    const affected = res.affected || 0;
    showToast("已处理 " + affected + " 条", { type: "success", duration: 2500 });
    selectedIds.clear();
    lastQueryKey = ""; // 强制全量渲染：组结构/计数/勾选状态收敛
    fetchData();
  } catch (err) {
    console.error("Batch error:", err);
    showToast("批量操作失败，请重试", { type: "error", duration: 4000 });
  }
}

// ── 状态详情面板 ──
async function openStatusPanel() {
  $statusPopover.innerHTML = '<p class="text-muted">加载中...</p>';
  $statusPopover.classList.remove("hidden");
  let status = null;
  try {
    const res = await fetch("/api/status");
    if (res.ok) status = await res.json();
  } catch { /* 保持 null */ }
  if (!status) {
    $statusPopover.innerHTML = '<p class="text-muted">状态获取失败</p>';
    return;
  }
  const sources = Object.entries(status.sources || {});
  const rows = sources.map(([name, s]) => {
    const st = s.status || "offline";
    const icon = _STATUS_ICONS[st] || _STATUS_ICONS.offline;
    return '<div class="status-row"><img src="' + icon + '" class="icon-sm" alt="">' + esc(name) +
      ' <span class="status ' + st + '">' + (_STATUS_LABELS[st] || st) + '</span></div>';
  }).join("");
  const lastAbs = status.lastSync ? new Date(status.lastSync).toLocaleString("zh-CN") : "";
  const warnHtml = status.lastWarning
    ? '<div class="status-warning">' + esc(status.lastWarning) + '</div>'
    : "";
  const errHtml = status.lastError
    ? '<div class="status-error">' + esc(status.lastError) + '</div>'
    : "";
  $statusPopover.innerHTML =
    '<div class="status-popover-head"><span>运行状态</span><button id="status-popover-close" title="关闭 (Esc)">×</button></div>' +
    (rows || '<div class="text-muted">未连接消息源</div>') +
    '<div class="status-line">上次同步：' + esc(syncRelativeText(status.lastSync)) + (lastAbs ? '（' + esc(lastAbs) + '）' : '') + '</div>' +
    warnHtml +
    errHtml;
  document.getElementById("status-popover-close").addEventListener("click", closeStatusPanel);
}

function closeStatusPanel() {
  $statusPopover.classList.add("hidden");
}

// ── 分页加载更多 ──
function updateLoadMore() {
  if (!$loadMoreWrap) return;
  const show = hasMore && !batchMode;
  if (!show) { $loadMoreWrap.innerHTML = ""; return; }
  const busy = pageLoading || listRefreshing;
  $loadMoreWrap.innerHTML = '<button id="load-more-btn" class="load-more-btn" type="button"'
    + (busy ? " disabled" : "") + '>' + (busy ? "加载中..." : "加载更多") + '</button>';
  document.getElementById("load-more-btn").addEventListener("click", loadMore);
}

async function loadMore() {
  if (pageLoading || listRefreshing || !hasMore || !activeItemQuery) return;
  const seq = fetchSeq;
  const query = activeItemQuery;
  const offset = nextOffset;
  pageLoading = true;
  updateLoadMore();
  try {
    const data = await requestItemPage(query, offset);
    if (seq !== fetchSeq || query.key !== itemQueryKeyNow()) return;

    applySidebarData(data);
    hasMore = !!data.hasMore;
    nextOffset = Number.isInteger(data.nextOffset)
      ? data.nextOffset
      : offset + (data.items || []).length;
    loadedPageCount += 1;

    const combined = [];
    const seen = new Set();
    appendUniqueItems(combined, viewSourceItems, seen);
    appendUniqueItems(combined, data.items, seen);
    viewSourceItems = combined;
    searchGroups = currentSearch ? (data.sourceGroups || searchGroups) : [];
    renderItems(combined);
    renderFilterBar();
  } catch (err) {
    console.error("Load more error:", err);
    showToast("加载更多失败，请重试", { type: "error", duration: 4000 });
  } finally {
    pageLoading = false;
    updateLoadMore();
  }
}
// ── 时效徽章与时间工具 ──
function fmtDate(d) {
  // 本地日期 → "YYYY-MM-DD"（date-only 比较/查询参数通用）
  return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-"
    + String(d.getDate()).padStart(2, "0");
}

function parseLocalTime(s) {
  if (!s || typeof s !== "string") return null;
  const m = s.match(/^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2}))?$/);
  if (!m) return null;
  // date-only 按当日 00:00 解析（过期/徽章逻辑另行区分有无时刻）
  return new Date(+m[1], +m[2] - 1, +m[3], m[4] ? +m[4] : 0, m[5] ? +m[5] : 0);
}

function isDateOnly(s) {
  return typeof s === "string" && /^\d{4}-\d{2}-\d{2}$/.test(s.trim());
}

function timeExpired(t) {
  // 单个时间点是否已过期（date-only 按日比较：截止当天不过期）
  if (!t) return false;
  const dateOnly = isDateOnly(t);
  const d = parseLocalTime(t);
  if (!d) return false;
  return dateOnly ? t < fmtDate(new Date()) : d.getTime() < Date.now();
}

function hhmm(d) {
  return String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
}

function fmtMonthDay(d) {
  return (d.getMonth() + 1) + "月" + d.getDate() + "日";
}

function toLocalInput(d) {
  return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-"
    + String(d.getDate()).padStart(2, "0") + "T" + hhmm(d);
}

// 时效徽章：end 优先，其次 start；返回 {cls, label, expired, time} 或 null
// date-only（YYYY-MM-DD）：过期按日期比较（截止当天不过期），徽章不带时刻
function allTimePoints(item) {
  // 主字段 + extra_times 全部时间点 [{time, type}]，按时间升序
  const pts = [];
  const push = (t, type) => { if (typeof t === "string" && t) pts.push({ time: t, type }); };
  push(item.start, "start");
  push(item.end, "end");
  for (const e of parseExtraTimes(item)) push(e.time, e.type);
  pts.sort((a, b) => a.time.localeCompare(b.time));
  return pts;
}

function nextUpcomingTime(item, { needTime = false } = {}) {
  // 最早的未过期时间点；needTime=true 时要求带时刻（HH:MM，供提醒用）
  for (const p of allTimePoints(item)) {
    if (!parseLocalTime(p.time)) continue;
    if (timeExpired(p.time)) continue;
    if (needTime && !p.time.slice(11, 16)) continue;
    return p.time;
  }
  return "";
}

function timeBadgeInfo(item) {
  const now = new Date();
  const todayStr = fmtDate(now);
  const dlRaw = typeof item.end === "string" ? item.end.trim() : "";
  const dlDateOnly = isDateOnly(dlRaw);
  const dl = parseLocalTime(dlRaw);
  if (dl) {
    const expired = dlDateOnly ? dlRaw < todayStr : dl.getTime() < now.getTime();
    const diff = dl.getTime() - now.getTime();
    if (expired) {
      // 部分截止：主截止已过但卡片仍有未过的时间点（后续任务/场次），
      // 不算整卡过期（不置灰、不被「隐藏已截止」过滤）
      if (nextUpcomingTime(item)) return { cls: "partial", label: "部分截止", expired: false, time: dl };
      return { cls: "expired", label: "已截止", expired: true, time: dl };
    }
    if (dlDateOnly) {
      if (dlRaw === todayStr) return { cls: "urgent", label: "今天截止", expired: false, time: dl };
      const days = Math.ceil(diff / 86400000);
      if (days <= 3) return { cls: "soon", label: days + " 天后截止", expired: false, time: dl };
      return { cls: "later", label: "截止 " + fmtMonthDay(dl), expired: false, time: dl };
    }
    const minutes = Math.floor(diff / 60000);
    if (minutes < 60) return { cls: "urgent", label: (minutes <= 0 ? "即将截止" : Math.max(minutes, 1) + " 分钟后截止"), expired: false, time: dl };
    if (minutes < 1440) return { cls: "urgent", label: Math.floor(minutes / 60) + " 小时后截止", expired: false, time: dl };
    if (minutes < 4320) return { cls: "soon", label: Math.floor(minutes / 1440) + " 天后截止", expired: false, time: dl };
    return { cls: "later", label: "截止 " + fmtMonthDay(dl), expired: false, time: dl };
  }
  const stRaw = typeof item.start === "string" ? item.start.trim() : "";
  const stDateOnly = isDateOnly(stRaw);
  const st = parseLocalTime(stRaw);
  if (st) {
    const diff = st.getTime() - now.getTime();
    if (stDateOnly) {
      if (stRaw < todayStr) return { cls: "started", label: "已开始", expired: false, time: st };
      if (stRaw === todayStr) return { cls: "today", label: "今天", expired: false, time: st };
      return { cls: "later", label: fmtMonthDay(st), expired: false, time: st };
    }
    if (diff < 0) return { cls: "started", label: "已开始", expired: false, time: st };
    if (st.toDateString() === now.toDateString()) return { cls: "today", label: "今天 " + hhmm(st), expired: false, time: st };
    return { cls: "later", label: fmtMonthDay(st) + " " + hhmm(st), expired: false, time: st };
  }
  return null;
}

function parseExtraTimes(item) {
  // extra_times JSON → [{type, time, label}]；脏数据项丢弃
  if (typeof item.extra_times !== "string" || !item.extra_times) return [];
  try {
    const list = JSON.parse(item.extra_times);
    if (!Array.isArray(list)) return [];
    return list
      .filter(e => e && (e.type === "start" || e.type === "end")
        && typeof e.time === "string" && e.time)
      .map(e => ({ type: e.type, time: e.time, label: typeof e.label === "string" ? e.label : "" }));
  } catch { return []; }
}

function extraTimesHtml(item) {
  // 多时间点徽章：主字段之外的每个时间点各一枚（截止 8月15日·部门宣传视频）
  const list = parseExtraTimes(item);
  if (!list.length) return "";
  const now = new Date();
  const todayStr = fmtDate(now);
  return list.map(e => {
    const d = parseLocalTime(e.time);
    if (!d) return "";
    const dateOnly = isDateOnly(e.time);
    const expired = dateOnly ? e.time < todayStr : d.getTime() < now.getTime();
    const prefix = e.type === "end" ? "截止 " : "开始 ";
    let dayLabel = fmtMonthDay(d);
    if (!dateOnly) dayLabel += " " + hhmm(d);
    const text = prefix + dayLabel + (e.label ? "·" + e.label : "");
    return '<span class="time-badge time-extra ' + (expired ? "expired" : "later")
      + '" title="' + escAttr(text) + '">' + esc(text) + "</span>";
  }).join("");
}

function timeBadgeHtml(item) {
  const b = timeBadgeInfo(item);
  const primary = b ? '<span class="time-badge ' + b.cls + '" title="' + escAttr(b.label) + '">' + esc(b.label) + "</span>" : "";
  return primary + extraTimesHtml(item);
}

// ── 主体时间线 ──
async function openSubjectTimeline(subject, { syncHash: sh = true } = {}) {
  if (!subject) return;
  lastModalFocus = document.activeElement;
  timeline = { subject: subject, items: [], offset: 0, hasMore: false, count: 0, expandedIds: new Set() };
  $timelineTitle.textContent = subject;
  $timelineList.innerHTML = '<p class=\'text-muted\'>加载中...</p>';
  $timelineLoadMoreWrap.innerHTML = "";
  $timelineModal.classList.remove("hidden");
  syncBodyScrollLock();
  if (sh) syncHash("push");
  await loadTimelinePage();
}

function closeSubjectTimeline({ syncHash: sh = true } = {}) {
  if (!timeline) return;
  timeline = null;
  $timelineModal.classList.add("hidden");
  syncBodyScrollLock();
  if (lastModalFocus && document.contains(lastModalFocus)) lastModalFocus.focus();
  lastModalFocus = null;
  if (sh) { syncHash("push"); fetchData(); } // 时间线内可能做过处理，收敛主列表
}

async function loadTimelinePage() {
  if (!timeline) return;
  try {
    const res = await fetch("/api/subject/items?subject=" + encodeURIComponent(timeline.subject) + "&limit=50&offset=" + timeline.offset);
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = await res.json();
    if (!timeline) return;
    const items = data.items || [];
    timeline.items = timeline.offset === 0 ? items : timeline.items.concat(items);
    timeline.offset += items.length;
    timeline.count = data.count;
    timeline.hasMore = data.hasMore;
    renderTimelineList();
  } catch (err) {
    console.error("Timeline error:", err);
    $timelineList.innerHTML = '<p class=\'text-muted\'>加载失败</p>';
  }
}

function renderTimelineList() {
  if (!timeline) return;
  if (!timeline.items.length) {
    $timelineList.innerHTML = '<div class=\'ov-empty\'>该主体下暂无未忽略的信息</div>';
    $timelineLoadMoreWrap.innerHTML = "";
    return;
  }
  let html = '<div class="tl-meta">共 ' + timeline.count + " 条记录</div>";
  let lastLabel = "";
  for (const item of timeline.items) {
    const label = dayLabel(itemTime(item));
    if (label && label !== lastLabel) {
      html += '<div class="day-divider"><span>' + esc(label) + "</span></div>";
      lastLabel = label;
    }
    html += renderItemRow(item, { cls: "tl-row", showArticleLink: false });
  }
  $timelineList.innerHTML = html;
  // 恢复展开态
  for (const id of timeline.expandedIds) {
    const row = $timelineList.querySelector('.tl-row[data-id="' + CSS.escape(id) + '"]');
    if (!row) continue;
    const detail = row.querySelector(".ov-detail");
    if (detail) detail.classList.remove("hidden");
  }
  if (timeline.hasMore) {
    $timelineLoadMoreWrap.innerHTML = '<button id="timeline-more-btn" class="load-more-btn" type="button">加载更多</button>';
    document.getElementById("timeline-more-btn").addEventListener("click", loadTimelinePage);
  } else {
    $timelineLoadMoreWrap.innerHTML = "";
  }
}

async function timelineVerify(id, value, row) {
  const prev = row.classList.contains("memo") ? 1 : row.classList.contains("ignored") ? -1 : 0;
  if (prev === value) return;
  try {
    const res = await fetch("/api/items/" + encodeURIComponent(id) + "/verify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ verified: value }),
    });
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = await res.json();
    renderNav(data.categories, data.ignoredCount, data.memoCount);
    updateActiveNav();
    if (value === -1) {
      row.style.transition = "opacity 0.25s";
      row.style.opacity = "0";
      setTimeout(() => {
        row.remove();
        if (timeline) timeline.items = timeline.items.filter(i => String(i.id) !== id);
        if (!$timelineList.querySelector(".tl-row")) renderTimelineList();
      }, 250);
    } else {
      row.classList.toggle("memo", value === 1);
      row.classList.remove("ignored");
      const bm = row.querySelector(".btn-memo");
      const bi = row.querySelector(".btn-ignore");
      if (bm) { bm.classList.toggle("active", value === 1); bm.textContent = value === 1 ? "移出备忘录" : "加入备忘录"; }
      if (bi) { bi.classList.toggle("active", false); bi.textContent = "忽略"; }
    }
  } catch (err) {
    console.error("Timeline verify error:", err);
    showToast("操作失败，请重试", { type: "error", duration: 4000 });
  }
}

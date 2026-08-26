/* rag 插件前端：「问一问」视图的完整前端随插件包分发。
 *
 * 核心只提供通用加载器与视图钩子（app.js 的 registerPluginView）：
 * - 本文件注入后自行创建入口按钮 / 视图容器 / 上下文浮层（DOM 不入核心 index.html）；
 * - 提问走 POST /api/rag/ask；答案渲染引用芯片，点击复用核心 fetchContext 打开原文；
 * - 复用的核心全局工具：esc / registerPluginView / parseHash / syncHash /
 *   batchMode / exitBatchMode / inlineSvgIcons / fetchContext。
 */
(function () {
  "use strict";
  const PLUGIN = "rag";
  const VIEW = "ask"; // hash 字面量视图 id（核心 parseHash 按插件 matches 识别）

  // ── 状态 ──
  let askMode = false;   // 视图开关
  let asking = false;    // 请求互斥

  // ── 元素（init 时创建）──
  let $ragBtn = null;
  let $ragView = null;
  let $ragInput = null;
  let $ragAskBtn = null;
  let $ragResult = null;
  let $ctxModal = null;

  function buildDom() {
    $ragBtn = document.createElement("button");
    $ragBtn.id = "rag-btn";
    $ragBtn.className = "sync-btn";
    $ragBtn.title = "问一问（向群聊记录提问，带原文引用）";
    $ragBtn.innerHTML = '<img src="/icons/search.svg" class="icon" alt="">问一问';
    const $calBtn = document.getElementById("calendar-btn");
    if ($calBtn) $calBtn.parentNode.insertBefore($ragBtn, $calBtn);
    else {
      const $syncBtn = document.getElementById("sync-btn");
      if ($syncBtn) $syncBtn.parentNode.insertBefore($ragBtn, $syncBtn);
    }

    $ragView = document.createElement("div");
    $ragView.id = "rag-view";
    $ragView.classList.add("hidden");
    $ragView.innerHTML =
      '<div class="rag-panel">'
      + '<h2 class="rag-title">问一问</h2>'
      + '<p class="rag-hint">向已启用的群聊记录提问（如「那个活动什么时候截止」），回答附原始消息引用。</p>'
      + '<div class="rag-input-row">'
      + '<input id="rag-input" type="text" maxlength="500" placeholder="例如：xx活动的报名截止时间是什么时候？"/>'
      + '<button id="rag-ask" class="sync-btn">提问</button>'
      + "</div>"
      + '<div id="rag-result" class="rag-result"></div>'
      + "</div>";
    const $main = document.querySelector("main");
    const $loadMoreWrap = document.getElementById("load-more-wrap");
    if ($loadMoreWrap) $loadMoreWrap.after($ragView);
    else if ($main) $main.appendChild($ragView);

    $ctxModal = document.createElement("div");
    $ctxModal.id = "rag-ctx-modal";
    $ctxModal.className = "modal hidden";
    $ctxModal.setAttribute("role", "dialog");
    $ctxModal.setAttribute("aria-label", "引用原文上下文");
    $ctxModal.innerHTML =
      '<div class="modal-content detail-content">'
      + '<div class="group-overlay-head">'
      + '<h2 id="rag-ctx-title">引用上下文</h2>'
      + '<button id="rag-ctx-close" title="关闭 (Esc)">×</button>'
      + "</div>"
      + '<div id="rag-ctx-body"></div>'
      + "</div>";
    document.body.appendChild($ctxModal);
  }

  function bindEvents() {
    $ragBtn.addEventListener("click", () => {
      if (askMode) exitAskMode();
      else enterAskMode();
    });
    $ragAskBtn.addEventListener("click", submitQuestion);
    $ragInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") submitQuestion();
    });
    document.getElementById("rag-ctx-close").addEventListener("click", hideCtxModal);
    $ctxModal.addEventListener("click", (e) => {
      if (e.target === $ctxModal) hideCtxModal();
    });
  }

  // ── 视图进出 ──
  function enterAskMode() {
    askMode = true;
    if (typeof batchMode !== "undefined" && batchMode) exitBatchMode();
    document.body.classList.add("rag-mode");
    $ragBtn.classList.add("active");
    $ragView.classList.remove("hidden");
    syncHash("push");
    $ragInput.focus();
  }

  function exitAskMode() {
    askMode = false;
    document.body.classList.remove("rag-mode");
    $ragBtn.classList.remove("active");
    $ragView.classList.add("hidden");
    hideCtxModal();
    syncHash("push");
  }

  // ── 提问与渲染 ──
  async function submitQuestion() {
    if (asking) return;
    const question = ($ragInput.value || "").trim();
    if (question.length < 2) return;
    asking = true;
    $ragAskBtn.disabled = true;
    $ragResult.innerHTML = '<p class="text-muted rag-thinking">检索并生成中…</p>';
    try {
      const res = await fetch("/api/rag/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question }),
      });
      if (!res.ok) throw new Error("HTTP " + res.status);
      const data = await res.json();
      renderAnswer(data);
    } catch {
      $ragResult.innerHTML =
        '<p class="rag-error">问答服务暂时不可用，请稍后再试。</p>';
    } finally {
      asking = false;
      $ragAskBtn.disabled = false;
    }
  }

  function renderAnswer(data) {
    if (data.refused) {
      $ragResult.innerHTML =
        '<p class="rag-refused">' + esc(data.answer || "没有找到相关消息。") + "</p>";
      return;
    }
    const chips = (data.citations || [])
      .map((c) =>
        '<button class="rag-cite-chip" data-n="' + escAttr(String(c.n)) + '" title="'
        + escAttr(c.group_name + "·" + c.sender_name) + '">[' + c.n + "] "
        + escAttr(c.sender_name) + "</button>")
      .join("");
    $ragResult.innerHTML =
      '<div class="rag-answer"><pre class="rag-answer-text">' + esc(data.answer) + "</pre></div>"
      + '<div class="rag-cites"><span class="text-muted">引用：</span>' + chips + "</div>";
    $ragResult.querySelectorAll(".rag-cite-chip").forEach((chip) => {
      chip.addEventListener("click", () => {
        const n = Number(chip.dataset.n);
        const cite = (data.citations || []).find((c) => c.n === n);
        if (cite) openCtx(cite);
      });
    });
  }

  // ── 引用 → 原文上下文浮层（复用核心 fetchContext）──
  function openCtx(cite) {
    document.getElementById("rag-ctx-title").textContent =
      (cite.group_name ? cite.group_name + " · " : "") + cite.sender_name;
    const body = document.getElementById("rag-ctx-body");
    body.innerHTML = '<p class="text-muted">加载中…</p>';
    $ctxModal.classList.remove("hidden");
    fetchContext(body, cite.source, cite.session_id, cite.time, cite.msg_id);
  }

  function hideCtxModal() {
    $ctxModal.classList.add("hidden");
  }

  // ── 核心视图钩子：hash 路由 / fetchData / Esc 联动 ──
  function registerViewHook() {
    registerPluginView({
      name: PLUGIN,
      matches: (v) => !!v && v.view === VIEW,
      hash: (v) => {
        if (v && v.view === VIEW) enterAskMode();
        else exitAskMode();
      },
      isActive: () => askMode,
      refresh: () => {},
      onEsc: () => {
        if (!$ctxModal.classList.contains("hidden")) { hideCtxModal(); return true; }
        if (askMode) { exitAskMode(); return true; }
        return false;
      },
      buildHash: () => (askMode ? "#ask" : null),
      sidebarReady: () => {},
    });
  }

  // ── 入口：核心加载器注入本脚本后调用 ──
  function init(api) {
    if (!api || typeof api.isLoaded !== "function" || !api.isLoaded(PLUGIN)) return;
    buildDom();
    bindEvents();
    registerViewHook();
    inlineSvgIcons(); // 内联按钮图标（与核心图标一致）
    // F5 刷新 #ask：加载器注入晚于核心 hash 初始化，此处自查补进入
    const v = parseHash();
    if (v && v.view === VIEW) enterAskMode();
  }

  window.briefdeskPlugins = window.briefdeskPlugins || {};
  window.briefdeskPlugins.rag = { init: init };
})();

/* rag 插件前端：右侧「问一问」聊天侧边栏。
 *
 * 架构决定：不注册插件视图（registerPluginView），不触碰头部按钮区——
 * 日历按钮/视图系统保持原样；本面板是纯状态抽屉：侧边栏 nav-special 入口
 * 点击开合，Esc 关闭，会话历史保存在内存中。
 * 复用核心全局：esc / escAttr / fetchContext / inlineSvgIcons。
 */
(function () {
  "use strict";
  const PLUGIN = "rag";
  const ICON = "/icons/search.svg";

  let $navLink = null;
  let $drawer = null;
  let $msgs = null;
  let $input = null;
  let $sendBtn = null;
  let $clearBtn = null;
  let $ctxModal = null;

  let open = false;        // 抽屉开关
  let asking = false;      // 请求互斥
  const history = [];      // 会话内对话历史 [{role, content}]

  function buildDom() {
    // ── 侧边栏入口（工具类固定排在视图类之后，nav-special 底部）──
    $navLink = document.createElement("a");
    $navLink.id = "rag-nav-link";
    $navLink.href = "#";
    $navLink.className = "cat-link";
    $navLink.title = "问一问（向群聊记录提问，带原文引用）";
    $navLink.innerHTML =
      '<span class="cat-link-main"><img src="' + ICON + '" class="icon-sm cat-icon" alt="">问一问</span>';
    const $ignored = document.getElementById("ignored-link");
    if ($ignored && $ignored.parentNode) {
      $ignored.parentNode.insertBefore($navLink, $ignored.nextSibling);
    } else {
      const $aside = document.querySelector("aside.sidebar");
      if ($aside) $aside.appendChild($navLink);
    }

    // ── 右侧聊天抽屉 ──
    $drawer = document.createElement("aside");
    $drawer.id = "rag-drawer";
    $drawer.setAttribute("role", "complementary");
    $drawer.setAttribute("aria-label", "问一问聊天");
    $drawer.classList.add("hidden");
    $drawer.innerHTML =
      '<div class="rag-drawer-head">'
      + '<h2 class="rag-drawer-title">问一问</h2>'
      + '<button id="rag-clear" class="rag-drawer-btn" title="清空对话">清空</button>'
      + '<button id="rag-close" class="rag-drawer-btn rag-drawer-x" title="关闭 (Esc)">×</button>'
      + "</div>"
      + '<div id="rag-msgs" class="rag-msgs"></div>'
      + '<div class="rag-input-row">'
      + '<input id="rag-input" type="text" maxlength="500" '
      + 'placeholder="例如：那个活动的报名截止时间是什么时候？" autocomplete="off"/>'
      + '<button id="rag-send" type="button">发送</button>'
      + "</div>";
    document.body.appendChild($drawer);

    // ── 引用原文上下文浮层（复用核心 fetchContext）──
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

    $msgs = document.getElementById("rag-msgs");
    $input = document.getElementById("rag-input");
    $sendBtn = document.getElementById("rag-send");
    $clearBtn = document.getElementById("rag-clear");
  }

  function bindEvents() {
    $navLink.addEventListener("click", (e) => {
      e.preventDefault();
      toggleDrawer();
    });
    $sendBtn.addEventListener("click", submitQuestion);
    $input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") submitQuestion();
    });
    document.getElementById("rag-clear").addEventListener("click", clearChat);
    document.getElementById("rag-close").addEventListener("click", closeDrawer);
    document.getElementById("rag-ctx-close").addEventListener("click", hideCtxModal);
    $ctxModal.addEventListener("click", (e) => {
      if (e.target === $ctxModal) hideCtxModal();
    });
    $drawer.addEventListener("click", (e) => {
      if (e.target === $drawer) return;
    });
    document.addEventListener("keydown", (e) => {
      if (e.key !== "Escape") return;
      if (!$ctxModal.classList.contains("hidden")) { hideCtxModal(); return; }
      if (open) closeDrawer();
    });
  }

  // ── 抽屉开合 ──
  function toggleDrawer() {
    if (open) closeDrawer();
    else openDrawer();
  }
  function openDrawer() {
    open = true;
    $navLink.classList.add("active");
    $drawer.classList.remove("hidden");
    $input.focus();
  }
  function closeDrawer() {
    open = false;
    $navLink.classList.remove("active");
    $drawer.classList.add("hidden");
  }

  function clearChat() {
    history.length = 0;
    $msgs.innerHTML = "";
    addMsg("assistant", "已清空对话。可以直接开始提问了。");
  }

  function addMsg(role, content, extra) {
    const wrap = document.createElement("div");
    wrap.className = "rag-msg rag-msg-" + (role === "user" ? "user" : "assistant");
    const bubble = document.createElement("div");
    bubble.className = "rag-bubble";
    bubble.textContent = content;
    wrap.appendChild(bubble);
    if (extra && extra.citations && extra.citations.length) {
      const chips = document.createElement("div");
      chips.className = "rag-cites";
      extra.citations.forEach((c) => {
        const chip = document.createElement("button");
        chip.type = "button";
        chip.className = "rag-cite-chip";
        chip.dataset.n = String(c.n);
        chip.title = escAttr((c.group_name ? c.group_name + " · " : "") + c.sender_name);
        chip.textContent = "[" + c.n + "] " + c.sender_name;
        chip.addEventListener("click", () => openCtx(c));
        chips.appendChild(chip);
      });
      wrap.appendChild(chips);
    }
    $msgs.appendChild(wrap);
    $msgs.scrollTop = $msgs.scrollHeight;
  }

  async function submitQuestion() {
    if (asking) return;
    const question = ($input.value || "").trim();
    if (question.length < 2) return;
    asking = true;
    $sendBtn.disabled = true;
    addMsg("user", question);
    const turnHistory = history.slice(-20);
    history.push({ role: "user", content: question });
    const thinking = document.createElement("div");
    thinking.className = "rag-msg rag-msg-assistant rag-thinking";
    thinking.textContent = "检索并生成中…";
    $msgs.appendChild(thinking);
    $msgs.scrollTop = $msgs.scrollHeight;
    try {
      const res = await fetch("/api/rag/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question, history: turnHistory }),
      });
      if (!res.ok) throw new Error("HTTP " + res.status);
      const data = await res.json();
      thinking.remove();
      if (data.refused) {
        addMsg("assistant", data.answer || "没有找到相关消息。");
      } else {
        addMsg("assistant", data.answer, data);
        history.push({ role: "assistant", content: data.answer });
      }
    } catch {
      thinking.remove();
      addMsg("assistant", "问答服务暂时不可用，请稍后再试。");
    } finally {
      asking = false;
      $sendBtn.disabled = false;
      $input.value = "";
      $input.focus();
    }
  }

  // ── 引用 → 原文上下文浮层 ──
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

  // ── 入口：核心加载器注入本脚本后调用 ──
  function init(api) {
    if (!api || typeof api.isLoaded !== "function" || !api.isLoaded(PLUGIN)) return;
    buildDom();
    bindEvents();
    inlineSvgIcons();
    addMsg("assistant", "你好，我是群聊知识助手。问我任何在消息里出现过的问题——我会附上原文引用。");
  }

  window.briefdeskPlugins = window.briefdeskPlugins || {};
  window.briefdeskPlugins.rag = { init: init };
})();

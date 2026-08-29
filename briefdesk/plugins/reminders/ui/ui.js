/* reminders 插件前端：提醒功能的完整前端随插件包分发。
 *
 * 核心只提供通用加载器与行内扩展钩子（app.js 的 registerItemRowExtension）：
 * - 本文件注入后自行构建卡片「提醒」按钮/菜单（挂到核心 renderItemRow 与
 *   renderCard 的动作区）、设置弹窗「通知」面板的自动提醒控件、到期轮询定时器；
 * - 复用的核心全局工具：esc / escAttr / showToast / currentItems / parseLocalTime /
 *   isDateOnly / nextUpcomingTime / toLocalInput / syncBodyScrollLock /
 *   exitPluginViews / $memoLink 等。
 */
(function () {
  "use strict";
  const PLUGIN = "reminders";

  // ── 状态 ──
  let autoRemindOffset = "off";       // 自动提醒：off | 30m | 1h | 1d
  let remindTimer = null;             // 提醒检查定时器
  let notifiedReminders = new Set();  // 本次会话已通知的提醒卡片 id（防重）

  // ── 设置弹窗「通知」面板：自动提醒控件（核心留 data-plugin-slot 注入点）──
  function buildSettingsControl() {
    const slot = document.querySelector('[data-plugin-slot="settings-notify"]');
    if (!slot) return;
    slot.innerHTML =
      '<label>自动提醒（加入备忘录时按卡片时间设置）</label>'
      + '<select id="auto-remind" class="settings-select" aria-label="自动提醒">'
      + '<option value="off">关闭</option>'
      + '<option value="30m">截止/开始前 30 分钟</option>'
      + '<option value="1h">截止/开始前 1 小时</option>'
      + '<option value="1d">截止/开始前 1 天</option>'
      + "</select>"
      + '<p class="text-muted settings-hint">提醒在本应用运行期间生效；关闭期间错过的提醒不补发。</p>';
    const $autoRemind = document.getElementById("auto-remind");
    if ($autoRemind) {
      // 走核心 lsGet/lsSet：此前裸 setItem 在隐私模式下会抛，中断 change 处理函数
      autoRemindOffset = lsGet("briefdesk.autoRemind", "off");
      $autoRemind.value = autoRemindOffset;
      $autoRemind.addEventListener("change", () => {
        autoRemindOffset = $autoRemind.value;
        lsSet("briefdesk.autoRemind", autoRemindOffset);
      });
    }
  }

  // ── 卡片行内「提醒」按钮与菜单 ──
  // datetime-local 要求日期+时刻：date-only 值补默认 09:00
  function remindInputValue(s) {
    if (!s) return "";
    const t = s.trim();
    if (/^\d{4}-\d{2}-\d{2}$/.test(t)) return t + "T09:00";
    return t.replace(" ", "T");
  }

  function renderButton(item) {
    // 原先 .active 只是视觉态，读屏用户无法得知提醒是否已设置；
    // title 也恒为"设置提醒"，已设提醒的卡片上是错的。
    const on = !!item.remind_at;
    return '<button type="button" class="btn-remind' + (on ? " active" : "") +
      '" aria-pressed="' + (on ? "true" : "false") +
      '" title="' + (on ? "修改或清除提醒" : "设置提醒") +
      '"><img src="/icons/alarm-clock.svg" class="icon-sm" alt="">提醒</button>';
  }

  function renderMenu(item) {
    const dv = item.remind_at ? remindInputValue(item.remind_at)
      : remindInputValue(nextUpcomingTime(item) || item.end || item.start || "");
    return [
      '<div class="card-remind-menu hidden">',
      '<div class="remind-menu-head" id="remind-head-' + item.id + '">提醒时间' + (item.remind_at ? "（已设）" : "") + "</div>",
      // 原先该输入框没有任何可访问名称，读屏只报控件类型
      '<input type="datetime-local" class="remind-input" aria-label="提醒时间"' +
        ' aria-describedby="remind-head-' + item.id + '" value="' + escAttr(dv) + '">',
      '<div class="remind-menu-actions">',
      '<button class="remind-save">保存</button>',
      item.remind_at ? '<button class="remind-clear">清除提醒</button>' : "",
      '<button class="remind-cancel">取消</button>',
      "</div>",
      "</div>",
    ].join("");
  }

  async function setReminderApi(id, atOrNull, { silent = false } = {}) {
    try {
      // 首次设提醒前同步申请桌面通知权限（页面后台时的到期提醒依赖它）：
      // Firefox 要求权限弹窗绑定 user activation，放到 await fetch 之后
      // 激活态可能已被消费而静默不弹（审查 A4）。仅 default 态请求；点击
      // 菜单已是强意图信号，设置失败多弹一次可接受。拒绝/失败静默降级——
      // 前台 toast 提醒始终可用。
      if ("Notification" in window && Notification.permission === "default") {
        try { Notification.requestPermission().catch(() => { }); } catch { /* 忽略 */ }
      }
      const res = await fetch("/api/items/" + encodeURIComponent(id) + "/reminder", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ at: atOrNull }),
      });
      if (!res.ok) throw new Error("HTTP " + res.status);
      const data = await res.json();
      const it = currentItems.find(x => String(x.id) === id);
      if (it) it.remind_at = data.remind_at || null;
      document.querySelectorAll(".card-remind-menu").forEach(m => m.classList.add("hidden"));
      document.querySelectorAll('[data-id="' + CSS.escape(id) + '"] .btn-remind').forEach(b => {
        b.classList.toggle("active", !!data.remind_at);
        b.setAttribute("aria-pressed", data.remind_at ? "true" : "false");
        b.title = data.remind_at ? "修改或清除提醒" : "设置提醒";
      });
      if (!silent) {
        showToast(data.remind_at ? "提醒已设置" : "提醒已清除", { type: "success", duration: 2000 });
      }
      return true;
    } catch (err) {
      console.error("Reminder error:", err);
      if (!silent) showToast("提醒设置失败，请重试", { type: "error", duration: 4000 });
      return false;
    }
  }

  // 行内动作处理（经核心 handleRowAction 委派；返回 true = 已消费）
  function handle(e, ctx) {
    const btn = e.target.closest("button");
    if (!btn) return false;
    const row = ctx.rowOf(btn);
    const id = row ? row.dataset.id : "";
    if (btn.classList.contains("btn-remind")) {
      if (!row) return true;
      const menu = row.querySelector(".card-remind-menu");
      if (!menu) return true;
      const wasHidden = menu.classList.contains("hidden");
      document.querySelectorAll(".card-remind-menu").forEach(m => m.classList.add("hidden"));
      document.querySelectorAll(".card-recat-menu").forEach(m => m.classList.add("hidden"));
      menu.classList.toggle("hidden", !wasHidden);
      return true;
    }
    if (btn.classList.contains("remind-save")) {
      const menu = btn.closest(".card-remind-menu");
      const input = menu && menu.querySelector(".remind-input");
      if (!row || !input || !input.value) {
        showToast("请选择提醒时间", { type: "error", duration: 3000 });
        return true;
      }
      setReminderApi(id, input.value);
      return true;
    }
    if (btn.classList.contains("remind-clear")) {
      if (!row) return true;
      setReminderApi(id, null);
      return true;
    }
    if (btn.classList.contains("remind-cancel")) {
      const menu = btn.closest(".card-remind-menu");
      if (menu) menu.classList.add("hidden");
      return true;
    }
    return false;
  }

  // 点击其它区域关闭菜单（核心文档级点击委托；按钮/菜单内部不关）
  function closeMenus(e) {
    if (!e.target.closest(".btn-remind") && !e.target.closest(".card-remind-menu")) {
      document.querySelectorAll(".card-remind-menu").forEach(m => m.classList.add("hidden"));
    }
  }

  // Esc 关闭提醒菜单。核心的 Esc 栈只覆盖自身浮层，不认识插件行内菜单：
  // 此前菜单打开时按 Esc 会穿透到栈末尾（清空搜索框），菜单本身留在原地。
  // 用捕获阶段抢在核心 document 监听之前处理，并在消费掉时阻止继续传播。
  function onEscCapture(e) {
    if (e.key !== "Escape") return;
    const openMenus = document.querySelectorAll(".card-remind-menu:not(.hidden)");
    if (!openMenus.length) return;
    const trigger = openMenus[0].closest("[data-id]");
    openMenus.forEach(m => m.classList.add("hidden"));
    e.stopPropagation();
    e.preventDefault();
    // 焦点回到触发按钮，避免菜单隐藏后焦点悬在不可见元素上
    const btn = trigger && trigger.querySelector(".btn-remind");
    if (btn && typeof btn.focus === "function") btn.focus();
  }

  // 自动提醒：加入备忘录且开启自动提醒、卡片带具体时刻且尚未设提醒
  async function maybeAutoRemind(id, opts) {
    if (autoRemindOffset === "off") return;
    const it = currentItems.find(x => String(x.id) === id);
    // 部分截止卡片：主截止已过时改用下一个未过且带时刻的时间点作提醒基准
    const base = it && (nextUpcomingTime(it, { needTime: true }) || it.end || it.start);
    // A：仅日期无具体时刻 → 不自动提醒（卡片仍进日历，用户可手动设置提醒）
    // B：时刻已过去 → 不自动提醒（避免回填老活动立即触发过期提醒）
    if (it && base && !it.remind_at && !isDateOnly(base)) {
      const baseAt = parseLocalTime(base);
      const offsets = { "30m": 30 * 60000, "1h": 3600000, "1d": 86400000 };
      if (baseAt && baseAt.getTime() > Date.now()) {
        const at = new Date(baseAt.getTime() - (offsets[autoRemindOffset] || 0));
        if (!isNaN(at.getTime())) {
          const ok = await setReminderApi(id, toLocalInput(at), { silent: true });
          if (ok) showToast("已自动设置提醒", { type: "info", duration: 3000 });
        }
      }
    }
  }

  // 到期提醒「查看」跳转：备忘录卡 → 备忘录视图；其余卡（未处理等，提醒
  // 可设在任意卡片上）→ 主列表全部视图后定位并高亮该卡片
  function locateDueItem(it) {
    exitPluginViews();
    if (it.is_verified === 1) {
      $memoLink.click();
      return;
    }
    const navAll = document.querySelector('.cat-link[data-category="全部"][data-verified="unverified"]');
    if (navAll) navAll.click();
    setTimeout(() => {
      const card = document.querySelector('.item-card[data-id="' + CSS.escape(String(it.id)) + '"]');
      if (!card) {
        showToast("该卡片不在当前列表首页，可在搜索框输入标题查找", { type: "info", duration: 5000 });
        return;
      }
      card.scrollIntoView({ block: "center", behavior: "smooth" });
      card.classList.add("card-new");
      setTimeout(() => card.classList.remove("card-new"), 4000);
    }, 600);
  }

  // ── 到期轮询：与列表加载状态解耦（任意分类/页面的提醒都能触发）──
  function startReminderTimer() {
    if (remindTimer) clearInterval(remindTimer);
    remindTimer = setInterval(checkDueReminders, 30000);
    setTimeout(checkDueReminders, 10000); // 首屏加载后先查一轮
  }

  async function checkDueReminders() {
    let due = [];
    try {
      const res = await fetch("/api/reminders/due");
      if (!res.ok) return;
      due = (await res.json()).items || [];
    } catch { /* 忽略 */ }
    const now = new Date();
    for (const it of due) {
      if (notifiedReminders.has(String(it.id))) continue;
      const at = parseLocalTime(it.remind_at);
      if (!at || at.getTime() > now.getTime()) continue;
      // 先清后通知：多标签页同时到点时，只有抢到清除权的那个负责通知
      try {
        const res = await fetch("/api/items/" + encodeURIComponent(it.id) + "/reminder", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ at: null }),
        });
        if (!res.ok) continue;
        notifiedReminders.add(String(it.id));
        const cur = currentItems.find(x => String(x.id) === it.id);
        if (cur) cur.remind_at = null;
        const body = it.category ? it.category + " · " + (it.title || "") : (it.title || "");
        if (document.hidden) {
          if ("Notification" in window && Notification.permission === "granted") {
            try {
              const n = new Notification("提醒：" + (it.title || "校园信息"), { body: body.slice(0, 120), tag: "briefdesk-reminder-" + it.id });
              n.onclick = () => { window.focus(); n.close(); };
            } catch { /* 忽略 */ }
          }
        } else {
          showToast("提醒：" + (it.title || "校园信息"), {
            type: "info",
            duration: 8000,
            actionLabel: "查看",
            actionFn: () => locateDueItem(it),
          });
        }
        document.querySelectorAll('[data-id="' + CSS.escape(String(it.id)) + '"] .btn-remind').forEach(b => {
          b.classList.toggle("active", false);
        });
      } catch { /* 忽略 */ }
    }
  }

  // ── 入口：核心加载器注入本脚本后调用 ──
  function init(api) {
    if (!api || typeof api.isLoaded !== "function" || !api.isLoaded(PLUGIN)) return;
    buildSettingsControl();
    registerItemRowExtension({
      name: PLUGIN,
      renderButton: renderButton,
      renderMenu: renderMenu,
      handle: handle,
      closeMenus: closeMenus,
      onVerify: (id, value, opts) => {
        // 核心 verifyItem 成功后委派：加入备忘录（主列表）时尝试自动提醒
        if (value === 1 && !(opts && opts.overlay)) maybeAutoRemind(id, opts);
      },
    });
    document.addEventListener("keydown", onEscCapture, true);
    startReminderTimer();
  }

  window.briefdeskPlugins = window.briefdeskPlugins || {};
  window.briefdeskPlugins.reminders = { init: init };
})();

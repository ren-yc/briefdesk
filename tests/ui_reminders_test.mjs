// reminders 插件前端回归：到期提醒的投递语义与重设后可再次触发。
//
// 守两件事：
//  1. 页面隐藏且无桌面通知权限时不得清除服务端提醒（清除会静默吞掉提醒），
//     回到前台（visibilitychange）立即补查并展示。
//  2. 重设提醒会清掉本地「已通知」标记——否则同一卡片在本页会话内永不再触发。
//
// 本文件不经 loadAppJs，自建最小 vm 沙箱（reminders 只依赖核心全局助手）。
// 数据一律虚构（见 AGENTS.md）。

import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const UI_JS = path.join(ROOT, "briefdesk", "plugins", "reminders", "ui", "ui.js");

const DUE_ITEM = {
  id: "c1",
  title: "虚构讲座通知",
  category: "活动通知",
  remind_at: "2000-01-01 00:00",
};

function makeSandbox() {
  const state = {
    fetchCalls: [],       // [{url, init}]
    toasts: [],
    notifications: [],
    visibilityHandlers: [],
    timers: [],           // setTimeout 捕获的回调
    extension: null,
    hidden: false,
  };

  const documentStub = {
    hidden: false,
    querySelectorAll: () => [],
    querySelector: () => null,
    getElementById: () => null,
    addEventListener(type, fn) {
      if (type === "visibilitychange") state.visibilityHandlers.push(fn);
      if (type === "keydown") { /* onEscCapture：无需触发 */ }
    },
    removeEventListener() {},
  };
  Object.defineProperty(documentStub, "hidden", {
    get: () => state.hidden,
    set: (v) => { state.hidden = v; },
  });

  class NotificationStub {
    static permission = "denied";
    static requestPermission() { return Promise.resolve("denied"); }
    constructor(title, opts) { state.notifications.push({ title, opts }); }
    close() {}
  }

  const sandbox = {
    document: documentStub,
    Notification: NotificationStub,
    CSS: { escape: (s) => s },
    console,
    currentItems: [],
    parseLocalTime: (s) => new Date(String(s).replace(" ", "T")),
    isDateOnly: () => false,
    nextUpcomingTime: () => "",
    toLocalInput: (d) => d.toISOString(),
    escAttr: (s) => String(s),
    showToast: (msg, opts) => state.toasts.push({ msg, opts }),
    syncBodyScrollLock: () => {},
    exitPluginViews: () => {},
    $memoLink: { click: () => {} },
    registerItemRowExtension: (ext) => { state.extension = ext; },
    setInterval: () => 0,
    clearInterval: () => {},
    setTimeout: (fn) => { state.timers.push(fn); return 0; },
    clearTimeout: () => {},
    fetch: async (url, init) => {
      state.fetchCalls.push({ url, init });
      if (String(url).endsWith("/api/reminders/due")) {
        return { ok: true, json: async () => ({ items: [DUE_ITEM] }) };
      }
      return { ok: true, json: async () => ({ cleared: true, remind_at: null }) };
    },
  };
  sandbox.window = sandbox;
  sandbox.self = sandbox;

  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(UI_JS, "utf8"), sandbox, { filename: "ui.js" });
  return { sandbox, state, NotificationStub };
}

// 注意：「/api/reminders/due」与设置提醒的 POST 都含 "/reminder"，
// 必须按 URL 结尾 + body {"at":null}（清除语义）双重匹配
const countReminderClears = (state) =>
  state.fetchCalls.filter(
    (c) =>
      String(c.url).endsWith("/reminder")
      && String((c.init && c.init.body) || "").includes('"at":null')
  ).length;

// 只看到期提醒 toast；setReminderApi 的「提醒已设置」不算
const countReminderToasts = (state) =>
  state.toasts.filter((t) => String(t.msg).startsWith("提醒：")).length;

const flush = () => new Promise((resolve) => setImmediate(resolve));

// ── 场景 1：隐藏 + 无权限 → 不清除；回到前台补查并展示 ──
{
  const { sandbox, state, NotificationStub } = makeSandbox();
  NotificationStub.permission = "denied";
  sandbox.window.briefdeskPlugins.reminders.init({ isLoaded: () => true });

  // init 内的 setTimeout(checkDueReminders, 10000) 捕获了私有函数引用
  const check = state.timers.find((fn) => typeof fn === "function");

  state.hidden = true;
  await check();
  await flush();
  assert.equal(countReminderClears(state), 0, "隐藏且无权限时不得清除提醒");
  assert.equal(countReminderToasts(state), 0, "不可投递时不得弹 toast");

  // 回到前台：visibilitychange 回调补查
  state.hidden = false;
  assert.equal(state.visibilityHandlers.length, 1, "init 应注册 visibilitychange");
  await state.visibilityHandlers[0]();
  await flush();
  assert.equal(countReminderClears(state), 1, "回到前台应清除并投递");
  assert.equal(countReminderToasts(state), 1, "回到前台应弹一次 toast");
}

// ── 场景 2：重设提醒后可再次触发 ──
{
  const { sandbox, state, NotificationStub } = makeSandbox();
  NotificationStub.permission = "granted";
  sandbox.window.briefdeskPlugins.reminders.init({ isLoaded: () => true });
  const check = state.timers.find((fn) => typeof fn === "function");

  await check();
  await flush();
  assert.equal(countReminderToasts(state), 1, "首次到期应投递一次");

  // 模拟点击「保存」：经插件行内 handle 扩展
  const saveBtn = {
    classList: { contains: (c) => c === "remind-save" },
    closest: () => ({
      querySelector: () => ({ value: "2000-01-01T00:00" }),
    }),
  };
  const evt = { target: { closest: () => saveBtn } };
  const ctx = { rowOf: () => ({ dataset: { id: "c1" } }) };
  assert.equal(state.extension.handle(evt, ctx), true, "handle 应消费保存点击");
  await flush();

  await check();
  await flush();
  assert.equal(countReminderToasts(state), 2, "重设后应能再次触发提醒");
  assert.equal(countReminderClears(state), 2, "重设后应再次清除服务端提醒");
}

console.log("ui_reminders_test: all assertions passed");

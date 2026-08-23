// 前端首次使用向导“全选”回归测试（Node vm 加载真实 ui/app.js）。
// 验证 step2 渲染时会生成全选行，且全选行不会混入保存逻辑所需的会话复选框选择器。
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const appJs = fs.readFileSync(path.join(ROOT, "ui", "app.js"), "utf8");

function makeElement() {
  const el = {
    innerHTML: "",
    textContent: "",
    value: "",
    className: "",
    style: {},
    dataset: {},
    hidden: false,
    checked: false,
    indeterminate: false,
    classList: {
      add() {},
      remove() {},
      contains() { return false; },
      toggle() {},
    },
    addEventListener() {},
    removeEventListener() {},
    querySelector() { return null; },
    querySelectorAll() { return []; },
    closest() { return null; },
    setAttribute() {},
    removeAttribute() {},
    focus() {},
    appendChild() {},
    remove() {},
  };
  return el;
}

const elements = new Map();
function getElement(id) {
  if (!elements.has(id)) elements.set(id, makeElement());
  return elements.get(id);
}

const document = {
  getElementById: getElement,
  querySelector: () => makeElement(),
  querySelectorAll: () => [],
  createElement: (tag) => {
    const el = makeElement();
    if (tag === "div") {
      // 模拟 textContent -> innerHTML 的转义语义（app.js 的 esc() 依赖该行为）
      Object.defineProperty(el, "textContent", {
        get() { return this._textContent || ""; },
        set(value) {
          this._textContent = String(value);
          this.innerHTML = this._textContent.replace(/[&<>"']/g, (ch) => ({
            "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
          }[ch]));
        },
      });
    }
    return el;
  },
  addEventListener() {},
  body: makeElement(),
  documentElement: makeElement(),
  scrollingElement: makeElement(),
  activeElement: makeElement(),
  contains: () => true,
};

const noop = () => {};
const sandbox = {
  document,
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  navigator: { clipboard: {} },
  EventSource: class { constructor() { this.readyState = 0; } close() {} },
  MutationObserver: class { constructor() {} observe() {} disconnect() {} takeRecords() { return []; } },
  IntersectionObserver: class { constructor() {} observe() {} disconnect() {} },
  CSS: { escape: (s) => s },
  console,
  setTimeout,
  clearTimeout,
  setInterval,
  clearInterval,
  requestAnimationFrame: (fn) => fn(),
  cancelAnimationFrame: noop,
  matchMedia: () => ({ matches: false, addEventListener: noop, removeEventListener: noop }),
  addEventListener: noop,
  removeEventListener: noop,
  open: noop,
};
sandbox.window = sandbox;
sandbox.self = sandbox;

vm.createContext(sandbox);
vm.runInContext(appJs, sandbox, { filename: "ui/app.js" });

// 虚构会话，不含真实聊天内容
const sessions = [
  { source: "weflow", session_id: "g1", name: "测试群", is_group: 1, is_official: 0, enabled: 0, last_active: 1 },
  { source: "weflow", session_id: "p1", name: "测试私聊", is_group: 0, is_official: 0, enabled: 0, last_active: 1 },
];

sandbox.fetch = async (url) => {
  if (url === "/api/sessions") {
    return { ok: true, json: async () => ({ sessions, backfillHours: 24 }) };
  }
  if (url === "/api/status") {
    return { ok: true, json: async () => ({ sources: {} }) };
  }
  throw new Error("unexpected fetch: " + url);
};

await sandbox.renderOnboardSessions();

const list = document.getElementById("onboard-sessions");
assert.ok(
  list.innerHTML.includes('id="onboard-select-all"'),
  "向导 step2 应渲染全选复选框"
);
assert.ok(
  list.innerHTML.includes("全选"),
  "向导 step2 应显示“全选”文字"
);
assert.ok(
  list.innerHTML.includes('data-session-id="g1"'),
  "会话行应保留 data-session-id 供保存逻辑使用"
);

console.log("ui_onboarding_select_all_test: ok");

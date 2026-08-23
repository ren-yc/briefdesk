// 前端上下文缓存回归测试（Node vm 直接加载真实的 ui/app.js）。
// 复现场景：同会话同秒两条消息（msg_time 相同、msg_id 不同），
// 先展开卡片 A 再展开卡片 B，各自的“原文在上下文中标绿”必须各归其位。
// 数据为虚构文本，避免写入真实聊天内容。

import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const appJs = fs.readFileSync(path.join(ROOT, "ui", "app.js"), "utf8");

const ESC_MAP = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

function makeElement() {
  const el = {
    innerHTML: "",
    textContent: "",
    value: "",
    className: "",
    style: {},
    dataset: {},
    hidden: false,
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

const document = {
  getElementById: () => makeElement(),
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
          this.innerHTML = this._textContent.replace(/[&<>"']/g, (ch) => ESC_MAP[ch]);
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
  EventSource: class {
    constructor() { this.readyState = 0; }
    close() {}
  },
  MutationObserver: class {
    constructor() {}
    observe() {}
    disconnect() {}
    takeRecords() { return []; }
  },
  IntersectionObserver: class {
    constructor() {}
    observe() {}
    disconnect() {}
  },
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

assert.equal(typeof sandbox.fetchContext, "function", "应能从 app.js 加载 fetchContext");

// 同秒不同消息（虚构数据）
const messages = [
  { time: 1700000000, msg_id: "msg-alpha", sender: "甲同学", content: "甲社团招新", article_url: "" },
  { time: 1700000000, msg_id: "msg-beta", sender: "乙同学", content: "乙社团招新", article_url: "" },
];

let fetchCalls = 0;
sandbox.fetch = async () => {
  fetchCalls += 1;
  return { json: async () => ({ messages }) };
};

const fetchContext = sandbox.fetchContext;

function targetLine(html) {
  const start = html.indexOf('class="ctx-msg ctx-target"');
  if (start === -1) return null;
  const br = html.indexOf("<br>", start);
  const end = html.indexOf("</div>", br);
  return html.slice(br + 4, end);
}

const divA = makeElement();
const divB = makeElement();
await fetchContext(divA, "weflow", "test-session", 1700000000, "msg-alpha");
await fetchContext(divB, "weflow", "test-session", 1700000000, "msg-beta");

assert.equal(fetchCalls, 1, "同一 source|session|t 的消息列表应只请求一次");
assert.equal(targetLine(divA.innerHTML), "甲社团招新", "卡片 A 应高亮自己的原文");
assert.equal(targetLine(divB.innerHTML), "乙社团招新", "卡片 B 应高亮自己的原文（不能复用 A 的高亮 HTML）");

// 再次展开（模拟视图切换恢复）仍走缓存且高亮不串
const divC = makeElement();
await fetchContext(divC, "weflow", "test-session", 1700000000, "msg-alpha");
assert.equal(fetchCalls, 1, "再次展开同时间窗口不应重复请求");
assert.equal(targetLine(divC.innerHTML), "甲社团招新", "缓存恢复时高亮仍应正确");

console.log("ui_context_cache_test: ok");

// 前端测试共用的最小 DOM/BOM 桩：把真实的 ui/app.js 加载进 Node vm。
//
// app.js 是同源 classic script（不能 ESM 化、不能 IIFE 包裹，见 docs/architecture.md），
// 顶层就会 document.getElementById(...) 一大批常量，因此桩只需"任何 id 都返回一个
// 元素"即可让脚本跑完；各测试再按需覆写 sandbox.fetch 等注入点。
//
// getElementById 按 id 缓存（同 id 恒返回同一对象）：app.js 里"渲染时写
// innerHTML、随后另一处按 id 取回来读"的写法很常见，不缓存的话读到的是空白新元素。
//
// 测试数据一律虚构，不得写入真实聊天内容（见 AGENTS.md）。

import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

export const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

const ESC_MAP = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

// selectorMap: { ".card-quote-context": el } —— 只给需要"按选择器取到真实子元素"的
// 测试用；不传时 querySelector 恒为 null，与此前行为一致。
export function makeElement(selectorMap = null) {
  const classes = new Set();
  return {
    innerHTML: "",
    textContent: "",
    value: "",
    className: "",
    style: {},
    dataset: {},
    hidden: false,
    checked: false,
    indeterminate: false,
    // classList 真实记账：app.js 大量使用 add("hidden") 后再 contains("hidden") 判定
    // 幂等（如"已展开就不重复请求上下文"）。恒返回 false 的桩会让这类分支永远走
    // "首次"路径，把幂等缺陷测成通过。
    classList: {
      add: (...names) => names.forEach((n) => classes.add(n)),
      remove: (...names) => names.forEach((n) => classes.delete(n)),
      contains: (name) => classes.has(name),
      toggle: (name, force) => {
        const on = force === undefined ? !classes.has(name) : force;
        if (on) classes.add(name); else classes.delete(name);
        return on;
      },
    },
    addEventListener() {},
    removeEventListener() {},
    querySelector(sel) { return (selectorMap && selectorMap[sel]) || null; },
    querySelectorAll() { return []; },
    closest() { return null; },
    setAttribute() {},
    removeAttribute() {},
    focus() {},
    appendChild() {},
    remove() {},
  };
}

// 建立 sandbox 并执行 ui/app.js；返回的 sandbox 即 window，其上是 app.js 的全局符号。
// localStorage 可选注入：需要测试「localStorage 初始态 → 模块级初始化」（如 listMode
// 迁移读取）时传入受控实现；默认桩恒返回 null（首次使用）。
export function loadAppJs({ localStorage: localStorageStub } = {}) {
  const elements = new Map();
  const getElement = (id) => {
    if (!elements.has(id)) elements.set(id, makeElement());
    return elements.get(id);
  };

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
    localStorage: localStorageStub || { getItem: () => null, setItem() {}, removeItem() {} },
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
  vm.runInContext(fs.readFileSync(path.join(ROOT, "ui", "app.js"), "utf8"), sandbox, {
    filename: "ui/app.js",
  });
  return { sandbox, document, getElement };
}

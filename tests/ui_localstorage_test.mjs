// 前端 localStorage 单源助手回归测试（Node vm 直接加载真实的 ui/app.js）。
//
// 守的缺陷：隐私模式/配额耗尽/站点存储被策略禁用时，localStorage 的读写都会抛异常。
// 助手化之前有一半调用点没包 try，异常会中断整个事件处理函数，把"持久化失败"升级成
// "功能不响应"（主题不切换、折叠开关不重渲染）。故此处显式用"必抛的 localStorage"
// 跑一遍读写，断言异常不外泄且退回 fallback。
//
// 数据一律虚构（见 AGENTS.md）。

import assert from "node:assert/strict";

import { loadAppJs } from "./ui_harness.mjs";

const { sandbox } = loadAppJs();

for (const name of ["lsGet", "lsSet", "lsGetJson", "lsSetJson"]) {
  assert.equal(typeof sandbox[name], "function", `应能从 app.js 加载 ${name}`);
}
const { lsGet, lsSet, lsGetJson, lsSetJson } = sandbox;

// vm 里 JSON.parse 出来的对象挂的是 vm realm 的 Object.prototype，assert/strict 的
// 深比较会连原型一起比，导致"长得一样却不相等"。故跨 realm 的值先在本 realm 重建。
const plain = (v) => JSON.parse(JSON.stringify(v));

// ── 正常存储：读写往返 ──
const store = new Map();
sandbox.localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k),
};

assert.equal(lsSet("bd.t", "dark"), true, "写入成功应返回 true");
assert.equal(lsGet("bd.t"), "dark", "应读回写入的值");
assert.equal(lsGet("bd.missing"), null, "缺键默认返回 null");
assert.equal(lsGet("bd.missing", "light"), "light", "缺键应返回传入的 fallback");

// 空串是合法存值，不能被当成"缺键"而退回 fallback
lsSet("bd.empty", "");
assert.equal(lsGet("bd.empty", "fallback"), "", "空串是有效值，不应退回 fallback");

assert.equal(lsSetJson("bd.j", { a: 1, b: ["x"] }), true, "JSON 写入应返回 true");
assert.deepEqual(plain(lsGetJson("bd.j")), { a: 1, b: ["x"] }, "应读回等价的 JSON 结构");
assert.deepEqual(lsGetJson("bd.none", []), [], "缺键应返回 JSON fallback");
assert.equal(lsGetJson("bd.none"), null, "缺键且未传 fallback 时返回 null");

// 坏数据（非法 JSON）不该让功能崩掉
store.set("bd.broken", "{不是合法 JSON");
assert.deepEqual(lsGetJson("bd.broken", []), [], "解析失败应退回 fallback 而非抛出");
// 合法但非对象的存值（标量/null）原样返回，由调用点自行兜（loadSettings 用 || {}）
store.set("bd.scalar", "42");
assert.equal(lsGetJson("bd.scalar", {}), 42, "标量 JSON 应原样返回");
store.set("bd.null", "null");
assert.equal(lsGetJson("bd.null", { a: 1 }), null, "存值为 null 时返回 null（非 fallback）");

// 循环引用导致 JSON.stringify 抛出：吞掉并报告失败
const cyclic = { name: "环" };
cyclic.self = cyclic;
assert.equal(lsSetJson("bd.cyclic", cyclic), false, "序列化失败应返回 false 而非抛出");

// ── 必抛的存储（隐私模式 / 配额耗尽 / 存储被禁用）──
sandbox.localStorage = {
  getItem() { throw new Error("SecurityError: storage disabled"); },
  setItem() { throw new Error("QuotaExceededError"); },
  removeItem() { throw new Error("SecurityError: storage disabled"); },
};

assert.equal(lsGet("bd.t"), null, "读抛异常时应返回 null");
assert.equal(lsGet("bd.t", "light"), "light", "读抛异常时应返回 fallback");
assert.equal(lsSet("bd.t", "dark"), false, "写抛异常时应返回 false 而非抛出");
assert.deepEqual(lsGetJson("bd.j", { fallback: true }), { fallback: true }, "JSON 读抛异常时应返回 fallback");
assert.equal(lsSetJson("bd.j", { a: 1 }), false, "JSON 写抛异常时应返回 false 而非抛出");

// ── 存储本身不可访问（getter 即抛，部分浏览器的 cookie 全禁场景）──
// 助手把 localStorage 的解析也放在 try 内，故这种形态同样不外泄。
Object.defineProperty(sandbox, "localStorage", {
  configurable: true,
  get() { throw new Error("SecurityError: access denied"); },
});

assert.equal(lsGet("bd.t", "light"), "light", "localStorage 不可访问时读应退回 fallback");
assert.equal(lsSet("bd.t", "dark"), false, "localStorage 不可访问时写应返回 false");
assert.equal(lsGetJson("bd.j", null), null, "localStorage 不可访问时 JSON 读应退回 fallback");
assert.equal(lsSetJson("bd.j", { a: 1 }), false, "localStorage 不可访问时 JSON 写应返回 false");

console.log("ui_localstorage_test: ok");

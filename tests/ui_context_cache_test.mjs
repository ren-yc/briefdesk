// 前端上下文缓存回归测试（Node vm 直接加载真实的 ui/app.js）。
// 复现场景：同会话同秒两条消息（msg_time 相同、msg_id 不同），
// 先展开卡片 A 再展开卡片 B，各自的“原文在上下文中标绿”必须各归其位。
// 数据为虚构文本，避免写入真实聊天内容。

import assert from "node:assert/strict";

import { loadAppJs, makeElement } from "./ui_harness.mjs";

const { sandbox } = loadAppJs();

assert.equal(typeof sandbox.fetchContext, "function", "应能从 app.js 加载 fetchContext");

// 同秒不同消息（虚构数据）
const messages = [
  { time: 1700000000, msg_id: "msg-alpha", sender: "甲同学", content: "甲社团招新", article_url: "" },
  { time: 1700000000, msg_id: "msg-beta", sender: "乙同学", content: "乙社团招新", article_url: "" },
];

// 假响应带上 ok/status：生产侧统一走 reqJson，非 2xx 一律抛错。缺 ok 的桩会被
// 判成失败请求 → 不写缓存 → 下次重复请求，把契约缺口伪装成缓存 bug。
let fetchCalls = 0;
sandbox.fetch = async () => {
  fetchCalls += 1;
  return { ok: true, status: 200, json: async () => ({ messages }) };
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

// ── expandQuoteContext：四处展开入口共用的装载逻辑 ──
const expandQuoteContext = sandbox.expandQuoteContext;
assert.equal(typeof expandQuoteContext, "function", "应能从 app.js 加载 expandQuoteContext");

// 构造「容器 + 其中的 .card-quote-context」，容器自带 dataset（主列表卡片形态）
function makeScope(dataset, { ctxHidden = true } = {}) {
  const ctx = makeElement();
  if (ctxHidden) ctx.classList.add("hidden");
  const scope = makeElement({ ".card-quote-context": ctx });
  Object.assign(scope.dataset, dataset);
  return { scope, ctx };
}

const okDataset = { source: "weflow", sessionId: "test-session", msgtime: "1700000000", msgid: "msg-alpha" };

// 单参形态：scope 同时提供 ctxDiv 与 dataset
const one = makeScope(okDataset);
expandQuoteContext(one.scope);
await Promise.resolve();
assert.equal(one.ctx.classList.contains("hidden"), false, "展开后应移除 hidden");
assert.equal(fetchCalls, 1, "命中缓存时不应新增请求");
assert.equal(targetLine(one.ctx.innerHTML), "甲社团招新", "单参形态应把上下文写进 ctxDiv 并标绿原文");

// 双参形态：ctxDiv 在 scope 里、dataset 在另一个元素上（浮层行 / 时间线行）
const ctxTwo = makeElement();
ctxTwo.classList.add("hidden");
const detail = makeElement({ ".card-quote-context": ctxTwo });
const row = makeElement();
Object.assign(row.dataset, { ...okDataset, msgid: "msg-beta" });
expandQuoteContext(detail, row);
await Promise.resolve();
assert.equal(ctxTwo.classList.contains("hidden"), false, "双参形态也应移除 hidden");
assert.equal(targetLine(ctxTwo.innerHTML), "乙社团招新", "双参形态应按 srcEl 的 dataset 定位原文");

// 幂等：已展开（无 hidden）时直接返回，不重写内容也不再请求
const opened = makeScope(okDataset, { ctxHidden: false });
opened.ctx.innerHTML = "保留原样";
expandQuoteContext(opened.scope);
await Promise.resolve();
assert.equal(opened.ctx.innerHTML, "保留原样", "已展开时应原样返回（幂等）");

// 缺 session_id 的旧数据：提示而非发请求
const legacy = makeScope({ source: "weflow", msgtime: "1700000000" });
expandQuoteContext(legacy.scope);
await Promise.resolve();
assert.match(legacy.ctx.innerHTML, /缺少会话ID/, "旧数据应显示提示文案");
assert.equal(fetchCalls, 1, "缺 session_id 时不应发起请求");

// 容器里没有 .card-quote-context（无原文引用的卡片）时不应抛错
assert.doesNotThrow(() => expandQuoteContext(makeElement()), "无 ctxDiv 时应静默返回");

console.log("ui_context_cache_test: ok");

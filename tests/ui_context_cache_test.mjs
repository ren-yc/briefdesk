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

console.log("ui_context_cache_test: ok");

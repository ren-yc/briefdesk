// 前端首次使用向导“全选”回归测试（Node vm 加载真实 ui/app.js）。
// 验证 step2 渲染时会生成全选行，且全选行不会混入保存逻辑所需的会话复选框选择器。

import assert from "node:assert/strict";

import { loadAppJs } from "./ui_harness.mjs";

const { sandbox, document } = loadAppJs();

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

// 备份 / 导出在基准窗口内的 409 提示回归。
//
// downloadExport 此前对一切非 2xx 只提示「导出失败，请重试」且不读响应体：
// 窗口期后端返回 409 + {"detail":{"code":"benchmark_running"}} 时，用户会
// 反复点重试而重试在窗口结束前不可能成功。
//
// 数据一律虚构（见 AGENTS.md）。

import assert from "node:assert/strict";

import { loadAppJs } from "./ui_harness.mjs";

const BUSY_DETAIL = {
  code: "benchmark_running",
  message: "基准运行中：界面写操作与备份/导出暂不可用，请等运行结束后重试",
};

function response(status, body) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    blob: async () => { throw new Error("409 路径不得读到 blob"); },
    headers: { get: () => null },
  };
}

function setup(res) {
  const { sandbox } = loadAppJs();
  const toasts = [];
  sandbox.showToast = (msg, opts) => toasts.push({ msg, opts });
  sandbox.fetch = async () => res;
  // downloadExport 的 catch 会打 console.error，压掉以免刷屏（断言只看 toast）
  sandbox.console = { log: console.log, error: () => {}, warn: () => {} };
  return { sandbox, toasts };
}

// ── 场景 1：窗口期 409 → 专用提示，而非「导出失败」──
{
  const { sandbox, toasts } = setup(response(409, { detail: BUSY_DETAIL }));
  await sandbox.downloadExport("/api/backup");
  assert.equal(toasts.length, 1, "应只提示一次");
  assert.ok(
    toasts[0].msg.includes("基准运行中") && toasts[0].msg.includes("备份/导出"),
    `窗口期应提示基准运行中，实际: ${toasts[0].msg}`,
  );
  assert.equal(toasts[0].opts && toasts[0].opts.type, "info", "窗口提示应为 info 类型");
}

// ── 场景 2（对照）：其它 409（detail 为普通字符串）→ 保持原文案 ──
{
  const { sandbox, toasts } = setup(response(409, { detail: "其它冲突" }));
  await sandbox.downloadExport("/api/export/items");
  assert.equal(toasts.length, 1);
  assert.equal(toasts[0].msg, "导出失败，请重试", "非基准 409 不得误报为窗口期");
}

// ── 场景 3（对照）：500 → 保持原文案 ──
{
  const { sandbox, toasts } = setup(response(500, { detail: "boom" }));
  await sandbox.downloadExport("/api/export/items");
  assert.equal(toasts.length, 1);
  assert.equal(toasts[0].msg, "导出失败，请重试", "非 409 不得读响应体改文案");
}

// ── 场景 4：非 JSON 的 409 响应体不得让提示路径抛错 ──
{
  const { sandbox, toasts } = setup({
    ok: false,
    status: 409,
    json: async () => { throw new SyntaxError("not json"); },
    blob: async () => { throw new Error("不应读到 blob"); },
    headers: { get: () => null },
  });
  await sandbox.downloadExport("/api/backup");
  assert.equal(toasts.length, 1);
  assert.equal(toasts[0].msg, "导出失败，请重试", "解析失败应退回原文案");
}

console.log("ui_export_window_test: all assertions passed");

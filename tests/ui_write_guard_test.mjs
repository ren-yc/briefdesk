// 写操作失败提示回归：普通错误必须保持调用点原文案与 error 级别。
//
// 基准窗口机制已删除：后端不再有写闸门，前端也不再有「基准运行中」的分流，
// 因此这里只守保留下来的那条出口。
//
// 数据一律虚构（见 AGENTS.md）。

import assert from "node:assert/strict";

import { loadAppJs } from "./ui_harness.mjs";

// ── 场景 1：普通写失败按调用点原文案与级别提示 ──
{
  const { sandbox } = loadAppJs();
  const toasts = [];
  sandbox.showToast = (msg, opts) => toasts.push({ msg, opts });

  sandbox.showWriteError(new Error("HTTP 500"), "操作失败，请重试", 6000);
  assert.equal(toasts.length, 1, "应只提示一次");
  assert.equal(toasts[0].msg, "操作失败，请重试");
  assert.equal(toasts[0].opts.type, "error");
  assert.equal(toasts[0].opts.duration, 6000, "长文案调用点的原时长必须保留");
}

console.log("ui_write_guard_test: all assertions passed");

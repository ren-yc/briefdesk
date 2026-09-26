// 基准窗口的前端提示分流：写操作失败提示、同步按钮 409 分流。
//
// 窗口内后端以写闸门拒绝一切变更请求（409 + {"detail":{"code":
// "benchmark_running"}}）。前端若不读响应体，会把窗口期拒绝说成普通失败或
// 「同步已在后台进行中」，用户会反复重试或一直等一个不会来的结果。
//
// 数据一律虚构（见 AGENTS.md）。

import assert from "node:assert/strict";

import { loadAppJs } from "./ui_harness.mjs";

const BUSY = { detail: { code: "benchmark_running", message: "基准运行中" } };

function busyError() {
  const err = new Error("HTTP 409");
  err.payload = BUSY;
  return err;
}

// ── 场景 1：showWriteError 在窗口期给 info 专用提示 ──
{
  const { sandbox } = loadAppJs();
  const toasts = [];
  sandbox.showToast = (msg, opts) => toasts.push({ msg, opts });

  sandbox.showWriteError(busyError(), "操作失败，请重试");
  assert.equal(toasts.length, 1, "应只提示一次");
  assert.ok(toasts[0].msg.includes("基准运行中"), `窗口期应提示基准运行中: ${toasts[0].msg}`);
  assert.equal(toasts[0].opts.type, "info", "窗口提示应为 info 而非 error");
}

// ── 场景 2（对照）：非窗口错误保持原文案与 error 级别 ──
{
  const { sandbox } = loadAppJs();
  const toasts = [];
  sandbox.showToast = (msg, opts) => toasts.push({ msg, opts });

  sandbox.showWriteError(new Error("HTTP 500"), "操作失败，请重试");
  assert.equal(toasts[0].msg, "操作失败，请重试", "非窗口错误不得改写文案");
  assert.equal(toasts[0].opts.type, "error");
}

// ── 场景 3（对照）：detail 是普通字符串的 409 不算窗口期 ──
{
  const { sandbox } = loadAppJs();
  const toasts = [];
  sandbox.showToast = (msg, opts) => toasts.push({ msg, opts });

  const err = new Error("HTTP 409");
  err.payload = { detail: "其它冲突" };
  sandbox.showWriteError(err, "操作失败，请重试");
  assert.equal(toasts[0].msg, "操作失败，请重试");
  assert.equal(toasts[0].opts.type, "error");
}

function setupSync(res) {
  const { sandbox, getElement } = loadAppJs();
  const toasts = [];
  sandbox.showToast = (msg, opts) => toasts.push({ msg, opts });
  let handler = null;
  getElement("sync-btn").addEventListener = (ev, fn) => {
    if (ev === "click") handler = fn;
  };
  sandbox.bindSyncButtonEvents();
  assert.ok(handler, "应能捕获同步按钮的 click 处理器");
  sandbox.fetch = async () => res;
  return { sandbox, toasts, handler };
}

// ── 场景 4：窗口期同步返回 409 → 提示基准运行中，而非「同步已在进行」──
{
  const { toasts, handler } = setupSync({
    ok: false,
    status: 409,
    json: async () => BUSY,
  });
  await handler();
  assert.ok(toasts.some(t => t.msg.includes("基准运行中")), "窗口期应提示基准运行中");
  assert.ok(
    !toasts.some(t => t.msg.includes("同步已在后台进行中")),
    "不得把窗口期拒绝说成同步已在后台进行中",
  );
}

// ── 场景 5（对照）：确实在同步中的 409 保持原提示 ──
{
  const { toasts, handler } = setupSync({
    ok: false,
    status: 409,
    json: async () => ({ detail: "同步已在后台进行中" }),
  });
  await handler();
  assert.equal(toasts.length, 1);
  assert.equal(toasts[0].msg, "同步已在后台进行中，无需重复触发");
}

// ── 场景 6：窗口期列表区整块替换为提示，不渲染临时库合成卡 ──
{
  const { sandbox, getElement } = loadAppJs();
  // 这些助手在窗口分支里被调用；先断言它们确实存在（拼错名字会在此暴露），
  // 再替换为 noop 以免依赖桩 DOM 的复杂状态
  for (const name of [
    "updateListCount", "updateLoadMore", "updateSubsBadge",
    "syncBatchGroupStates", "rebuildKbUnits", "syncOverlayWithData",
  ]) {
    assert.equal(typeof sandbox[name], "function", `app.js 应保留 ${name}（窗口分支依赖）`);
    sandbox[name] = () => {};
  }
  const toasts = [];
  sandbox.showToast = (msg, opts) => toasts.push({ msg, opts });

  sandbox.renderAnnouncements([
    { code: "benchmark_running", level: "warning", message: "基准运行中" },
  ]);
  sandbox.renderItems([{ id: "1", title: "临时库合成卡标题" }], { full: true });
  const html = getElement("items-container").innerHTML;
  assert.ok(html.includes("列表暂不可用"), "窗口期列表区应显示不可用提示");
  assert.ok(!html.includes("临时库合成卡标题"), "窗口期不得渲染临时库里的卡片");

  // 公告撤销后标志回落：再渲染时列表恢复正常，并提示补一次同步
  sandbox.renderAnnouncements([]);
  const after = toasts.filter(t => t.msg.includes("基准已结束"));
  assert.equal(after.length, 1, "窗口结束应提示补一次同步");
  sandbox.renderItems([{ id: "2", title: "生产卡片二" }], { full: true });
  const resumed = getElement("items-container").innerHTML;
  assert.ok(resumed.includes("生产卡片二"), "标志回落后列表应恢复渲染");
  assert.ok(!resumed.includes("列表暂不可用"), "标志回落后不应再显示占位");
}

// ── 场景 7：窗口标志经 SSE 推送切换时立即重新拉取列表 ──
{
  const { sandbox } = loadAppJs();
  let fetches = 0;
  sandbox.fetchData = () => { fetches += 1; };
  sandbox.showToast = () => {};

  assert.equal(
    typeof sandbox.onAnnouncementsPushed, "function",
    "应提供公告推送入口（SSE 路径不经 fetchData，需要它触发重绘）",
  );

  // 与窗口标志无关的公告（准备阶段 DB 仍是生产库，列表本就正确）不触发拉取
  sandbox.onAnnouncementsPushed([
    { code: "benchmark_preparing", level: "warning", message: "基准准备中" },
  ]);
  assert.equal(fetches, 0, "与窗口标志无关的公告不应触发拉取");

  const running = [
    { code: "benchmark_running", level: "warning", message: "基准运行中" },
  ];
  sandbox.onAnnouncementsPushed(running);
  assert.equal(fetches, 1, "窗口开始应触发一次重新拉取");

  sandbox.onAnnouncementsPushed(running);
  assert.equal(fetches, 1, "内容相同的重复推送不应重复拉取");

  sandbox.onAnnouncementsPushed([]);
  assert.equal(fetches, 2, "窗口结束应触发一次重新拉取，否则列表停在占位");
}

// ── 场景 8：窗口内保存——暂存段成功、类别/会话 ops 被拒时两条提示都要给出 ──
{
  const { sandbox } = loadAppJs();
  const toasts = [];
  sandbox.showToast = (msg, opts) => toasts.push({ msg, opts });
  sandbox.stagePendingEnvChanges = async () => "committed";
  sandbox.collectAllOps = () => [{ type: "delete", id: 1, name: "虚构类别" }];
  sandbox.getJson = async () => ({ syncing: false });
  const busy = new Error("HTTP 409");
  busy.payload = BUSY;
  sandbox.runSettingsOps = async () => { throw busy; };
  for (const name of [
    "saveSettings", "closeSettingsModal", "startRefreshTimer", "fetchData",
    "loadCategories", "loadSessions", "loadEnvConfig",
  ]) {
    assert.equal(typeof sandbox[name], "function", `app.js 应保留 ${name}`);
    sandbox[name] = () => {};
  }

  await sandbox.saveAllSettings();
  assert.ok(
    toasts.some(t => t.msg.includes("基准运行中")),
    "应提示写操作因基准运行中不可用",
  );
  assert.ok(
    toasts.some(t => t.msg.includes("已暂存")),
    "暂存段确已成功的提示不得被窗口提示盖掉（否则用户以为什么都没保存）",
  );
}

// ── 场景 9：批量撤销部分被拒时仍汇报已成功条数 ──
{
  const { sandbox } = loadAppJs();
  const toasts = [];
  sandbox.showToast = (msg, opts) => toasts.push({ msg, opts });
  sandbox.fetchData = () => {};
  const busy = new Error("HTTP 409");
  busy.payload = BUSY;

  let calls = 0;
  sandbox.postVerify = async () => {
    calls += 1;
    if (calls > 1) throw busy;
  };
  await sandbox.restoreBatch(new Map([["a", 0], ["b", 0]]));
  assert.equal(toasts.length, 1, "只提示一次，避免逐条刷屏");
  assert.ok(toasts[0].msg.includes("1/2"), `应汇报已成功的条数: ${toasts[0].msg}`);
  assert.ok(toasts[0].msg.includes("基准运行中"), "应说明其余未执行的原因");

  toasts.length = 0;
  sandbox.postVerify = async () => { throw busy; };
  await sandbox.restoreBatch(new Map([["c", 0]]));
  assert.equal(toasts.length, 1);
  assert.ok(!toasts[0].msg.includes("0/1"), "全部被拒时不应出现 x/y 计数");
}

console.log("ui_write_guard_test: all assertions passed");

// fetchData 过期请求失败回归：旧请求晚失败不得覆盖最新连接状态。
//
// 快速切换查询会并发多个 fetchData；成功路径与 finally 早就有
// 「seq !== fetchSeq」守卫，catch 此前没有——旧请求失败会把状态胶囊改成
// 「连接失败」，并覆盖已渲染的新数据提示。
//
// 数据一律虚构（见 AGENTS.md）。

import assert from "node:assert/strict";

import { loadAppJs } from "./ui_harness.mjs";

const PAYLOAD = {
  items: [],
  hasMore: false,
  nextOffset: 0,
  status: { syncing: false },
  categories: [],
  allCategories: [],
  totalCount: 0,
  groupCount: 0,
  memoCount: 0,
  ignoredCount: 0,
};

function deferred() {
  let resolve, reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

// 渲染函数在桩 DOM 下可能抛错：一律换成 noop（本测试只关心状态胶囊）
function stub(sandbox) {
  for (const name of [
    "renderItems", "renderFilterBar", "applySidebarData", "renderStatusBanner",
    "renderAnnouncements", "updateLoadMore", "consumePluginRefresh",
  ]) {
    sandbox[name] = () => (name === "consumePluginRefresh" ? false : undefined);
  }
}

// ── 场景 1：旧请求晚失败 → 不覆盖新请求渲染的状态 ──
{
  const { sandbox, getElement } = loadAppJs();
  stub(sandbox);

  const first = deferred();
  const second = deferred();
  const queue = [first.promise, second.promise];
  sandbox.requestItemPage = () => queue.shift();

  const p1 = sandbox.fetchData();
  const p2 = sandbox.fetchData();

  second.resolve(PAYLOAD);
  await p2;

  first.reject(new Error("stale failure"));
  await p1;

  const indicator = getElement("status-indicator");
  const text = getElement("status-text");
  assert.notEqual(indicator.className, "status offline", "过期失败不得改成离线态");
  assert.ok(!String(text.innerHTML).includes("连接失败"), "过期失败不得写连接失败");
}

// ── 场景 2（对照）：最新请求失败 → 照常显示连接失败 ──
{
  const { sandbox, getElement } = loadAppJs();
  stub(sandbox);

  const only = deferred();
  sandbox.requestItemPage = () => only.promise;

  const p = sandbox.fetchData();
  only.reject(new Error("boom"));
  await p;

  const indicator = getElement("status-indicator");
  const text = getElement("status-text");
  assert.equal(indicator.className, "status offline", "最新请求失败应显示离线态");
  assert.ok(String(text.innerHTML).includes("连接失败"), "最新请求失败应写连接失败");
}

console.log("ui_fetch_stale_error_test: all assertions passed");

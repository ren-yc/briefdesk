// 基准运行期间列表区必须照常可用。
//
// 背景：前端曾按公告码把列表区整块替换成「列表暂不可用」占位——那是进程内运行路径的
// 语义（列表里是临时基准库的合成卡）。窗口机制删除后占位分支也已移除；运行只发
// benchmark_paused（表达「消息处理已暂停，结束后请点一次同步」），因此公告条照常显示，
// 列表区必须继续渲染真实卡片。

import assert from "node:assert/strict";
import { loadAppJs } from "./ui_harness.mjs";

{
  const { sandbox, getElement } = loadAppJs();
  // renderItems 会调用这些助手；先确认存在再替换为 noop（拼错名字会在此暴露）
  for (const name of [
    "updateListCount", "updateLoadMore", "updateSubsBadge",
    "syncBatchGroupStates", "rebuildKbUnits", "syncOverlayWithData",
  ]) {
    assert.equal(typeof sandbox[name], "function", `app.js 应保留 ${name}`);
    sandbox[name] = () => {};
  }
  sandbox.showToast = () => {};

  sandbox.renderAnnouncements([
    { code: "benchmark_paused", level: "warning", message: "基准运行中：消息处理已暂停" },
  ]);
  assert.ok(
    getElement("announcements").innerHTML.includes("消息处理已暂停"),
    "新公告码应照常显示在公告条上"
  );

  sandbox.renderItems([{ id: "1", title: "生产卡片甲" }], { full: true });
  const html = getElement("items-container").innerHTML;
  assert.ok(!html.includes("列表暂不可用"), "新公告码不得把列表区替换成占位");
  assert.ok(html.includes("生产卡片甲"), "运行期间列表区应照常渲染真实卡片");
}

console.log("ui_benchmark_paused_test: OK");

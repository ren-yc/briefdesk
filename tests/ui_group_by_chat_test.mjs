// 按群聊折叠（listMode="group"）回归测试：分组口径、多群卡复制渲染、新卡提示补齐、
// 计数三态与 localStorage 迁移。
//
// 守五件事：
//  1. 多群合并卡（source_group="群A, 群B"）在两个群组内各渲染一份（「每个来源群各出现
//     一次」），groupMap 以群名为键——浮层/批量整组选择/组高亮与合并模式共用同一数据源。
//  2. 组块按组内最新成员时间降序，组内成员保持传入顺序（服务端 msg_time DESC）。
//  3. 按群模式走全量渲染后，新卡高亮集合按渲染前 diff 重建（markNewCards 收到的
//     是真正新增的 id），不因 fullRenderItems 内 confirmNewItems 清空而丢失。
//  4. 头部计数按 listMode 三态分流；按群口径 N=渲染时统计的来源群数。
//  5. listMode 初始化读 briefdesk.listMode，旧两态键 briefdesk.collapseGroups 迁移读取，
//     非法值回退 merged（历史默认=折叠）。
//
// vm context 中顶层 let（listMode/viewCounts/currentItems 等）不在 sandbox 上，
// function 声明在且可覆写——故 listMode 等状态一律经「受控 localStorage 多实例 +
// 行为断言（updateListCount 文本 / 渲染分发）」观察，词法基准经真实 fullRenderItems
// 建立。数据一律虚构（见 AGENTS.md）。

import assert from "node:assert/strict";

import { loadAppJs } from "./ui_harness.mjs";

const T = 1750000000; // 固定秒级时间戳（虚构）

function item(id, groups, msgTime) {
  return { id, source_group: groups, msg_time: msgTime, subject: "", title: id };
}

// 渲染桩：只保留结构断言所需的最小形态（真实 renderCard 依赖链过长）
function stubRenderCard(sandbox) {
  sandbox.renderCard = (it, key) =>
    `<div class="item-card" data-id="${it.id}" data-key="${key || ""}"></div>`;
}

function loadWithStore(entries) {
  const store = new Map(entries);
  return loadAppJs({
    localStorage: {
      getItem: (k) => (store.has(k) ? store.get(k) : null),
      setItem: (k, v) => store.set(k, String(v)),
      removeItem: (k) => store.delete(k),
    },
  });
}

// ── 实例 A：listMode="group"（受控 localStorage）──
const A = loadWithStore([["briefdesk.listMode", "group"]]);
const a = A.sandbox;

// listMode 为词法 let 不可直读 → 经计数文本观察（初始计数全 0）
const countA = A.document.getElementById("list-count");
a.updateListCount();
assert.equal(countA.textContent, "共 0 群", "listMode=group 时计数走按群口径");

// ── chatGroupsOfSourceGroup：拆分口径（展开符跨 vm realm 转本 realm 数组）──
assert.deepEqual([...a.chatGroupsOfSourceGroup(item("a", "计算机系群", T))], ["计算机系群"]);
assert.deepEqual([...a.chatGroupsOfSourceGroup(item("b", "摄影社, 桌游社", T))], ["摄影社", "桌游社"]);
assert.deepEqual([...a.chatGroupsOfSourceGroup(item("c", "摄影社,桌游社", T))], ["摄影社", "桌游社"], "无空格逗号容错");
assert.deepEqual([...a.chatGroupsOfSourceGroup(item("d", "", T))], ["未知来源"], "空来源兜底");
assert.deepEqual([...a.chatGroupsOfSourceGroup(item("e", "  ,  ", T))], ["未知来源"], "全空白兜底");

// ── groupVisibleItemsByChat：多群卡两组各出现一次 + 排序 ──
const items = [
  item("newest", "摄影社", T + 300),
  item("multi", "摄影社, 桌游社", T + 200),
  item("old-photo", "摄影社", T + 100),
  item("board", "桌游社", T + 50),
];
const groups = a.groupVisibleItemsByChat(items);
assert.equal(groups.length, 2, "按来源群拆成两组");
assert.deepEqual([...groups.map(g => g.key)], ["摄影社", "桌游社"], "组块按组内最新成员时间降序");
assert.deepEqual([...groups[0].members.map(m => m.id)], ["newest", "multi", "old-photo"], "组内保持传入顺序");
assert.deepEqual([...groups[1].members.map(m => m.id)], ["multi", "board"], "多群卡复制进第二个来源群");

// ── buildListHtmlByChat：块结构 + groupMap 填充 + 计数 ──
stubRenderCard(a);
const html = a.buildListHtmlByChat(items);

assert.ok(html.includes('class="group-collapsed by-chat"'), "≥2 张的群渲染折叠块");
assert.ok(html.includes('data-chat="摄影社"') && html.includes('data-chat="桌游社"'), "组头带 data-chat");
assert.ok(html.includes('class="group-count">3 条<'), "组头条数徽章");
// 折叠态各组只渲染代表卡；「每个来源群各出现一次」体现在分组数据（groupMap/浮层）中
assert.equal(html.split('data-id="multi"').length - 1, 1, "多群卡作为桌游组代表卡出现（摄影社组代表卡是更新的 newest）");
assert.ok(html.includes('data-id="newest"'), "摄影社代表卡=组内最新");
assert.ok(html.indexOf('data-chat="摄影社"') < html.indexOf('data-chat="桌游社"'), "摄影社块（更新）在桌游块之前");
assert.ok(html.includes('data-key="摄影社"'), "代表卡 data-key=群键（供批量/组高亮复用）");

a.updateListCount();
assert.equal(countA.textContent, "共 2 群", "按群口径 N=渲染时统计的来源群数");

// 单成员群平铺不显组头，但计入群数
const soloHtml = a.buildListHtmlByChat([...items, item("solo", "天文台", T + 400)]);
assert.ok(!soloHtml.includes('data-chat="天文台"'), "单卡群不渲染折叠组头");
assert.ok(soloHtml.includes('data-id="solo"'), "单卡群平铺显示");
a.updateListCount();
assert.equal(countA.textContent, "共 3 群", "单卡群计入来源群数");
assert.ok(
  soloHtml.indexOf('data-id="solo"') < soloHtml.indexOf('data-chat="摄影社"'),
  "最新群块排最前",
);

// buildListHtml 按 listMode 分发到按群渲染
const viaBuildList = a.buildListHtml(items);
assert.ok(viaBuildList.includes('class="group-collapsed by-chat"'), "listMode=group 时 buildListHtml 走按群渲染");

// 空视图：计数归零（fullRenderItems 空视图分支）
a.fullRenderItems([]);
a.updateListCount();
assert.equal(countA.textContent, "共 0 群", "空视图按群计数归零");

// ── openChatOverlay：复用主体浮层通道（overlayKey=群名）──
stubRenderCard(a);
a.buildListHtmlByChat(items); // 填充 groupMap
const overlayTitle = A.document.getElementById("group-overlay-title");
let overlayRenderedKey = "";
a.renderOverlayList = (key) => { overlayRenderedKey = key; };
a.syncBodyScrollLock = () => {};
a.pushModalFocus = () => {};
// 先污染主体浮层遗留态，再开群浮层，验证对称清理
overlayTitle.dataset.subject = "某主体";
overlayTitle.classList.add("subject-link");

a.openChatOverlay("桌游社");
assert.equal(overlayRenderedKey, "桌游社", "群浮层以群名为键渲染成员列表");
assert.equal(overlayTitle.textContent, "桌游社", "浮层标题为群名");
assert.equal(overlayTitle.dataset.subject, undefined, "群名不是主体 → 不设 dataset.subject");
assert.equal(overlayTitle.classList.contains("subject-link"), false, "标题不可点进主体时间线");

overlayRenderedKey = "";
a.openChatOverlay("天文台");
assert.equal(overlayRenderedKey, "", "单卡群（成员<2）不开浮层");

// ── renderByChat：全量渲染后按渲染前 diff 重建新卡提示 ──
// 真实 fullRenderItems 参与（renderCard 已 stub）：它负责维护词法 currentItems 基准，
// renderByChat 的 diff 正确性依赖这一契约——stub 掉它基准就断了
stubRenderCard(a);
a.fullRenderItems(items); // 基准 = items
const marks = []; // 每次调用的 added ids
a.markNewCards = (ids) => { marks.push([...ids].map(String)); };
a.notifyNewItems = () => {};
a.updateNewItemsBar = () => {};
const next = [...items, item("fresh-1", "摄影社", T + 500), item("fresh-2", "桌游社", T + 600)];
a.renderByChat(next);
assert.deepEqual(marks[0], ["fresh-1", "fresh-2"], "新卡高亮只含真正新增的 id");

// 连续刷新无新增 → markNewCards 不再被调（marks 长度不增）
a.renderByChat(next);
assert.equal(marks.length, 1, "无新增时不再标记");

// 数据回退（某卡从列表消失再回来）→ 重新视为新增
const shrunk = next.filter(it => it.id !== "fresh-1");
a.renderByChat(shrunk);
a.renderByChat(next);
assert.deepEqual(marks[1], ["fresh-1"], "消失的卡重新出现时视为新增");

// ── 迁移与回退（listMode 为词法 let → 一律经计数文本分流观察）──
// flat → 「共 0 条」；merged/group → 「共 0 组」/「共 0 群」（初始计数全 0 可区分三态）
function assertMode(loadResult, expected) {
  const count = loadResult.document.getElementById("list-count");
  loadResult.sandbox.updateListCount();
  assert.equal(count.textContent, expected, `listMode 初始化应落在 ${expected} 口径`);
}

assertMode(loadWithStore([["briefdesk.collapseGroups", "expanded"]]), "共 0 条", );
assertMode(loadWithStore([["briefdesk.collapseGroups", "collapsed"]]), "共 0 组");
assertMode(loadWithStore([]), "共 0 组");
assertMode(loadWithStore([["briefdesk.listMode", "bogus"]]), "共 0 组");

console.log("ui_group_by_chat_test: all assertions passed");

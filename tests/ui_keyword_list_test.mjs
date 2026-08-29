// 前端关键词清单工厂（createKeywordList）回归测试：订阅与降噪黑名单同源。
//
// 守两件事：
//  1. 两侧唯一的命中差异是 fields——黑名单读 sender_name（按发送人降噪），订阅不读。
//     此前两份独立实现，很容易在改一侧时把这个差异抹平或反向抹平。
//  2. items 引用全程稳定：删除走 splice 原地改。若退回 `items = items.filter(...)`，
//     实例返回的 items 与内部变量会脱钩，外部（updateSubsBadge）读到的是删除前的旧数组。
//
// 数据一律虚构（见 AGENTS.md）。

import assert from "node:assert/strict";

import { loadAppJs } from "./ui_harness.mjs";

const { sandbox } = loadAppJs();

assert.equal(typeof sandbox.createKeywordList, "function", "应能从 app.js 加载 createKeywordList");
assert.equal(typeof sandbox.isSubscribed, "function", "isSubscribed 应为顶层 function");
assert.equal(typeof sandbox.isBlocked, "function", "isBlocked 应为顶层 function");
assert.equal(typeof sandbox.enabledSubKeywords, "function", "enabledSubKeywords 应为顶层 function");

// 让 lsGetJson/lsSetJson 有个可控存储
const store = new Map();
sandbox.localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k),
};

const SUB_FIELDS = ["title", "key_info", "subject", "source_quote"];
const BLOCK_FIELDS = [...SUB_FIELDS, "sender_name"];

function makeList(extra = {}) {
  return sandbox.createKeywordList({
    key: "bd.test.kw",
    ids: "subs",
    idPrefix: "s",
    fields: SUB_FIELDS,
    emptyHtml: "<p>空</p>",
    addedToast: "已添加",
    ...extra,
  });
}

const item = (over = {}) => ({
  title: "社团招新说明会",
  key_info: "周五 19:00 报名截止",
  subject: "招新",
  source_quote: "详情见群公告",
  sender_name: "张三",
  ...over,
});

// ── 命中判定 ──
const list = makeList();
assert.equal(list.matches(item()), false, "空列表应恒不命中（空集 = 不筛选）");

list.items.push({ id: "s1", keywords: "招新", enabled: false });
assert.equal(list.matches(item()), false, "组存在但未启用时不应命中");

list.items[0].enabled = true;
assert.equal(list.matches(item()), true, "启用后应命中 title 里的关键词");
assert.equal(list.matches(item({ title: "读书会", subject: "读书", key_info: "无", source_quote: "无" })), false,
  "四个字段都不含关键词时不应命中");

// 空格分词 = OR
list.items[0].keywords = "读书会 讲座";
assert.equal(list.matches(item({ title: "讲座通知", key_info: "无", subject: "无", source_quote: "无" })), true,
  "空格分词应按 OR 命中任一词");

// 大小写无关
list.items[0].keywords = "Workshop";
assert.equal(list.matches(item({ title: "WORKSHOP 报名", key_info: "无", subject: "无", source_quote: "无" })), true,
  "命中判定应大小写无关");

// ── 两侧唯一差异：sender_name ──
const onlySender = item({ title: "无关", key_info: "无关", subject: "无关", source_quote: "无关", sender_name: "李四" });
const subs = makeList();
subs.items.push({ id: "s1", keywords: "李四", enabled: true });
assert.equal(subs.matches(onlySender), false, "订阅不读 sender_name：仅发送人命中时不应算命中");

const block = makeList({ ids: "block", idPrefix: "b", fields: BLOCK_FIELDS });
block.items.push({ id: "b1", keywords: "李四", enabled: true });
assert.equal(block.matches(onlySender), true, "黑名单读 sender_name：应按发送人命中");

// ── 生产实例对账（关键：上面测的是工厂机制，这里测真实装配）──
// subsList / blockList 是顶层 const，不挂 window，故经真实 localStorage 键 +
// loadSubscriptions()/loadBlocklist() 灌入，再从 isSubscribed/isBlocked 观察。
// 这样连键名与 fields 配置一起对账——只测自建实例的话，改错 subsList 的
// fields（把订阅也读成 sender_name，或把黑名单的 sender_name 删掉）测试全绿。
store.set("briefdesk.subscriptions", JSON.stringify([{ id: "s1", keywords: "李四", enabled: true }]));
store.set("briefdesk.blocklist", JSON.stringify([{ id: "b1", keywords: "李四", enabled: true }]));
sandbox.loadSubscriptions();
sandbox.loadBlocklist();

assert.equal(sandbox.isSubscribed(onlySender), false,
  "生产订阅实例不应读 sender_name（fields 配置被改错时此处必红）");
assert.equal(sandbox.isBlocked(onlySender), true,
  "生产黑名单实例必须读 sender_name（按发送人降噪）");

// 正文关键词两侧都应命中，确认上一条的差异不是"黑名单整体失效"造成的
const inTitle = item({ title: "李四的讲座", sender_name: "王五" });
assert.equal(sandbox.isSubscribed(inTitle), true, "订阅应命中 title 里的关键词");
assert.equal(sandbox.isBlocked(inTitle), true, "黑名单也应命中 title 里的关键词");

// 键名对账：换掉存储内容后重新载入，结果必须跟着变
store.set("briefdesk.subscriptions", JSON.stringify([{ id: "s1", keywords: "读书会", enabled: true }]));
sandbox.loadSubscriptions();
assert.equal(sandbox.isSubscribed(inTitle), false, "改存储后重载，订阅判定应跟着变（键名对账）");
assert.equal(sandbox.enabledSubKeywords(), "读书会", "enabledSubKeywords 应取生产订阅实例的启用组");

// ── enabledKeywords：只取启用且非空的组，空格拼接 ──
const kwList = makeList();
kwList.items.push(
  { id: "s1", keywords: "招新", enabled: true },
  { id: "s2", keywords: "  ", enabled: true },      // 纯空白应被剔除
  { id: "s3", keywords: "讲座", enabled: false },    // 未启用应被剔除
  { id: "s4", keywords: " 报名 ", enabled: true },   // 应 trim
);
assert.equal(kwList.enabledKeywords(), "招新 报名", "应只拼接启用且非空的关键词组");

// ── 存取往返 + items 引用稳定 ──
store.clear();
const rt = makeList({ key: "bd.test.rt" });
rt.items.push({ id: "s1", keywords: "甲", enabled: true }, { id: "s2", keywords: "乙", enabled: false });
rt.save();
assert.deepEqual(JSON.parse(store.get("bd.test.rt")).map(s => s.id), ["s1", "s2"], "save 应写入两条");

// items 是工厂内的 const 数组，语言层面已挡住"换数组"（写不出 items = ...），
// 故此处只断言载入是原地生效的、外部引用能看到新内容。
const itemsRef = rt.items;           // 外部持有的引用（updateSubsBadge 就是这么读的）
rt.load();
assert.equal(rt.items, itemsRef, "load 后 items 应仍是同一个数组对象（原地改）");
assert.equal(itemsRef.length, 2, "外部引用应看到载入后的两条");

// 坏数据被过滤：keywords 非字符串的条目丢弃
store.set("bd.test.rt", JSON.stringify([{ id: "s1", keywords: "甲", enabled: true }, { id: "s2", enabled: true }, null, 42]));
rt.load();
assert.equal(itemsRef.length, 1, "keywords 非字符串/非对象的条目应被丢弃");
assert.equal(itemsRef[0].id, "s1", "应保留合法条目");

// 存储里是非数组时清空而非抛出
store.set("bd.test.rt", JSON.stringify({ notAnArray: true }));
rt.load();
assert.equal(itemsRef.length, 0, "存值非数组时应清空");
assert.equal(rt.items, itemsRef, "清空也必须原地改");

// 读取抛异常（隐私模式）时同样清空且不外泄
rt.items.push({ id: "s9", keywords: "丙", enabled: true });
sandbox.localStorage = { getItem() { throw new Error("SecurityError"); }, setItem() { throw new Error("Quota"); } };
assert.doesNotThrow(() => rt.load(), "存储读失败时 load 不应抛出");
assert.equal(itemsRef.length, 0, "存储读失败时应清空为空列表");
assert.doesNotThrow(() => rt.save(), "存储写失败时 save 不应抛出");

console.log("ui_keyword_list_test: ok");

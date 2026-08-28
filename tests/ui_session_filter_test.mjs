// 会话筛选回归测试（Node vm 加载真实 ui/app.js）。
//
// 设置「群聊筛选」与首次使用向导 step2 是同一套筛选（类型多选 + 消息源多选 +
// 名称搜索 + 时间窗口，四者 AND 叠加；空集 = 不筛选），历史上各自抄了一份实现。
// 现两侧都由 createSessionFilter 产出实例，本测试直接测这唯一实现：
//   1. sessionRowMatches 四个维度各自的语义与叠加（纯函数）
//   2. 工厂实例对列表的实际过滤、全选框三态、时间档位规范化与持久化
// 会话数据全部虚构。

import assert from "node:assert/strict";

import { loadAppJs, makeElement } from "./ui_harness.mjs";

const { sandbox, getElement } = loadAppJs();

// ── 假会话行：结构与两侧渲染出的 .session-row 一致 ──
function makeRow({ isGroup, isOfficial, source, lastActive, text }) {
  const row = makeElement();
  row.dataset = {
    isGroup: isGroup ? "1" : "0",
    isOfficial: isOfficial ? "1" : "0",
    source,
    lastActive: lastActive === undefined ? "" : String(lastActive),
  };
  row.textContent = text;
  return row;
}

const NOW = Date.now() / 1000;
const ROW_SPECS = [
  { isGroup: true, isOfficial: false, source: "weflow", lastActive: NOW - 3600, text: "甲社团群" },
  { isGroup: false, isOfficial: false, source: "weflow", lastActive: NOW - 3600, text: "乙同学私聊" },
  { isGroup: true, isOfficial: true, source: "qqflow", lastActive: NOW - 3600, text: "丙公众号" },
  { isGroup: true, isOfficial: false, source: "qqflow", lastActive: NOW - 86400 * 30, text: "丁沉寂群" },
  { isGroup: true, isOfficial: false, source: "weflow", lastActive: 0, text: "戊无活跃时间群" },
];

// ── 1. sessionRowMatches：四个维度各自的语义 ──
const matches = sandbox.sessionRowMatches;
assert.equal(typeof matches, "function", "应能从 app.js 加载 sessionRowMatches");

const PURE_ROWS = ROW_SPECS.map(makeRow);
const [rowGroup, rowPrivate, rowOfficial, rowStale, rowNoTime] = PURE_ROWS;
const NONE = { types: new Set(), sources: new Set(), query: "", cutoff: 0 };
const visible = (opts) => PURE_ROWS.filter(r => matches(r, { ...NONE, ...opts }));

assert.equal(visible({}).length, 5, "四个维度都不筛选时全部可见");

// 类型：空集 = 全部；official 优先于 group（公众号不算群聊）
assert.deepEqual(visible({ types: new Set(["group"]) }), [rowGroup, rowStale, rowNoTime], "选群聊应排除私聊与公众号");
assert.deepEqual(visible({ types: new Set(["private"]) }), [rowPrivate], "选私聊只剩私聊");
assert.deepEqual(visible({ types: new Set(["official"]) }), [rowOfficial], "选公众号只剩公众号");
assert.deepEqual(
  visible({ types: new Set(["private", "official"]) }),
  [rowPrivate, rowOfficial],
  "类型多选在类型内是 OR"
);

// 消息源：空集 = 全部，源内 OR
assert.deepEqual(visible({ sources: new Set(["qqflow"]) }), [rowOfficial, rowStale], "按源筛选只剩该源");
assert.equal(visible({ sources: new Set(["weflow", "qqflow"]) }).length, 5, "两个源全选等于不筛选");

// 名称搜索：对行文本做包含匹配（调用方负责传小写）
assert.deepEqual(visible({ query: "社团" }), [rowGroup], "搜索命中行文本");
assert.equal(visible({ query: "不存在的词" }).length, 0, "搜索不命中时无可见行");

// 时间窗口：cutoff=0 不筛选；last_active 缺失/为 0 的行在筛选时不可见
assert.deepEqual(
  visible({ cutoff: NOW - 7200 }),
  [rowGroup, rowPrivate, rowOfficial],
  "时间窗口应排除沉寂行与无活跃时间行"
);
assert.equal(visible({ cutoff: 0 }).length, 5, "cutoff=0 表示不按时间筛选");

// 四维叠加是 AND
assert.equal(
  visible({ types: new Set(["group"]), sources: new Set(["weflow"]), query: "社团", cutoff: NOW - 7200 }).length,
  1,
  "四个维度叠加为 AND"
);
assert.equal(
  visible({ types: new Set(["group"]), sources: new Set(["qqflow"]), query: "社团" }).length,
  0,
  "任一维度不命中即不可见"
);

// ── 2. createSessionFilter 实例：真的作用到列表上 ──
const createSessionFilter = sandbox.createSessionFilter;
assert.equal(typeof createSessionFilter, "function", "应能从 app.js 加载 createSessionFilter");

const IDS = {
  list: "t-list",
  selectAll: "t-select-all",
  search: "t-search",
  typeFilter: "t-type",
  sourceFilter: "t-source",
  sourceGroup: "t-source-group",
  timePreset: "t-time-preset",
  timeCustom: "t-time-custom",
};

// 挂载：列表返回假行，全选框统计假复选框（每行一个，checked 状态可控）
const rows = ROW_SPECS.map(makeRow);
const boxes = rows.map(row => {
  const box = makeElement();
  box.closest = () => row;
  return box;
});
const list = getElement(IDS.list);
list.querySelectorAll = (sel) => {
  if (sel.includes("input[data-session-id]")) return boxes;
  if (sel.includes("session-row")) return rows;
  return [];
};
const emptyHintEl = makeElement();
let hintHidden = null;
emptyHintEl.classList.toggle = (_cls, on) => { hintHidden = on; };
list.querySelector = (sel) => (sel === ".t-empty" ? emptyHintEl : null);

const $search = getElement(IDS.search);
const $preset = getElement(IDS.timePreset);
const $custom = getElement(IDS.timeCustom);
const $selectAll = getElement(IDS.selectAll);

// localStorage 桩：验证 storageKey 持久化确实写入
const written = new Map();
sandbox.localStorage.setItem = (k, v) => { written.set(k, String(v)); };

const filter = createSessionFilter({
  ids: IDS,
  storageKey: "test.timeFilter",
  emptyHint: ".t-empty",
});

const shown = () => rows.filter(r => r.style.display !== "none").map(r => r.textContent);
const resetRows = () => rows.forEach(r => { r.style.display = ""; });

// 初始态：不筛选，全部可见
resetRows();
filter.apply();
assert.equal(shown().length, 5, "初始态（空集 + 空搜索 + all）不筛选");
assert.equal(hintHidden, true, "有可见行时空态提示应隐藏");

// 类型多选 + 搜索叠加
filter.state.types.add("group");
$search.value = "群";
resetRows();
filter.apply();
assert.deepEqual(shown(), ["甲社团群", "丁沉寂群", "戊无活跃时间群"], "类型=群聊 + 搜索“群”");

// 搜索词大小写不敏感（apply 内部转小写）
$search.value = "QQ";
resetRows();
filter.apply();
assert.equal(shown().length, 0, "搜索词与行文本都规范化后仍不命中则无可见行");
assert.equal(hintHidden, false, "无可见行时应显示空态提示");

// 时间档位：setTime 规范化 + 持久化 + 控件同步
$search.value = "";
filter.state.types.clear();

filter.setTime(2); // 2 小时窗口
assert.equal(filter.state.time, 2, "正数小时应生效");
assert.equal(written.get("test.timeFilter"), "2", "时间档位应持久化");
assert.deepEqual(shown(), ["甲社团群", "乙同学私聊", "丙公众号"], "2 小时窗口排除沉寂行与无活跃时间行");
assert.equal($preset.value, "custom", "非预设档位下拉应显示“自定义”");
assert.equal($custom.value, "2", "输入框应回填当前档位");

filter.setTime(24); // 命中预设
assert.equal($preset.value, "24", "命中预设时下拉应选中该档");
assert.equal($custom.value, "24", "命中预设时输入框同样回填");

// 非法输入统一退化为 'all'（原向导侧缺这层规范化，输入非数字会得到 NaN cutoff 而全表隐藏）
for (const bad of ["", "abc", "0", "-5", null]) {
  filter.setTime(bad);
  assert.equal(filter.state.time, "all", `非法档位 ${JSON.stringify(bad)} 应退化为 all`);
}
resetRows();
filter.apply();
assert.equal(shown().length, 5, "档位为 all 时不按时间筛选");
assert.equal($preset.value, "all", "all 档位下拉回到“全部”");
assert.equal($custom.value, "", "all 档位输入框清空");

// 全选框三态：仅统计可见行
filter.state.types.add("private"); // 只剩「乙同学私聊」可见
resetRows();
filter.apply();
assert.equal(shown().length, 1, "类型=私聊只剩一行可见");

boxes.forEach(b => { b.checked = false; });
filter.updateSelectAll();
assert.equal($selectAll.checked, false, "无勾选时全选框未选");
assert.equal($selectAll.indeterminate, false, "无勾选时不是半选");

boxes[1].checked = true; // 可见行（私聊）勾上
filter.updateSelectAll();
assert.equal($selectAll.checked, true, "唯一可见行勾上后全选框应全选");
assert.equal($selectAll.indeterminate, false, "全部可见行勾上时不是半选");

boxes[0].checked = true; // 不可见行勾上：不参与统计，三态不变
filter.updateSelectAll();
assert.equal($selectAll.checked, true, "不可见行的勾选不应影响全选框三态");

filter.state.types.clear(); // 五行全可见，此时只有 2 行勾上 → 半选
resetRows();
filter.apply();
assert.equal($selectAll.indeterminate, true, "部分勾选应为半选");
assert.equal($selectAll.checked, false, "部分勾选时不是全选");

// reset：清空多选与搜索框、档位回到指定初始值
filter.state.types.add("group");
filter.state.sources.add("weflow");
$search.value = "群";
filter.reset(48);
assert.equal(filter.state.types.size, 0, "reset 应清空类型多选");
assert.equal(filter.state.sources.size, 0, "reset 应清空源多选");
assert.equal($search.value, "", "reset 应清空搜索框");
assert.equal(filter.state.time, 48, "reset 应把档位设为传入初始值");
assert.equal(written.get("test.timeFilter"), "all", "reset 不写持久化（仍是上一次 setTime 的值）");
filter.reset();
assert.equal(filter.state.time, "all", "reset 不传参时档位回到 all");

// 两个实例状态互不干扰
const other = createSessionFilter({ ids: IDS });
other.state.types.add("official");
assert.equal(filter.state.types.size, 0, "实例间状态互不共享");

console.log("ui_session_filter_test: ok");

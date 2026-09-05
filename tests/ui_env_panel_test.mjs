// 启动配置（设置 → 启动配置/插件）面板逻辑回归（Node vm 加载真实 ui/app.js）。
//
// 守七件事：
// 1. _collectEnvChanges 的布尔分支必须跳过未变化项——此前缺失相等性检查，
//    每次「暂存更改」都会把所有布尔项重写进暂存文件，「没有需要暂存的更改」
//    永不触发，差异计数常驻虚高；
// 2. 分组渲染：未启用插件组默认折叠并在组头标注「未启用」（行内徽章不再
//    重复）；布尔项渲染为开关；已配置（钥匙串）的密钥提供「替换/取消」入口；
//    hidden 标记项（PLUGINS）不渲染进启动配置面板；
// 3. 「保存」按钮的全局合并计数联动（类别/会话/刷新间隔/暂存差异一并计入）。
// 4. 脏检查差异派生：_hasPendingChanges 与保存共用同一 diff 函数——改了
//    又改回、行内动作后的残留不再误问「是否放弃」；类别新增/行内编辑
//    表单打开中（选项 b 保守项）仍视为有未保存修改。
// 5. 插件面板渲染：核心插件无开关（恒启用徽章）、可选插件开关 + 依赖提示。
// 6. 插件开关草稿：依赖/互斥阻止并提示（不隐式改其它插件），通过后仅更新
//    本插件草稿态；_pluginChanges 把草稿 diff 成单个 PLUGINS JSON 值。
// 7. 行内动作（恢复默认/密钥写清）行级贴片：不整面重载 loadEnvConfig——
//    整面重载会丢其它行的未暂存编辑、「插件」面板开关草稿与搜索过滤态。
//
// 数据一律虚构（见 AGENTS.md）。

import assert from "node:assert/strict";
import vm from "node:vm";

import { loadAppJs, makeElement } from "./ui_harness.mjs";

const { sandbox, getElement } = loadAppJs();

// envData 是 app.js 顶层 let（全局词法绑定，不在 globalThis 上），
// 但同一 context 的后续 script 可以对它赋值——借此注入测试夹具。
function setEnvData(data) {
  vm.runInContext(`envData = ${JSON.stringify(data)};`, sandbox);
}

// 构造 _collectEnvChanges 可检索的行桩：dataset.envKey 定位 + 按需的控件桩。
function makeRow(key, { checkbox = null, control = null, text = "" } = {}) {
  const row = makeElement();
  row.dataset.envKey = key;
  row.textContent = text;
  row.querySelector = (sel) => {
    if (sel === 'input[type="checkbox"]') return checkbox;
    if (sel === "[data-env-key]") return control;
    return null;
  };
  return row;
}

// ── 1. 布尔差异检查：未变化项不算差异，变化项产出 "true"/"false" ──
setEnvData({
  filePath: "C:/tmp/settings.env",
  pluginOptions: [],
  items: [
    { key: "CORE_FLAG", type: "boolean", label: "核心开关", plugin: "", staged: null, current: true },
    { key: "ALPHA_FLAG", type: "boolean", label: "插件开关", plugin: "", staged: null, current: false },
  ],
  secrets: [],
});
{
  const rows = [
    makeRow("CORE_FLAG", { checkbox: { checked: true } }),   // 与 current 一致 → 应跳过
    makeRow("ALPHA_FLAG", { checkbox: { checked: true } }),  // current=false → 差异
  ];
  getElement("env-items").querySelectorAll = () => rows;
  assert.deepEqual(
    JSON.parse(JSON.stringify(sandbox._collectEnvChanges())),
    { ALPHA_FLAG: "true" },
    "未变化的布尔项不应进入差异集",
  );

  rows[1].querySelector('input[type="checkbox"]').checked = false;
  assert.deepEqual(
    Object.keys(sandbox._collectEnvChanges()),
    [],
    "全部未变化时差异集应为空（「没有需要暂存的更改」可触发）",
  );
}

// ── 2. 保存按钮联动：差异出现时文案带计数，归零后还原为「保存」──
// （统一保存语义下一次点击提交全部草稿，计数与实际提交项一致）
{
  const rows = [
    makeRow("CORE_FLAG", { checkbox: { checked: true } }),
    makeRow("ALPHA_FLAG", { checkbox: { checked: false } }), // current=false → 无差异
  ];
  getElement("env-items").querySelectorAll = () => rows;
  const saveEl = getElement("settings-save");
  sandbox._updateSaveButton();
  assert.equal(saveEl.textContent, "保存", "无差异时应显示纯「保存」");

  rows[1].querySelector('input[type="checkbox"]').checked = true;
  sandbox._updateSaveButton();
  assert.equal(saveEl.textContent, "保存（1 项）", "有差异时应显示差异数");
}

// ── 3. 分组渲染：折叠、组头徽章、行内徽章去重、布尔开关 ──
setEnvData({
  filePath: "C:/tmp/settings.env",
  pluginOptions: ["alpha"],
  items: [
    { key: "CORE_FLAG", type: "boolean", label: "核心开关", plugin: "", staged: null, current: true },
    { key: "ALPHA_MODE", type: "boolean", label: "Alpha 开关", plugin: "alpha", staged: null, current: false, pluginStatus: "disabled" },
  ],
  secrets: [],
});
sandbox.renderEnvConfig();
{
  const html = getElement("env-items").innerHTML;
  assert.ok(html.includes('data-env-default-open="1"'), "core 组应默认展开");
  assert.ok(html.includes('data-env-default-open="0"'), "未启用插件组应默认折叠");
  assert.ok(html.includes('<summary class="env-group-head">'), "配置组应为 details 折叠结构（summary 挂组头类以启用 flex 并去除 UA 标记）");
  assert.ok(html.includes('<span class="env-badge">未启用</span>'), "未启用组应在组头标注");
  assert.ok(!html.includes("插件未启用"), "组头已标注时行内不应重复徽章");
  assert.ok(html.includes('class="env-switch"'), "布尔项应渲染为开关");
}

// ── 4. 密钥渲染：钥匙串已配置给「替换」入口且藏起输入框 ──
setEnvData({
  filePath: "C:/tmp/settings.env",
  pluginOptions: [],
  items: [],
  secrets: [
    { name: "ALPHA_TOKEN", label: "Alpha 令牌", plugin: "", configured: true, keyringConfigured: true },
    { name: "BETA_KEY", label: "Beta 密钥", plugin: "", configured: false },
  ],
});
sandbox.renderEnvConfig();
{
  const html = getElement("env-secrets").innerHTML;
  assert.ok(html.includes('data-sec-replace="ALPHA_TOKEN"'), "已配置密钥应有「替换」入口");
  assert.ok(html.includes('data-sec-clear="ALPHA_TOKEN"'), "已配置密钥应有「清除」");
  // 「替换/清除」与输入框行内的「保存/取消」同款轮廓按钮（行头右置与
  // 防平分推力的对齐规则按该类名生效）
  assert.ok(html.includes('class="settings-outline-btn" data-sec-replace="ALPHA_TOKEN"'), "「替换」应与「保存/取消」同款轮廓按钮");
  assert.ok(html.includes('class="settings-outline-btn" data-sec-clear="ALPHA_TOKEN"'), "「清除」应与「保存/取消」同款轮廓按钮");
  assert.ok(html.includes('class="env-secret-input hidden"'), "已配置密钥的输入框应藏起");
  assert.ok(html.includes('class="env-secret-input">'), "未配置密钥的输入框应直接可见");
  assert.ok(!html.includes('data-sec-replace="BETA_KEY"'), "未配置密钥无需「替换」入口");
  // 「取消」只属于钥匙串托管行：它还原的是「替换」展开态；非托管行的
  // 输入框是常驻配置入口，收起就没有配置门路了
  const replaceIdx = html.indexOf('data-sec-replace="ALPHA_TOKEN"');
  const cancelIdx = html.indexOf('data-sec-cancel="ALPHA_TOKEN"');
  assert.ok(cancelIdx !== -1, "钥匙串托管密钥应有「取消」");
  assert.ok(cancelIdx > replaceIdx, "「取消」应在「替换」之后渲染（输入框行内、保存旁）");
  assert.ok(html.indexOf('data-sec-set="ALPHA_TOKEN"') < cancelIdx, "「取消」应与「保存」同在输入框行内");
  assert.ok(!html.includes('data-sec-cancel="BETA_KEY"'), "非钥匙串密钥不应有「取消」");
}

// ── 5. 搜索过滤：行级显隐、组级整组显隐、details 组自动展开/还原 ──
{
  // instanceof 判定发生在 vm realm：类与实例都必须在该 realm 内创建
  vm.runInContext("globalThis.HTMLDetailsElement = class HTMLDetailsElement {};", sandbox);
  const Details = sandbox.HTMLDetailsElement;

  const hitRow = makeRow("ALPHA_MODE", { text: "Alpha 开关 提示一" });
  const missRow = makeRow("BETA_MODE", { text: "Beta 开关 提示二" });
  const hitGroup = new Details();
  hitGroup.classList = makeElement().classList;
  hitGroup.dataset = { envDefaultOpen: "0" };
  hitGroup.querySelectorAll = () => [hitRow];
  const missGroup = new Details();
  missGroup.classList = makeElement().classList;
  missGroup.dataset = { envDefaultOpen: "1" };
  missGroup.querySelectorAll = () => [missRow];

  getElement("env-items").querySelectorAll = (sel) =>
    sel === ".env-group" ? [hitGroup, missGroup] : [];
  getElement("env-secrets").querySelectorAll = () => [];

  const filterEl = getElement("env-filter");
  filterEl.value = "alpha";
  sandbox._applyEnvFilter();
  assert.equal(hitRow.classList.contains("hidden"), false, "命中行应可见");
  assert.equal(missRow.classList.contains("hidden"), true, "未命中行应隐藏");
  assert.equal(hitGroup.classList.contains("hidden"), false, "有命中的组应可见");
  assert.equal(hitGroup.open, true, "过滤时应自动展开命中组");
  assert.equal(missGroup.classList.contains("hidden"), true, "无命中的组应整组隐藏");

  filterEl.value = "";
  sandbox._applyEnvFilter();
  assert.equal(hitRow.classList.contains("hidden"), false, "清空后全部行可见");
  assert.equal(missRow.classList.contains("hidden"), false, "清空后全部行可见");
  assert.equal(hitGroup.open, false, "清空后按默认展开态还原（该组默认折叠）");
  assert.equal(missGroup.open, true, "清空后按默认展开态还原（该组默认展开）");
}

// ── 6. 脏检查差异派生：_hasPendingChanges 按五类草稿源的真实差异判定 ──
// 与保存路径共用同一 diff 函数：改了又改回/行内动作后的残留不再误报；
// 选项 b 保守项：类别新增/行内编辑表单打开中即视为有未保存修改。
{
  // 类别/会话草稿源注入（app.js 顶层 let，同一 context 的后续 script 可赋值）
  const catRow = { key: "c1", id: 7, name: "类别甲", prompt: "p", color: "#111111", enabled: 1, item_count: 0 };
  vm.runInContext(
    `catDraft = [${JSON.stringify(catRow)}]; catOriginal = [${JSON.stringify(catRow)}];`
      + `catDeleted = []; sessionOriginal = [{ source: "qq", session_id: "123", enabled: 0 }];`,
    sandbox,
  );
  const cb = { checked: false, dataset: { source: "qq", sessionId: "123" } };
  getElement("session-list").querySelectorAll = () => [cb];

  setEnvData({
    filePath: "C:/tmp/settings.env",
    pluginOptions: [],
    items: [
      { key: "ALPHA_KEY", type: "text", label: "Alpha 项", plugin: "", staged: null, current: "base" },
    ],
    secrets: [],
    plugins: [
      { name: "weflow", version: "1.0.0", dependencies: [], conflicts: [], core: false, enabled: false, status: "disabled", reason: "" },
    ],
  });
  sandbox._pluginSets();
  const rows = [makeRow("ALPHA_KEY", { control: { value: "base" } })];
  getElement("env-items").querySelectorAll = () => rows;
  const refreshEl = getElement("refresh-interval");
  refreshEl.value = vm.runInContext("String(refreshIntervalSec)", sandbox);

  // harness 的 document.querySelector 恒返回真值元素，会让 _inlineEditorOpen
  // 恒真——本段专用桩化：行内编辑表默认收起，选项 b 断言时再模拟打开
  const realQuery = sandbox.document.querySelector;
  const editorSel = ".cat-edit-form:not(.hidden), #cat-add-form:not(.hidden)";
  sandbox.document.querySelector = (sel) => (sel === editorSel ? null : realQuery(sel));

  assert.equal(sandbox._hasPendingChanges(), false, "初始无草稿不脏");

  // env 输入：改了又改回 → 不脏（旧「任一事件即置位」布尔机制会误报）
  rows[0].querySelector("[data-env-key]").value = "changed";
  assert.ok(sandbox._hasPendingChanges(), "env 真实差异 → 脏");
  rows[0].querySelector("[data-env-key]").value = "base";
  assert.equal(sandbox._hasPendingChanges(), false, "env 改回原值 → 不脏");

  // 插件开关：拨开再拨回 → 不脏
  sandbox._onPluginToggle("weflow", true, { checked: false });
  assert.ok(sandbox._hasPendingChanges(), "插件草稿 → 脏");
  sandbox._onPluginToggle("weflow", false, { checked: true });
  assert.equal(sandbox._hasPendingChanges(), false, "插件拨回 → 不脏");

  // 类别：改名后改回 → 不脏
  vm.runInContext("catDraft[0].name = '类别乙'", sandbox);
  assert.ok(sandbox._hasPendingChanges(), "类别真实差异 → 脏");
  vm.runInContext("catDraft[0].name = '类别甲'", sandbox);
  assert.equal(sandbox._hasPendingChanges(), false, "类别改回 → 不脏");

  // 会话勾选：翻转后翻回 → 不脏
  cb.checked = true;
  assert.ok(sandbox._hasPendingChanges(), "会话勾选真实差异 → 脏");
  cb.checked = false;
  assert.equal(sandbox._hasPendingChanges(), false, "会话勾选改回 → 不脏");

  // 刷新间隔：改了又改回 → 不脏
  refreshEl.value = "999";
  assert.ok(sandbox._hasPendingChanges(), "刷新间隔真实差异 → 脏");
  refreshEl.value = vm.runInContext("String(refreshIntervalSec)", sandbox);
  assert.equal(sandbox._hasPendingChanges(), false, "刷新间隔改回 → 不脏");

  // 选项 b 保守项：行内编辑表单打开中 → 脏（未确认输入不进保存链路，但
  // 静默丢弃半截输入属可避免错误，宁多问一次）
  sandbox.document.querySelector = (sel) => (sel === editorSel ? makeElement() : realQuery(sel));
  assert.ok(sandbox._hasPendingChanges(), "行内编辑表单打开中 → 脏（选项 b）");
  // 段尾保持「编辑态收起」桩（还原 harness 默认会让 _inlineEditorOpen 恒真）
  sandbox.document.querySelector = (sel) => (sel === editorSel ? null : realQuery(sel));
  assert.equal(sandbox._hasPendingChanges(), false, "表单收起后恢复按真实差异判定");
}

// ── 7. hidden 标记项（PLUGINS）不渲染进启动配置面板（由插件面板编辑）──
setEnvData({
  filePath: "C:/tmp/settings.env",
  pluginOptions: [],
  items: [
    { key: "PLUGINS", type: "multi", label: "启用的可选插件", plugin: "", staged: null, current: [], hidden: true },
    { key: "OTHER_KEY", type: "text", label: "普通项", plugin: "", staged: null, current: "x" },
  ],
  secrets: [],
});
sandbox.renderEnvConfig();
{
  const html = getElement("env-items").innerHTML;
  assert.ok(!html.includes('data-env-key="PLUGINS"'), "hidden 项（PLUGINS）不应渲染进启动配置面板");
  assert.ok(html.includes('data-env-key="OTHER_KEY"'), "未标记 hidden 的项照常渲染");
}

// ── 8/9 共用夹具：核心行无开关、可选行开关 + 依赖提示；开关草稿校验 ──
setEnvData({
  filePath: "C:/tmp/settings.env",
  pluginOptions: [],
  items: [],
  secrets: [],
  plugins: [
    { name: "ai_provider", version: "1.0.0", dependencies: [], conflicts: [], core: true, enabled: true, status: "loaded", reason: "" },
    { name: "coresink", version: "1.0.0", dependencies: ["weflow"], conflicts: [], core: true, enabled: true, status: "loaded", reason: "" },
    { name: "pending", version: "1.0.0", dependencies: [], conflicts: [], core: false, enabled: false, status: "discovered", reason: "" },
    { name: "weflow", version: "1.0.0", dependencies: [], conflicts: ["weflow-legacy"], core: false, enabled: true, status: "loaded", reason: "" },
    { name: "weflow-legacy", version: "1.0.0", dependencies: [], conflicts: ["weflow"], core: false, enabled: false, status: "disabled", reason: "未启用：在 PLUGINS 中列出或经「插件」面板开关即可启用" },
    { name: "qqflow", version: "1.0.1", dependencies: [], conflicts: [], core: false, enabled: false, status: "disabled", reason: "" },
    { name: "stage", version: "1.0.0", dependencies: ["src"], conflicts: [], core: false, enabled: false, status: "disabled", reason: "" },
    { name: "src", version: "1.0.0", dependencies: [], conflicts: [], core: false, enabled: false, status: "disabled", reason: "" },
    { name: "dependent", version: "1.0.0", dependencies: ["weflow"], conflicts: [], core: false, enabled: true, status: "loaded", reason: "" },
  ],
});
sandbox._pluginSets();
sandbox.renderPluginToggles();
{
  const html = getElement("plugins-list").innerHTML;
  assert.ok(html.includes("核心插件（始终启用）"), "核心分组标题应渲染");
  assert.ok(html.includes("核心 · 始终启用"), "核心插件应带恒启用徽章");
  assert.ok(!html.includes('data-plugin-toggle="ai_provider"'), "核心插件不应渲染开关");
  assert.ok(html.includes('data-plugin-toggle="weflow" checked'), "已启用可选插件开关应为勾选态");
  assert.ok(html.includes('data-plugin-toggle="qqflow"'), "禁用可选插件也渲染开关");
  assert.ok(html.includes("依赖："), "依赖提示应渲染");
  assert.ok(html.includes("未装配"), "discovered 状态应映射为中文「未装配」（warn 色而非红色不可用）");
  assert.ok(!html.includes("重启后启用"), "未改草稿时不应有草稿徽章");
}

// ── 9. 插件开关草稿：阻止并提示 + 通过后仅改本插件 + PLUGINS 差异 ──
{
  const toasts = [];
  sandbox.showToast = (msg) => toasts.push(msg);
  getElement("env-items").querySelectorAll = () => []; // 成功路径联动 _updateSaveButton 用

  // 互斥阻止：weflow 已启用，启用 weflow-legacy 被拒
  const blockedInput = { checked: false };
  sandbox._onPluginToggle("weflow-legacy", true, blockedInput);
  assert.equal(blockedInput.checked, false, "被拒后开关应回弹");
  assert.equal(toasts.length, 1, "被拒应提示一次");
  assert.ok(toasts[0].includes("互斥"), "互斥提示应说明原因");
  assert.deepEqual(JSON.parse(JSON.stringify(sandbox._pluginChanges())), {}, "被拒不改草稿（差异仍为空）");

  // 缺依赖阻止：stage 依赖 src（未启用）
  sandbox._onPluginToggle("stage", true, { checked: true });
  assert.ok(toasts.at(-1).includes("需先启用 src"), "缺依赖应提示先启用什么");
  assert.deepEqual(JSON.parse(JSON.stringify(sandbox._pluginChanges())), {}, "缺依赖不改草稿");

  // 禁用被下游阻止：weflow 被启用中的可选插件 dependent 依赖，
  // 且被核心插件 coresink 依赖——两类下游都点名
  sandbox._onPluginToggle("weflow", false, { checked: true });
  assert.ok(toasts.at(-1).includes("核心插件 coresink 依赖它"), "核心下游应单独点名");
  assert.ok(toasts.at(-1).includes("请先禁用 dependent"), "可选下游应提示先禁用");
  assert.deepEqual(JSON.parse(JSON.stringify(sandbox._pluginChanges())), {}, "禁用被阻不改草稿");

  // 通过：启用无冲突无依赖的 qqflow → 草稿 diff 成单个 PLUGINS JSON
  sandbox._onPluginToggle("qqflow", true, { checked: false });
  assert.deepEqual(
    JSON.parse(JSON.stringify(sandbox._pluginChanges())),
    { PLUGINS: JSON.stringify(["dependent", "qqflow", "weflow"]) },
    "草稿应 diff 成排序稳定的 PLUGINS 期望列表",
  );
  const html = getElement("plugins-list").innerHTML;
  assert.ok(html.includes("重启后启用"), "草稿变更应显示「重启后启用」徽章");

  // 回退草稿：再次关闭 qqflow → 与基准一致，差异清空
  sandbox._onPluginToggle("qqflow", false, { checked: true });
  assert.deepEqual(JSON.parse(JSON.stringify(sandbox._pluginChanges())), {}, "草稿回退后差异应清空");
}

// ── 10. 行内动作行级贴片：不整面重载（保住插件草稿），只更新受影响行 ──
{
  // 行桩：outerHTML 赋值记录（makeElement 无该属性，defineProperty 捕获）
  const rowStub = makeElement();
  let rowHtml = "";
  Object.defineProperty(rowStub, "outerHTML", {
    set(v) { rowHtml = v; },
    get() { return rowHtml; },
  });

  setEnvData({
    filePath: "C:/tmp/settings.env",
    pluginOptions: [],
    items: [
      { key: "ALPHA_KEY", type: "text", label: "Alpha 项", plugin: "", staged: "old", current: "base", source: "override" },
    ],
    secrets: [],
    plugins: [
      { name: "weflow", version: "1.0.0", dependencies: [], conflicts: [], core: false, enabled: false, status: "disabled", reason: "" },
    ],
  });
  sandbox._pluginSets();
  sandbox.renderPluginToggles();

  const realQuery = sandbox.document.querySelector;
  sandbox.document.querySelector = (sel) =>
    sel === '#env-items .env-row[data-env-key="ALPHA_KEY"]' ? rowStub : realQuery(sel);
  const filterCalls = [];
  const realFilter = sandbox._applyEnvFilter;
  sandbox._applyEnvFilter = () => { filterCalls.push(1); realFilter(); };
  const loadCalls = [];
  const realLoad = sandbox.loadEnvConfig;
  sandbox.loadEnvConfig = () => { loadCalls.push(1); return realLoad(); };
  // harness 无 fetch 桩：供 reqJson 走通成功路径
  sandbox.fetch = async () => ({
    ok: true,
    json: async () => ({ ok: true, items: { ALPHA_KEY: { staged: null, source: "default" } } }),
  });

  // 前置：拨一个插件开关制造草稿
  sandbox._onPluginToggle("weflow", true, { checked: false });
  assert.deepEqual(
    JSON.parse(JSON.stringify(sandbox._pluginChanges())),
    { PLUGINS: JSON.stringify(["weflow"]) },
    "前置：插件草稿已建立",
  );

  await sandbox.restoreEnvKey("ALPHA_KEY");

  assert.equal(loadCalls.length, 0, "行内恢复默认不应整面重载 loadEnvConfig");
  assert.deepEqual(
    JSON.parse(JSON.stringify(sandbox._pluginChanges())),
    { PLUGINS: JSON.stringify(["weflow"]) },
    "行内动作后「插件」面板开关草稿应保留",
  );
  assert.ok(rowHtml.includes('data-env-key="ALPHA_KEY"'), "受影响行应被重绘");
  assert.ok(!rowHtml.includes("恢复默认"), "恢复后该行不应再有「恢复默认」按钮（staged 已清）");
  assert.ok(filterCalls.length > 0, "贴片后应重放搜索过滤");

  sandbox.document.querySelector = realQuery;
  sandbox._applyEnvFilter = realFilter;
  sandbox.loadEnvConfig = realLoad;
  delete sandbox.fetch;
}

// ── 11. 密钥行贴片：两布尔更新 + 按 data-sec-name 定位重绘 ──
{
  const secRow = makeElement();
  let secHtml = "";
  Object.defineProperty(secRow, "outerHTML", {
    set(v) { secHtml = v; },
    get() { return ""; },
  });

  setEnvData({
    filePath: "C:/tmp/settings.env",
    pluginOptions: [],
    items: [],
    secrets: [
      { name: "ALPHA_TOKEN", label: "Alpha 令牌", plugin: "", configured: false, keyringConfigured: false },
    ],
    plugins: [],
  });

  const realQuery = sandbox.document.querySelector;
  sandbox.document.querySelector = (sel) =>
    sel === '#env-secrets .env-row[data-sec-name="ALPHA_TOKEN"]' ? secRow : realQuery(sel);
  sandbox._patchSecretRow("ALPHA_TOKEN", { configured: true, keyringConfigured: true });
  assert.ok(secHtml.includes('data-sec-name="ALPHA_TOKEN"'), "密钥行应按 data-sec-name 重绘");
  assert.ok(secHtml.includes("已配置（钥匙串）"), "写入后应显示钥匙串已配置徽章");

  secHtml = "";
  sandbox._patchSecretRow("ALPHA_TOKEN", { configured: false, keyringConfigured: false });
  assert.ok(!secHtml.includes("已配置"), "清除后不应再显示已配置徽章");
  assert.ok(!secHtml.includes("data-sec-replace"), "未托管行不应有「替换」入口");

  sandbox.document.querySelector = realQuery;
}

// ── 12. 保存按钮合并计数：env 差异 + 插件草稿一并计入（与合并提交一致） ──
{
  setEnvData({
    filePath: "C:/tmp/settings.env",
    pluginOptions: [],
    items: [
      { key: "CORE_FLAG", type: "boolean", label: "核心开关", plugin: "", staged: null, current: false },
    ],
    secrets: [],
    plugins: [
      { name: "weflow", version: "1.0.0", dependencies: [], conflicts: [], core: false, enabled: false, status: "disabled", reason: "" },
    ],
  });
  sandbox._pluginSets();
  sandbox.renderPluginToggles();
  const rows = [makeRow("CORE_FLAG", { checkbox: { checked: true } })]; // current=false → env 差异 1 项
  getElement("env-items").querySelectorAll = () => rows;
  const saveEl = getElement("settings-save");

  sandbox._updateSaveButton();
  assert.equal(saveEl.textContent, "保存（1 项）", "仅 env 差异 → 1 项");

  // 拨一个插件开关：成功路径内部会联动刷新保存按钮
  sandbox._onPluginToggle("weflow", true, { checked: false });
  assert.equal(saveEl.textContent, "保存（2 项）", "env 差异 + 插件草稿应合并计数（与统一保存提交口径一致）");

  // 草稿回退 → 计数回落到仅 env 差异
  sandbox._onPluginToggle("weflow", false, { checked: true });
  assert.equal(saveEl.textContent, "保存（1 项）", "插件草稿回退后计数回落");
}

// ── 13. 统一保存：一次点击提交全部草稿（暂存 PUT + 类别 ops 同一次生效）──
{
  setEnvData({
    filePath: "C:/tmp/settings.env",
    pluginOptions: [],
    items: [
      { key: "ALPHA_KEY", type: "text", label: "Alpha 项", plugin: "", staged: null, current: "base" },
    ],
    secrets: [],
    plugins: [],
  });
  sandbox._pluginSets();
  const rows = [makeRow("ALPHA_KEY", { control: { value: "changed" } })];
  getElement("env-items").querySelectorAll = () => rows;
  // 类别草稿：名字与基线不同 → 一个 update op
  vm.runInContext(
    `catDraft = [{ key: "c1", id: 7, name: "类别乙", prompt: "p", color: "#111111", enabled: 1, item_count: 0 }];`
      + `catOriginal = [{ key: "c1", id: 7, name: "类别甲", prompt: "p", color: "#111111", enabled: 1, item_count: 0 }];`
      + `catDeleted = []; sessionOriginal = [];`,
    sandbox,
  );
  getElement("session-list").querySelectorAll = () => [];

  const calls = [];
  sandbox.fetch = async (url, opts = {}) => {
    calls.push({ url: String(url), method: opts.method || "GET" });
    if (url === "/api/settings/env" && (opts.method || "GET") === "PUT") {
      return { ok: true, json: async () => ({ ok: true, items: {} }) };
    }
    if (url === "/api/status") return { ok: true, json: async () => ({ syncing: false }) };
    if (String(url).startsWith("/api/categories/")) return { ok: true, json: async () => ({}) };
    return { ok: false, status: 404, json: async () => ({}) };
  };
  const toasts = [];
  sandbox.showToast = (msg) => toasts.push(msg);
  sandbox.startRefreshTimer = () => {};
  sandbox.fetchData = () => {};

  await sandbox.saveAllSettings();

  assert.ok(
    calls.some(c => c.url === "/api/settings/env" && c.method === "PUT"),
    "暂存差异应提交（旧面板分流下类别面板点保存不会提交暂存）",
  );
  assert.ok(
    calls.some(c => c.url.includes("/api/categories/7/update")),
    "类别 ops 应同一次点击提交",
  );
  assert.ok(
    getElement("settings-modal").classList.contains("hidden"),
    "双改共存保存成功后应关闭弹窗",
  );
  assert.ok(toasts.some(t => t.includes("已暂存")), "双改共存应同时提示暂存待重启");

  // 皆无更改：静默关闭（「保存」即完成键，不再 toast「没有需要暂存的更改」）
  rows[0].querySelector("[data-env-key]").value = "base";
  vm.runInContext("catDraft[0].name = '类别甲'", sandbox);
  const realQuery13 = sandbox.document.querySelector;
  sandbox.document.querySelector = (sel) =>
    sel === ".cat-edit-form:not(.hidden), #cat-add-form:not(.hidden)" ? null : realQuery13(sel);
  calls.length = 0;
  const modalEl = getElement("settings-modal");
  modalEl.classList.remove("hidden");
  await sandbox.saveAllSettings();
  assert.equal(modalEl.classList.contains("hidden"), true, "皆无更改时点保存应静默关闭");
  assert.deepEqual(calls.filter(c => c.method === "PUT" || c.url.includes("/api/categories")), [],
    "皆无更改时不应发出任何提交请求");
  sandbox.document.querySelector = realQuery13;
}

// ── 14. 中止路径：暂存成功遇名称冲突仍需明示；警示确认取消不发 PUT ──
{
  // 夹具：env 一项差异 + 类别「改名甲→乙」撞「新建乙」（名称冲突 → ops = null）
  setEnvData({
    filePath: "C:/tmp/settings.env",
    pluginOptions: [],
    items: [
      { key: "ALPHA_KEY", type: "text", label: "Alpha 项", plugin: "", staged: null, current: "base" },
    ],
    secrets: [],
    plugins: [],
  });
  sandbox._pluginSets();
  const rows = [makeRow("ALPHA_KEY", { control: { value: "changed" } })];
  getElement("env-items").querySelectorAll = () => rows;
  vm.runInContext(
    `catDraft = [{ key: "c1", id: 7, name: "类别乙", prompt: "p", color: "#111111", enabled: 1, item_count: 0 },`
      + `{ key: "n1", id: null, name: "类别乙", prompt: "", color: "#6B7280", enabled: 1, item_count: 0 }];`
      + `catOriginal = [{ key: "c1", id: 7, name: "类别甲", prompt: "p", color: "#111111", enabled: 1, item_count: 0 }];`
      + `catDeleted = []; sessionOriginal = [];`,
    sandbox,
  );
  getElement("session-list").querySelectorAll = () => [];

  const calls = [];
  sandbox.fetch = async (url, opts = {}) => {
    calls.push({ url: String(url), method: opts.method || "GET" });
    if (url === "/api/settings/env" && (opts.method || "GET") === "PUT") {
      return { ok: true, json: async () => ({ ok: true, items: {} }) };
    }
    // 名称冲突中止后的 loadEnvConfig 重载
    if (url === "/api/settings/env") {
      return {
        ok: true,
        json: async () => ({ ok: true, filePath: "C:/tmp/settings.env", items: [], secrets: [], plugins: [], pluginOptions: [] }),
      };
    }
    if (url === "/api/status") return { ok: true, json: async () => ({ syncing: false }) };
    return { ok: false, status: 404, json: async () => ({}) };
  };
  const toasts = [];
  sandbox.showToast = (msg) => toasts.push(msg);
  sandbox.startRefreshTimer = () => {};
  sandbox.fetchData = () => {};
  const modalEl = getElement("settings-modal");
  modalEl.classList.remove("hidden");

  await sandbox.saveAllSettings();

  assert.ok(calls.some(c => c.url === "/api/settings/env" && c.method === "PUT"), "暂存应已提交");
  assert.ok(!calls.some(c => String(c.url).startsWith("/api/categories")),
    "名称冲突中止时类别 ops 不应执行");
  assert.ok(toasts.some(t => t.includes("类别名称冲突")), "名称冲突应有提示");
  assert.ok(toasts.some(t => t.includes("已暂存")), "中止时已提交的暂存仍应明示（不因中止被吞掉）");
  assert.equal(modalEl.classList.contains("hidden"), false, "中止时弹窗应保留");

  // 警示确认取消 → aborted：confirm 返回 false 时不发 PUT
  setEnvData({
    filePath: "C:/tmp/settings.env",
    pluginOptions: [],
    items: [
      { key: "SERVER_PORT", type: "number", label: "服务端口", plugin: "", staged: null, current: 3000, warn: "重启后访问地址将变为新端口" },
    ],
    secrets: [],
    plugins: [],
  });
  sandbox._pluginSets();
  getElement("env-items").querySelectorAll = () => [makeRow("SERVER_PORT", { control: { value: "3001" } })];
  const realConfirm = sandbox.confirm;
  let confirmAsked = false;
  sandbox.confirm = () => { confirmAsked = true; return false; };
  calls.length = 0;
  assert.equal(await sandbox.stagePendingEnvChanges(), "aborted", "警示确认取消应返回 aborted");
  assert.ok(confirmAsked, "含 warn 项的暂存应先弹确认");
  assert.ok(!calls.some(c => c.method === "PUT"), "确认取消后不应发 PUT");
  delete sandbox.confirm;
}

console.log("ui_env_panel_test: all assertions passed");

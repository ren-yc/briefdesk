// 启动配置（设置 → 启动配置/插件）面板逻辑回归（Node vm 加载真实 ui/app.js）。
//
// 守六件事：
// 1. _collectEnvChanges 的布尔分支必须跳过未变化项——此前缺失相等性检查，
//    每次「暂存更改」都会把所有布尔项重写进暂存文件，「没有需要暂存的更改」
//    永不触发，差异计数常驻虚高；
// 2. 分组渲染：未启用插件组默认折叠并在组头标注「未启用」（行内徽章不再
//    重复）；布尔项渲染为开关；已配置（钥匙串）的密钥提供「替换/取消」入口；
//    hidden 标记项（PLUGINS）不渲染进启动配置面板；
// 3. 「暂存更改」按钮的脏计数联动。
// 4. 脏检查排除清单：非草稿控件（即时提交/视图过滤）不置位脏标记，否则
//    密钥框打字后点弹窗取消会被误问「是否放弃修改」。
// 5. 插件面板渲染：核心插件无开关（恒启用徽章）、可选插件开关 + 依赖提示。
// 6. 插件开关草稿：依赖/互斥阻止并提示（不隐式改其它插件），通过后仅更新
//    本插件草稿态；_pluginChanges 把草稿 diff 成单个 PLUGINS JSON 值。
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
// （env 面板复用弹窗底部全局「保存」按钮，点击语义按面板分流）
{
  const rows = [
    makeRow("CORE_FLAG", { checkbox: { checked: true } }),
    makeRow("ALPHA_FLAG", { checkbox: { checked: false } }), // current=false → 无差异
  ];
  getElement("env-items").querySelectorAll = () => rows;
  const saveEl = getElement("settings-save");
  sandbox._updateEnvSaveButton();
  assert.equal(saveEl.textContent, "保存", "无差异时应显示纯「保存」");

  rows[1].querySelector('input[type="checkbox"]').checked = true;
  sandbox._updateEnvSaveButton();
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

// ── 6. 脏检查排除清单：非草稿控件不置位脏标记 ──
// 真实 closest 一次收到完整选择器串（_NON_DRAFT_SELECTOR），桩据此校验
// 清单确实覆盖每个关键控件，同时验证谓词的分支行为。
{
  for (const frag of [
    ".env-secret-input",   // 密钥框：行内「保存」即时写钥匙串
    "#env-filter",         // 启动配置搜索：纯视图过滤
    "#subs-add-kw",        // 订阅添加：点「添加」即写 localStorage
    "#block-add-kw",       // 黑名单添加：同上
    ".subs-enabled",       // 订阅启用勾选：勾选即写 localStorage
    ".block-enabled",      // 黑名单启用勾选：同上
    "#notify-mode",        // 通知模式：变更即写 localStorage
    "#session-search",     // 群聊筛选搜索：纯视图过滤
    "#restore-file",       // 备份文件：选完即走立即恢复流程
  ]) {
    const probe = { closest: (sel) => (typeof sel === "string" && sel.includes(frag) ? {} : null) };
    assert.equal(sandbox._isNonDraftSettingsControl(probe), true, `排除清单应覆盖非草稿控件 ${frag}`);
  }
  const draftControl = { closest: () => null }; // 草稿控件（如刷新间隔、类别编辑）
  assert.equal(sandbox._isNonDraftSettingsControl(draftControl), false, "草稿控件不命中排除清单，应继续置位脏标记");
  assert.equal(sandbox._isNonDraftSettingsControl(null), false, "空目标应安全返回 false");
  assert.equal(sandbox._isNonDraftSettingsControl({}), false, "无 closest 的目标应安全返回 false");
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
  assert.ok(!html.includes("重启后启用"), "未改草稿时不应有草稿徽章");
}

// ── 9. 插件开关草稿：阻止并提示 + 通过后仅改本插件 + PLUGINS 差异 ──
{
  const toasts = [];
  sandbox.showToast = (msg) => toasts.push(msg);
  getElement("env-items").querySelectorAll = () => []; // 成功路径联动 _updateEnvSaveButton 用

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

console.log("ui_env_panel_test: all assertions passed");

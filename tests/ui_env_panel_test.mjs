// 启动配置（设置 → 启动配置）面板逻辑回归（Node vm 加载真实 ui/app.js）。
//
// 守三件事：
// 1. _collectEnvChanges 的布尔分支必须跳过未变化项——此前缺失相等性检查，
//    每次「暂存更改」都会把所有布尔项重写进暂存文件，「没有需要暂存的更改」
//    永不触发，差异计数常驻虚高；
// 2. 分组渲染：未启用插件组默认折叠并在组头标注「未启用」（行内徽章不再
//    重复）；布尔项渲染为开关；已配置（钥匙串）的密钥提供「替换」入口；
// 3. 「暂存更改」按钮的脏计数联动。
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
  assert.ok(html.includes('class="env-secret-input hidden"'), "已配置密钥的输入框应藏起");
  assert.ok(html.includes('class="env-secret-input">'), "未配置密钥的输入框应直接可见");
  assert.ok(!html.includes('data-sec-replace="BETA_KEY"'), "未配置密钥无需「替换」入口");
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

console.log("ui_env_panel_test: all assertions passed");

// 状态横幅（#error-banner）回归测试（Node vm 加载真实 ui/app.js）。
//
// 守三件事：
// 1. 零源降级（/api/status.sources 为空）→ 显示「未启用任何消息源」警示横幅
//    +「去启用」按钮，点击直达「设置 → 插件」面板（openSettingsModal panel=plugins）：
//    新装/升级用户默认 PLUGINS=[] 时的主入口引导；
// 2. 有消息源时横幅隐藏；lastError/lastWarning 与零源并存时更具体的报错优先
//    （零源信息已在状态文字「无消息源（检查插件配置，降级运行）」可见）；
// 3. 「去启用」复用 openSettingsModal 的 panel 参数——不回归「去设置」与
//    「重试同步」既有分支（后两者仅断言不被误改）。
//
// 数据一律虚构（见 AGENTS.md）。

import assert from "node:assert/strict";
import vm from "node:vm";

import { loadAppJs, makeElement } from "./ui_harness.mjs";

const { sandbox, getElement } = loadAppJs();
const bannerEl = getElement("error-banner");

// renderStatusBanner 渲染后经 banner.querySelector 取按钮再挂点击；桩需按
// 选择器返回可控按钮（dataset 供 data-action 读取）。
function stubBannerButtons() {
  const clickHandlers = {};
  const btn = {
    dataset: {},
    addEventListener(ev, cb) { clickHandlers[ev] = cb; },
    _fire() { if (clickHandlers.click) clickHandlers.click(); },
  };
  bannerEl.querySelector = (sel) => {
    if (sel === ".error-banner-btn") return btn;
    if (sel === ".error-banner-close") return { addEventListener() {} };
    return null;
  };
  return btn;
}

// ── 1. 零源降级 → 横幅 +「去启用」直达插件面板 ──
{
  const btn = stubBannerButtons();
  const opened = [];
  sandbox.openSettingsModal = (e, { panel } = {}) => opened.push(panel);

  sandbox.renderStatusBanner({ sources: {}, lastSync: null });
  assert.equal(bannerEl.classList.contains("hidden"), false, "零源时应显示横幅");
  assert.ok(bannerEl.innerHTML.includes("未启用任何消息源"), "横幅应说明零源");
  assert.ok(bannerEl.innerHTML.includes("去启用"), "应有「去启用」动作按钮");
  assert.ok(bannerEl.innerHTML.includes('data-action="settings-plugins"'), "按钮动作应指向插件面板");

  btn.dataset.action = "settings-plugins";
  btn._fire();
  assert.deepEqual(opened, ["plugins"], "「去启用」应直达插件面板");
  delete sandbox.openSettingsModal;
}

// ── 2a. 有消息源 → 无零源横幅（隐藏、清空） ──
{
  bannerEl.classList.remove("hidden");
  sandbox.renderStatusBanner({ sources: { weflow: { status: "online" } }, lastSync: null });
  assert.equal(bannerEl.classList.contains("hidden"), true, "有源时横幅应隐藏");
  assert.equal(bannerEl.innerHTML, "", "有源时横幅内容应清空");
}

// ── 2b. lastError 与零源并存 → 具体报错优先（不显示零源横幅） ──
{
  const btn = stubBannerButtons();
  sandbox.renderStatusBanner({ sources: {}, lastError: "同步失败：连接被拒", lastSync: null });
  assert.equal(bannerEl.classList.contains("hidden"), false, "有 lastError 时横幅应显示");
  assert.ok(bannerEl.innerHTML.includes("同步失败：连接被拒"), "应显示具体错误");
  assert.ok(bannerEl.innerHTML.includes("重试同步"), "错误态应有重试按钮");
  assert.ok(!bannerEl.innerHTML.includes("未启用任何消息源"), "错误态不应叠加零源横幅");
  assert.equal(bannerEl.className.includes("error"), true, "错误态应套用 error 样式");
}

// ── 2c. lastWarning 与零源并存 → 具体警告优先 ──
{
  const btn = stubBannerButtons();
  sandbox.renderStatusBanner({ sources: {}, lastWarning: "分类/去重阶段未启用", lastSync: null });
  assert.ok(bannerEl.innerHTML.includes("分类/去重阶段未启用"), "应显示具体警告");
  assert.ok(bannerEl.innerHTML.includes("去设置"), "警告态应有去设置按钮");
  assert.ok(!bannerEl.innerHTML.includes("未启用任何消息源"), "警告态不应叠加零源横幅");
}

console.log("ui_status_banner_test: all assertions passed");

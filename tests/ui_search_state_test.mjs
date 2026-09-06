// 搜索态与侧栏分类导航禁用开关的同步回归测试（Node vm 加载真实 ui/app.js）。
//
// 历史缺陷：`body.searching`（style.css 里 `body.searching #category-nav` 的
// pointer-events:none + 「搜索中 · 分类导航已停用」说明）只在 applySearch 里切换，
// clearSearch()（备忘录/已忽略/分类入口等退出搜索路径）与 applyHashView（hash 恢复）
// 清空搜索词后都不摘牌——表现为搜索框已清空、分类导航仍停在「已停用」且不可点击，
// 整个侧栏无法操作（截图红框复现）。
// 约定：`body.searching` 与 `currentSearch` 必须同真假，三个会改 currentSearch 的
// 函数都经 updateSearchingClass() 同步。本测试虚构输入，不涉及真实数据。

import assert from "node:assert/strict";

import { loadAppJs } from "./ui_harness.mjs";

const { sandbox, document, getElement } = loadAppJs();
const bodyCls = document.body.classList;
const searchInput = getElement("item-search"); // 搜索框（clearSearch/applySearch 写 value）

// applySearch → syncHash 需要 history/location；fetchData 由桩替换（vm 无真实 fetch）
sandbox.history = { pushState() {}, replaceState() {} };
sandbox.location = { pathname: "/", search: "", hash: "" };
let fetchCalled = false;
sandbox.fetchData = () => { fetchCalled = true; };

// 1) 进入搜索 → 导航禁用
sandbox.applySearch("test-keyword");
assert.ok(bodyCls.contains("searching"), "搜索态应置 body.searching");
assert.ok(fetchCalled, "applySearch 应触发列表重取");

// 2) clearSearch（备忘录/已忽略/导航入口同款退出路径）→ 必须摘牌
sandbox.clearSearch();
assert.equal(bodyCls.contains("searching"), false, "clearSearch 后应摘掉 body.searching");
assert.equal(searchInput.value, "", "clearSearch 应清空搜索框");

// 3) hash 恢复：带 q → 复现搜索态；不带 q → 摘牌
sandbox.applyHashView({ category: "全部", verified: "unverified", q: "restored" });
assert.ok(bodyCls.contains("searching"), "hash 恢复带 q 应置 body.searching");
sandbox.applyHashView({ category: "全部", verified: "unverified", q: "" });
assert.equal(bodyCls.contains("searching"), false, "hash 恢复不带 q 应摘掉 body.searching");

// 4) 搜索态进入后经 clearSearch 再切视图（备忘录点击的真实序列）→ 导航恢复可操作
sandbox.applySearch("second");
assert.ok(bodyCls.contains("searching"), "再次进入搜索态");
sandbox.clearSearch();
assert.equal(bodyCls.contains("searching"), false, "搜索后切视图应恢复导航");

// 5) applySearch("")（搜索框清除按钮路径）同样摘牌
sandbox.applySearch("third");
sandbox.applySearch("");
assert.equal(bodyCls.contains("searching"), false, "applySearch 空词应摘掉 body.searching");

console.log("ui_search_state_test: ok");

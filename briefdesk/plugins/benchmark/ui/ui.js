/* benchmark 插件前端：在设置弹窗「关于」面板注入「基准测试」区块。
 *
 * 核心只提供通用加载器；本文件注入后自行构建：
 * - 「导出当前列表为基准用例」：把当前筛选条件下的卡片导出为四类基准用例
 *   （classify/dedup/merge/title，期望=卡片当前状态，与「导出卡片 CSV」
 *   同口径），逐功能覆盖写入插件目录 cases/<feature>.fromweb.json
 *   （文件存储，不写数据库；无可导出用例的功能保留原文件）；
 * - 「记录处理过程」：打开/关闭 benchmark 阶段插件（管道处理时点采集
 *   dedup/merge 判定观察记录，含判重/合并命中的正向用例——按卡片最终
 *   状态导出观察不到命中），「导出处理记录」写入同一 cases 文件；
 * - 用例数 / 记录状态 / 运行状态（轮询 /api/benchmark/run）；
 * - 「运行基准测试」与「打开图表报告」（/api/benchmark/report）；
 * - 「清空基准用例」（删除 cases/*.fromweb.json）。
 *
 * 复用的核心全局工具：esc / showToast / makeItemQuery / itemPageParams。
 */
(function () {
  "use strict";
  const PLUGIN = "benchmark";
  let pollTimer = null;
  let runActive = false; // 最近一次状态轮询的运行中标记（驱动轮询保活）

  // ── 设置弹窗「关于」面板注入基准区块 ──
  function buildSection() {
    const about = document.querySelector('.settings-panel[data-panel="about"]');
    if (!about) return;
    const sec = document.createElement("section");
    sec.id = "benchmark-settings-section";
    sec.innerHTML =
      '<h3 class="settings-section settings-subsection">基准测试</h3>'
      + '<p class="text-muted settings-hint">把当前筛选的卡片导出为四类基准用例'
      + '（期望 = 卡片当前状态：分类/标题/关键词；去重与合并按共存状态配对；'
      + '已忽略的卡片作为分类噪声样本——AI 误分类的闲聊，模型不应输出分类结果），'
      + '或开启「记录处理过程」在管道真实处理时点采集判定记录'
      + '（含判重/合并命中的正向用例），逐功能覆盖写入插件目录'
      + ' cases/*.fromweb.json，不写数据库。用例数与列表规模成正比，'
      + '请先用筛选控制规模。运行会真实调用 AI，期间请勿触发同步。</p>'
      + '<div class="about-sources settings-btn-row">'
      + '<button id="bench-import-btn" class="settings-outline-btn">导出当前列表为基准用例</button>'
      + '<button id="bench-record-btn" class="settings-outline-btn">记录处理过程</button>'
      + '<button id="bench-export-recorded-btn" class="settings-outline-btn">导出处理记录</button>'
      + '<button id="bench-drop-record-btn" class="settings-outline-btn">丢弃记录</button>'
      + '<button id="bench-run-btn" class="settings-outline-btn">运行基准测试</button>'
      + '<button id="bench-clear-btn" class="settings-outline-btn">清空基准用例</button>'
      + "</div>"
      + '<p id="bench-status" class="text-muted settings-hint" style="margin-top:8px">加载中…</p>';
    // 挂在「数据导出」区块之后
    const exportSection = about.querySelector(".settings-section");
    const anchor = exportSection && exportSection.parentNode.querySelector(
      '.about-sources:last-of-type'
    );
    (anchor ? anchor.parentNode : about).appendChild(sec);
  }

  // ── 状态刷新：用例数 + 记录状态 + 运行状态 ──
  async function renderStatus() {
    const $status = document.getElementById("bench-status");
    if (!$status) {
      runActive = false;
      return;
    }
    let casesText = "-";
    try {
      const res = await fetch("/api/benchmark/cases");
      if (res.ok) {
        const data = await res.json();
        const cases = data.cases || [];
        const byFeature = {};
        cases.forEach(c => { byFeature[c.feature] = (byFeature[c.feature] || 0) + 1; });
        casesText = Object.keys(byFeature).length
          ? Object.entries(byFeature).map(([f, n]) => f + " " + n + " 条").join("，")
          : "0 条";
      }
    } catch { /* 忽略 */ }
    let recordText = "";
    try {
      const res = await fetch("/api/benchmark/record");
      if (res.ok) {
        const st = await res.json();
        const counts = st.counts || {};
        const rec = Object.entries(counts).filter(([, n]) => n > 0)
          .map(([f, n]) => f + " " + n + " 条").join("，");
        recordText = (st.enabled ? "记录中" : "记录已停")
          + (rec ? "（" + rec + "）" : "");
      }
    } catch { /* 忽略 */ }
    try {
      const res = await fetch("/api/benchmark/run");
      if (res.ok) {
        const st = await res.json();
        if (st.running) {
          runActive = true;
          $status.innerHTML = "运行中（用例：" + esc(casesText) + "）…请勿触发同步"
            + (recordText ? "；" + esc(recordText) : "");
          return;
        }
        runActive = false;
        if (st.error) {
          $status.innerHTML = "用例：" + esc(casesText) + "。最近一次运行失败：" + esc(st.error)
            + (recordText ? "；" + esc(recordText) : "");
          return;
        }
        if (st.summary) {
          const parts = Object.entries(st.summary).map(([f, s]) => {
            const key = f === "title" ? "keyword_hit_rate" : f === "classify" ? "category_accuracy" : "accuracy";
            const v = s && s[key];
            const pct = typeof v === "number" ? (v * 100).toFixed(1) + "%" : "-";
            return f + " " + pct;
          });
          let durText = "";
          if (typeof st.elapsed_sec === "number" && isFinite(st.elapsed_sec)) {
            durText = st.elapsed_sec < 1
              ? Math.round(st.elapsed_sec * 1000) + "ms"
              : st.elapsed_sec.toFixed(1) + "s";
          }
          $status.innerHTML = "用例：" + esc(casesText) + "。最近结果：" + esc(parts.join("，"))
            + (durText ? "；用时 " + esc(durText) : "")
            + '。 <a href="/api/benchmark/report" target="_blank" rel="noopener">打开图表报告</a>'
            + (recordText ? "；" + esc(recordText) : "");
          return;
        }
      }
    } catch { /* 忽略 */ }
    $status.textContent = "用例：" + casesText + (recordText ? "；" + recordText : "");
    const $recordBtn = document.getElementById("bench-record-btn");
    if ($recordBtn) {
      try {
        const res = await fetch("/api/benchmark/record");
        if (res.ok) {
          const st = await res.json();
          $recordBtn.textContent = st.enabled ? "停止记录" : "记录处理过程";
        }
      } catch { /* 忽略 */ }
    }
  }

  // 轮询门控：仅设置弹窗打开或基准运行中时保持 3s 轮询，其余时刻停表，
  // 避免页面整个生命周期内对 /api/benchmark/* 的无谓轮询。
  function syncPolling(immediate = false) {
    const $modal = document.getElementById("settings-modal");
    const open = !!$modal && !$modal.classList.contains("hidden");
    const need = open || runActive;
    if (need && !pollTimer) {
      pollTimer = setInterval(refreshStatus, 3000);
      if (immediate) refreshStatus();
    } else if (!need && pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  async function refreshStatus() {
    await renderStatus();
    syncPolling(); // 运行结束且弹窗已关 → 就地停表
  }

  // ── 事件 ──
  async function importCurrent() {
    const $btn = document.getElementById("bench-import-btn");
    const $status = document.getElementById("bench-status");
    if (!$btn || !$status) return;
    if (!window.confirm("导出会覆盖 cases/*.fromweb.json 中各功能的现有用例文件（无可导出用例的功能保留原文件），继续？")) return;
    $btn.disabled = true;
    $status.textContent = "导出中…";
    try {
      const query = makeItemQuery();
      const params = itemPageParams(query, 0);
      params.delete("limit"); params.delete("offset"); // 服务端翻页全量
      const res = await fetch("/api/benchmark/import-current?" + params.toString(), {
        method: "POST",
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        showToast("导出失败：" + (data.detail || ("HTTP " + res.status)), { type: "error", duration: 5000 });
        $status.textContent = "导出失败";
        return;
      }
      const c = data.counts || {};
      showToast("已导出 " + (data.total || 0) + " 个用例（分类" + (c.classify || 0)
        + " / 去重" + (c.dedup || 0) + " / 合并" + (c.merge || 0)
        + " / 标题" + (c.title || 0) + "）"
        + ((data.noise || 0) > 0 ? "；噪声（已忽略）" + data.noise + " 条" : ""),
        { type: "success", duration: 4000 });
    } catch (err) {
      console.error("Benchmark export error:", err);
      showToast("导出失败，请重试", { type: "error", duration: 4000 });
    } finally {
      $btn.disabled = false;
      refreshStatus();
    }
  }

  async function toggleRecord() {
    const $btn = document.getElementById("bench-record-btn");
    if (!$btn) return;
    $btn.disabled = true;
    try {
      const cur = await fetch("/api/benchmark/record").then(r => r.json()).catch(() => ({}));
      const enabled = !(cur && cur.enabled);
      if (enabled && !window.confirm("开始记录处理过程？新到达消息在管道处理时点被采集为基准用例（含真实聊天内容，导出文件已 gitignore），停止记录后可用「导出处理记录」写入 cases 文件。")) return;
      const res = await fetch("/api/benchmark/record", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: enabled }),
      });
      if (!res.ok) throw new Error("HTTP " + res.status);
      showToast(enabled ? "已开始记录处理过程" : "已停止记录（累积内容保留，可导出或丢弃）",
        { type: "success", duration: 3000 });
    } catch (err) {
      console.error("Benchmark record toggle error:", err);
      showToast("切换记录状态失败，请重试", { type: "error", duration: 4000 });
    } finally {
      $btn.disabled = false;
      refreshStatus();
    }
  }

  async function exportRecorded() {
    const $btn = document.getElementById("bench-export-recorded-btn");
    const $status = document.getElementById("bench-status");
    if (!$btn || !$status) return;
    if (!window.confirm("把已累积的处理记录导出为基准用例？会覆盖 cases/*.fromweb.json 中对应功能的现有用例文件（无记录的功能保留原文件），导出后清空记录。")) return;
    $btn.disabled = true;
    $status.textContent = "导出记录中…";
    try {
      const res = await fetch("/api/benchmark/export-recorded", { method: "POST" });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        showToast("导出失败：" + (data.detail || ("HTTP " + res.status)), { type: "error", duration: 5000 });
        $status.textContent = "导出失败";
        return;
      }
      const c = data.counts || {};
      showToast("已导出 " + (data.total || 0) + " 个用例（去重" + (c.dedup || 0)
        + " / 合并" + (c.merge || 0) + " / 标题" + (c.title || 0) + "）",
        { type: "success", duration: 4000 });
    } catch (err) {
      console.error("Benchmark export recorded error:", err);
      showToast("导出失败，请重试", { type: "error", duration: 4000 });
    } finally {
      $btn.disabled = false;
      refreshStatus();
    }
  }

  async function dropRecord() {
    if (!window.confirm("丢弃已累积的处理记录（不导出，不影响 cases 文件）？")) return;
    try {
      const res = await fetch("/api/benchmark/record", { method: "DELETE" });
      if (!res.ok) throw new Error("HTTP " + res.status);
      const data = await res.json().catch(() => ({}));
      showToast("已丢弃 " + (data.cleared || 0) + " 条记录", { type: "success", duration: 3000 });
    } catch (err) {
      console.error("Benchmark drop record error:", err);
      showToast("丢弃失败，请重试", { type: "error", duration: 4000 });
    }
    refreshStatus();
  }

  async function runBench() {
    const $btn = document.getElementById("bench-run-btn");
    const $status = document.getElementById("bench-status");
    if (!$btn || !$status) return;
    $btn.disabled = true;
    try {
      const res = await fetch("/api/benchmark/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        showToast("启动失败：" + (data.detail || ("HTTP " + res.status)), { type: "error", duration: 4000 });
        return;
      }
      $status.textContent = "运行中…请勿触发同步";
      runActive = true;
      syncPolling(true);
    } catch (err) {
      console.error("Benchmark run error:", err);
      showToast("启动失败，请重试", { type: "error", duration: 4000 });
    } finally {
      $btn.disabled = false;
    }
  }

  async function clearCases() {
    if (!window.confirm("删除全部基准用例文件（cases/*.fromweb.json，不影响卡片数据）？")) return;
    try {
      const res = await fetch("/api/benchmark/cases", { method: "DELETE" });
      if (!res.ok) throw new Error("HTTP " + res.status);
      const data = await res.json().catch(() => ({}));
      showToast("已删除 " + (data.deleted || 0) + " 个用例文件", { type: "success", duration: 3000 });
    } catch (err) {
      console.error("Benchmark clear error:", err);
      showToast("清空失败，请重试", { type: "error", duration: 4000 });
    }
    refreshStatus();
  }

  // ── 入口：核心加载器注入本脚本后调用 ──
  function init(api) {
    if (!api || typeof api.isLoaded !== "function" || !api.isLoaded(PLUGIN)) return;
    buildSection();
    const $importBtn = document.getElementById("bench-import-btn");
    const $recordBtn = document.getElementById("bench-record-btn");
    const $exportRecBtn = document.getElementById("bench-export-recorded-btn");
    const $dropRecBtn = document.getElementById("bench-drop-record-btn");
    const $runBtn = document.getElementById("bench-run-btn");
    const $clearBtn = document.getElementById("bench-clear-btn");
    if ($importBtn) $importBtn.addEventListener("click", importCurrent);
    if ($recordBtn) $recordBtn.addEventListener("click", toggleRecord);
    if ($exportRecBtn) $exportRecBtn.addEventListener("click", exportRecorded);
    if ($dropRecBtn) $dropRecBtn.addEventListener("click", dropRecord);
    if ($runBtn) $runBtn.addEventListener("click", runBench);
    if ($clearBtn) $clearBtn.addEventListener("click", clearCases);
    // 弹窗开合驱动轮询门控；初始若弹窗已开则立即拉一轮状态
    const $settingsModal = document.getElementById("settings-modal");
    if ($settingsModal) {
      new MutationObserver(() => syncPolling(true)).observe($settingsModal, {
        attributes: true,
        attributeFilter: ["class"],
      });
    }
    syncPolling(true);
  }

  window.briefdeskPlugins = window.briefdeskPlugins || {};
  window.briefdeskPlugins.benchmark = { init: init };
})();

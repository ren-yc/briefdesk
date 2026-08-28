// 视觉回归对比工具（Playwright + 真实 ui/style.css + 真实 renderCard）。
//
// 为什么需要它：字号/间距/圆角的令牌化收敛是纯几何改写，ruff / mypy /
// pytest / tests/test_ui_contrast.py 全都查不出"某处宽了 2 像素"。
// 这个工具补上那一层。
//
// 基线图刻意不入库（约 2-4MB 二进制，且二进制 diff 无法 review）：
// 基线存放在系统临时目录，改样式前 --update 生成，改完后直接跑一次比对。
//
// 用法：
//   node tests/ui_visual_check.mjs --update   # 改动前：生成基线
//   node tests/ui_visual_check.mjs            # 改动后：与基线比对
//   node tests/ui_visual_check.mjs --open     # 比对并保留差异图目录
//   node tests/ui_visual_check.mjs --measure  # 量出各类控件的实际渲染尺寸
//
// 数据全为虚构，不含真实聊天内容。

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

// 存放位置按优先级解析：
//   1) BRIEFDESK_VISUAL_DIR 环境变量（显式指定，优先级最高）
//   2) 系统临时目录（默认，天然在仓库之外）
//   3) 仓库内 .visual-*（已 gitignore）——某些沙箱/受限环境下临时目录不可写
// 三者都不入库；第 3 种只是兜底，仍不会进 commit。
function resolveDirs() {
  const explicit = process.env.BRIEFDESK_VISUAL_DIR;
  const candidates = explicit ? [explicit] : [os.tmpdir(), ROOT];
  for (const base of candidates) {
    const probe = path.join(base, ".briefdesk-visual-probe");
    try {
      fs.mkdirSync(probe, { recursive: true });
      fs.rmSync(probe, { recursive: true, force: true });
    } catch {
      continue;
    }
    const inRepo = base === ROOT;
    const nameOf = (kind) => (inRepo ? `.visual-${kind}` : `briefdesk-visual-${kind}`);
    return {
      base,
      BASE_DIR: path.join(base, nameOf("baseline")),
      OUT_DIR: path.join(base, nameOf("current")),
      DIFF_DIR: path.join(base, nameOf("diff")),
    };
  }
  console.error(
    "找不到可写目录存放基线图。可用 BRIEFDESK_VISUAL_DIR 指定一个可写路径：\n" +
    "  BRIEFDESK_VISUAL_DIR=D:\\some\\dir node tests/ui_visual_check.mjs --update",
  );
  process.exit(1);
}

const { base: VISUAL_BASE, BASE_DIR, OUT_DIR, DIFF_DIR } = resolveDirs();

// Playwright 自己会在 os.tmpdir() 下 mkdtemp 放 artifacts。若系统临时目录
// 不可写（受限沙箱），launch() 会直接 EPERM 失败——把它的临时目录也指到
// 已确认可写的位置。
if (VISUAL_BASE === ROOT) {
  const pwTmp = path.join(ROOT, ".visual-tmp");
  fs.mkdirSync(pwTmp, { recursive: true });
  process.env.TMPDIR = pwTmp;
  process.env.TEMP = pwTmp;
  process.env.TMP = pwTmp;
}

const UPDATE = process.argv.includes("--update");
const KEEP = process.argv.includes("--open");
const MEASURE = process.argv.includes("--measure");

// ── 1. 用 vm 加载真实 app.js，拿到真实 renderCard / renderItemRow ──
// 与 tests/ui_context_cache_test.mjs 同一套桩，避免手抄 HTML 与实现漂移。
const ESC_MAP = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

function makeElement() {
  return {
    innerHTML: "", textContent: "", value: "", className: "", style: {},
    dataset: {}, hidden: false,
    classList: { add() {}, remove() {}, contains() { return false; }, toggle() {} },
    addEventListener() {}, removeEventListener() {},
    querySelector() { return null; }, querySelectorAll() { return []; },
    closest() { return null; }, setAttribute() {}, removeAttribute() {},
    focus() {}, appendChild() {}, remove() {},
  };
}

const documentStub = {
  getElementById: () => makeElement(),
  querySelector: () => makeElement(),
  querySelectorAll: () => [],
  createElement: (tag) => {
    const el = makeElement();
    if (tag === "div") {
      Object.defineProperty(el, "textContent", {
        get() { return this._t || ""; },
        set(v) {
          this._t = String(v);
          this.innerHTML = this._t.replace(/[&<>"']/g, (ch) => ESC_MAP[ch]);
        },
      });
    }
    return el;
  },
  addEventListener() {},
  body: makeElement(),
  documentElement: makeElement(),
  scrollingElement: makeElement(),
  activeElement: makeElement(),
  contains: () => true,
};

const noop = () => {};
const sandbox = {
  document: documentStub,
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  navigator: { clipboard: {} },
  EventSource: class { constructor() { this.readyState = 0; } close() {} },
  MutationObserver: class { observe() {} disconnect() {} takeRecords() { return []; } },
  IntersectionObserver: class { observe() {} disconnect() {} },
  CSS: { escape: (s) => s },
  console, setTimeout, clearTimeout, setInterval, clearInterval,
  requestAnimationFrame: (fn) => fn(), cancelAnimationFrame: noop,
  matchMedia: () => ({ matches: false, addEventListener: noop, removeEventListener: noop }),
  addEventListener: noop, removeEventListener: noop, open: noop,
  fetch: async () => ({ ok: true, json: async () => ({}) }),
};
sandbox.window = sandbox;
sandbox.self = sandbox;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(ROOT, "ui", "app.js"), "utf8"), sandbox, {
  filename: "ui/app.js",
});

for (const fn of ["renderCard", "renderItemRow"]) {
  if (typeof sandbox[fn] !== "function") {
    console.error(`无法从 ui/app.js 取到 ${fn}()——渲染函数可能已改名`);
    process.exit(1);
  }
}

// ── 2. 虚构数据（覆盖各种徽章/状态组合，勿用真实聊天内容）──
const NOW = 1700000000;
const item = (over) => ({
  id: 1, category: "社团", subject: "示例话题", summary: "这是一条用于视觉回归的虚构摘要文本，长度接近真实卡片。",
  content: "虚构正文内容", sender: "示例同学", session_name: "示例群聊",
  msg_time: NOW, deadline: null, is_verified: 0, source: "weflow",
  session_id: "demo-session", msg_id: "demo-1", article_url: "",
  source_group: null, cat_color: "#2563EB", ...over,
});

const FIXTURES = [
  { name: "card-basic", html: () => sandbox.renderCard(item({})) },
  { name: "card-memo", html: () => sandbox.renderCard(item({ id: 2, is_verified: 1 })) },
  { name: "card-ignored", html: () => sandbox.renderCard(item({ id: 3, is_verified: -1 })) },
  {
    name: "card-deadlines",
    html: () => [
      sandbox.renderCard(item({ id: 4, deadline: NOW + 1800, subject: "紧急" })),
      sandbox.renderCard(item({ id: 5, deadline: NOW + 7200, subject: "今天" })),
      sandbox.renderCard(item({ id: 6, deadline: NOW + 86400 * 2, subject: "稍后" })),
      sandbox.renderCard(item({ id: 7, deadline: NOW - 3600, subject: "已过期" })),
    ].join(""),
  },
  {
    name: "card-long-text",
    html: () => sandbox.renderCard(item({
      id: 8,
      subject: "很长的话题标题用来触发换行与截断处理的边界情况检查",
      summary: "很长的摘要：" + "测试文本".repeat(30),
      session_name: "名字很长的示例群聊用于检查元信息行换行",
    })),
  },
  { name: "row-basic", html: () => sandbox.renderItemRow(item({ id: 9 }), { cls: "demo-row", showSubject: true }) },
];

// ── 3. 组页面：真实 style.css + 固定视口 ──
const css = fs.readFileSync(path.join(ROOT, "ui", "style.css"), "utf8");

function page(bodyHtml, theme) {
  return `<!DOCTYPE html>
<html lang="zh-CN"${theme === "dark" ? ' data-theme="dark"' : ""}>
<head><meta charset="UTF-8"><style>${css}</style>
<style>
  /* 关掉动画与过渡，避免截图时序抖动 */
  *, *::before, *::after { animation: none !important; transition: none !important; }
  body { margin: 0; padding: 24px; width: 900px; }
  /* 图标是运行时内联的 SVG，这里统一成实心方块，避免网络请求造成抖动 */
  img.icon, img.icon-sm, img.icon-lg { visibility: hidden; }
</style></head>
<body><div id="items-container">${bodyHtml}</div></body></html>`;
}

// ── 4. 截图 ──
let chromium;
try {
  ({ chromium } = await import("playwright"));
} catch {
  console.error("未找到 playwright。安装：npm i -g playwright && npx playwright install chromium");
  process.exit(1);
}

// ── --measure：量真实渲染高度 ──
// padding 相同不等于高度相同（字号、line-height、border、min-height 都参与）。
// 这个模式把"看起来差不多的小按钮"的实际盒子尺寸列出来，用于判断哪些取值
// 差异是真的会被看见的，哪些只是写法不同。
if (MEASURE) {
  const SAMPLES = [
    ["主操作按钮", '<button class="sync-btn">同步</button>'],
    ["顶栏链接", '<a class="top-settings-link">设置</a>'],
    ["卡片操作", '<div class="card-actions"><button>备忘</button></div>'],
    ["浮层操作", '<div class="ov-actions"><button>确定</button></div>'],
    ["弹窗操作", '<div class="modal-actions"><button>保存</button></div>'],
    ["筛选 chip", '<button class="filter-chip">全部</button>'],
    ["批量开关", '<button class="batch-toggle-btn">批量操作</button>'],
    ["批量条按钮", '<div class="batch-bar"><button>删除</button></div>'],
    ["加载更多", '<button class="load-more-btn">加载更多</button>'],
    ["空态按钮", '<button class="empty-guide-btn">去设置</button>'],
    ["设置描边按钮", '<button class="settings-outline-btn">暂存更改</button>'],
    ["设置添加按钮", '<button class="settings-add-btn">添加</button>'],
    ["分类行按钮", '<div class="cat-row"><button>改名</button></div>'],
    ["错误条按钮", '<button class="error-banner-btn">重试</button>'],
    ["重试（状态）", '<button class="status-retry">重试</button>'],
    ["话题 chip", '<button class="subject-chip">话题</button>'],
    ["引导 chip", '<button class="onboard-chip">类别</button>'],
    ["时效徽章", '<span class="time-badge today">今天</span>'],
    ["订阅徽章", '<span class="subs-badge">订阅</span>'],
    ["环境徽章", '<span class="env-badge">已配置</span>'],
    ["分段按钮", '<div class="seg"><button class="seg-btn">折叠</button></div>'],
    ["菜单项", '<div class="card-more-menu"><button class="more-option">操作</button></div>'],
  ];
  const body = SAMPLES.map(([label, html]) =>
    `<div class="probe" data-label="${label}">${html}</div>`).join("");
  const browserM = await launchChromium();
  const ctx = await browserM.newContext({ viewport: { width: 900, height: 600 }, deviceScaleFactor: 1 });
  const pg = await ctx.newPage();
  await pg.setContent(page(body, "light"), { waitUntil: "load" });
  const rows = await pg.evaluate(() => {
    const out = [];
    for (const probe of document.querySelectorAll(".probe")) {
      const el = probe.querySelector("button, a, span") || probe.firstElementChild;
      if (!el) continue;
      const r = el.getBoundingClientRect();
      const cs = getComputedStyle(el);
      out.push({
        label: probe.dataset.label,
        h: Math.round(r.height * 100) / 100,
        w: Math.round(r.width * 100) / 100,
        fs: cs.fontSize,
        pad: `${cs.paddingTop}/${cs.paddingRight}`,
        radius: cs.borderTopLeftRadius,
      });
    }
    return out;
  });
  await browserM.close();
  rows.sort((a, b) => a.h - b.h);
  const pad = (s, n) => String(s).padEnd(n, " ");
  console.log(pad("控件", 16) + pad("高度", 9) + pad("字号", 8) + pad("内边距", 14) + "圆角");
  console.log("-".repeat(60));
  for (const r of rows) {
    console.log(pad(r.label, 16) + pad(r.h + "px", 9) + pad(r.fs, 8) + pad(r.pad, 14) + r.radius);
  }
  const heights = [...new Set(rows.map((r) => r.h))].sort((a, b) => a - b);
  console.log(`\n共 ${rows.length} 类控件，${heights.length} 种不同高度：${heights.join(", ")}`);
  const small = rows.filter((r) => r.h < 24);
  if (small.length) {
    console.log(`\n低于 24px（WCAG 2.5.8 目标尺寸）：`);
    for (const r of small) console.log(`  ${r.label}  ${r.h}px`);
  }
  process.exit(0);
}

const targetDir = UPDATE ? BASE_DIR : OUT_DIR;
fs.mkdirSync(targetDir, { recursive: true });

// Playwright 期望的 Chromium 版本可能与本机缓存的不一致（比如包升级了但
// 没重新 npx playwright install）。此时 launch() 会因找不到它期望的那个
// 精确版本而失败，尽管缓存里有可用的浏览器。这里退一步：自动挑缓存中
// 最新的一份，仍找不到才报错并给出安装命令。
// BRIEFDESK_CHROMIUM 可显式指定可执行文件路径。
function findChromium() {
  const explicit = process.env.BRIEFDESK_CHROMIUM;
  if (explicit) return fs.existsSync(explicit) ? explicit : null;
  const cache = process.env.PLAYWRIGHT_BROWSERS_PATH
    || path.join(process.env.LOCALAPPDATA || os.homedir(), "ms-playwright");
  if (!fs.existsSync(cache)) return null;
  const rel = [
    path.join("chrome-win64", "chrome.exe"),
    path.join("chrome-win", "chrome.exe"),
    path.join("chrome-linux", "chrome"),
    path.join("chrome-mac", "Chromium.app", "Contents", "MacOS", "Chromium"),
  ];
  const builds = fs.readdirSync(cache)
    .filter((d) => /^chromium-\d+$/.test(d))
    .sort((a, b) => Number(b.split("-")[1]) - Number(a.split("-")[1]));
  for (const b of builds) {
    for (const r of rel) {
      const exe = path.join(cache, b, r);
      if (fs.existsSync(exe)) return exe;
    }
  }
  return null;
}

async function launchChromium() {
  try {
    return await chromium.launch();
  } catch (err) {
    const exe = findChromium();
    if (!exe) {
      console.error("找不到可用的 Chromium。安装：npx playwright install chromium");
      console.error(String(err.message).split("\n")[0]);
      process.exit(1);
    }
    console.log(`提示：改用缓存中的 Chromium → ${exe}`);
    return await chromium.launch({ executablePath: exe });
  }
}

const browser = await launchChromium();
const shots = [];
try {
  for (const theme of ["light", "dark"]) {
    const ctx = await browser.newContext({
      viewport: { width: 900, height: 600 },
      deviceScaleFactor: 1,
    });
    const pg = await ctx.newPage();
    for (const f of FIXTURES) {
      await pg.setContent(page(f.html(), theme), { waitUntil: "load" });
      const name = `${f.name}--${theme}.png`;
      const buf = await pg.screenshot({ fullPage: true });
      fs.writeFileSync(path.join(targetDir, name), buf);
      shots.push(name);
    }
    await ctx.close();
  }
} finally {
  await browser.close();
}

if (UPDATE) {
  console.log(`基线已生成：${shots.length} 张 → ${BASE_DIR}`);
  console.log("现在可以改样式；改完运行 node tests/ui_visual_check.mjs 比对。");
  process.exit(0);
}

// ── 5. 比对 ──
if (!fs.existsSync(BASE_DIR) || !fs.readdirSync(BASE_DIR).length) {
  console.error("未找到基线。先运行：node tests/ui_visual_check.mjs --update");
  process.exit(1);
}

// 同一个 Chromium 编码器、同一视口、动画已关：内容相同则字节相同，
// 因此直接比字节即可判定"有无变化"，不必引入图像解码依赖。
// 差异的具体位置交给人工打开两张图对比——这个工具的职责是"发现变化"，
// 而不是替人判断变化对不对。
function decode(file) {
  return fs.readFileSync(file);
}

let changed = 0;
let missing = 0;
const report = [];
for (const name of shots) {
  const basePath = path.join(BASE_DIR, name);
  const curPath = path.join(OUT_DIR, name);
  if (!fs.existsSync(basePath)) { missing += 1; report.push(`  新增（基线中没有）：${name}`); continue; }
  const a = decode(basePath), b = decode(curPath);
  if (a.equals(b)) continue;
  changed += 1;
  // 尺寸取自 PNG IHDR：宽高各 4 字节，偏移 16/20
  const dim = (buf) => `${buf.readUInt32BE(16)}x${buf.readUInt32BE(20)}`;
  const da = dim(a), db = dim(b);
  report.push(
    da === db
      ? `  像素差异：${name}（尺寸未变 ${da}，${a.length} → ${b.length} 字节）`
      : `  尺寸变化：${name}  ${da} → ${db}  ← 布局发生位移`,
  );
}

if (changed || missing) {
  fs.mkdirSync(DIFF_DIR, { recursive: true });
  for (const name of shots) {
    const curPath = path.join(OUT_DIR, name);
    const basePath = path.join(BASE_DIR, name);
    if (fs.existsSync(basePath) && fs.existsSync(curPath) && decode(basePath).equals(decode(curPath))) continue;
    if (fs.existsSync(curPath)) fs.copyFileSync(curPath, path.join(DIFF_DIR, name));
  }
  console.error(`ui_visual_check: ${changed} 张有差异${missing ? `，${missing} 张新增` : ""}（共 ${shots.length} 张）`);
  console.error(report.join("\n"));
  console.error(`\n基线：${BASE_DIR}\n当前：${OUT_DIR}\n差异副本：${DIFF_DIR}`);
  console.error("请人工打开对比；若差异是有意的，重新 --update 更新基线。");
  process.exit(1);
}

if (!KEEP) fs.rmSync(OUT_DIR, { recursive: true, force: true });
console.log(`ui_visual_check: ok（${shots.length} 张与基线一致）`);

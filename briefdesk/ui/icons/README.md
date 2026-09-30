# ui/icons — 图标目录

本目录是前端唯一的图标来源：[Lucide](https://lucide.dev) 的 vendored 子集
（英文 kebab-case 扁平命名，经 `/icons/<name>.svg` 引用）。

- **选型**：Lucide（线框 stroke-2 风格），2026 年图标库选型对比后确定；
  对比过 Tabler（MIT，等价备选）、Phosphor Fill（叉号/对勾方框化，出局）、
  Remix Line（小尺寸辨识度与语义匹配度稍逊，出局）。
- **许可**：Lucide 采用 [ISC 许可](https://lucide.dev/license)。
  本子集随本项目（MIT）分发，原始版权声明如下：
  > ISC License © 2020-2026 Lucide Contributors
- **版本记录**：本子集钉定 vendored 自 `lucide-static@1.34.0`（文件中内嵌
  `@license lucide-static v1.34.0 - ISC` 头注释可核对）。钉定值唯一事实来源为
  `scripts/fetch_icons.py` 的 `LUCIDE_STATIC_VERSION`，`python scripts/fetch_icons.py
  show` 可查看。升级流程：先跑 `check` 确认清单全部图标在新版本仍可拉取
  （未被改名/移除）→ 改脚本常量 → 更新本节 → 跑清单守卫测试。
- **单一事实来源**：`ui/icon-manifest.txt`（与目录文件集合必须一致，
  由 `tests/test_icon_manifest.py` 双向守卫）。

## 新增图标流程

1. 取图标并登记（二选一）：
   - 首选脚本：`python scripts/fetch_icons.py add <name>`——从钉定版本的
     lucide-static 逐个拉取并自动登记 manifest（禁止整库拷贝、禁止引入
     第二图标库）；
   - 或从 [lucide.dev](https://lucide.dev) 取单个 SVG 拷入本目录（英文
     kebab-case 命名），手动在 `ui/icon-manifest.txt` 登记一行
     `/icons/<name>.svg`；
2. 代码引用：静态写法 `<img src="/icons/<name>.svg">`；动态渲染的图标必须
   同步加入 `ui/app.js` 的 `preloadSvgIcons()` 预取集合（或 `_CAT_ICONS` /
   `_CAT_PALETTE` / `_STATUS_ICONS` 映射），确保内联缓存命中；
3. 跑 `python -m pytest tests/test_icon_manifest.py tests/test_fetch_icons_script.py`，
   双向断言通过后方可提交。

## 插件图标通道

插件前端（`briefdesk/plugins/*/ui/`）**不单独携带图标目录**，图标来源二选一：

1. **复用核心 `/icons/<name>.svg`**：与核心前端同路径引用，图标须已登记
   `ui/icon-manifest.txt`——`tests/test_icon_manifest.py` 的引用守卫自动扫描
   插件文件，未登记/不存在的图标直接测试失败；
2. **在插件 `ui/ui.js` 内联 Lucide SVG**：须满足与核心内联管线同等的约束——
   `stroke`/`fill="currentColor"`（颜色随 CSS 继承，深色模式不黑化）、不携带
   `on*` 事件属性、不含 `<script>`（`test_plugin_inline_svg_follows_theme_and_stays_safe`
   守卫覆盖）；禁止硬编码颜色。

新增所需图标一律走「新增图标流程」登记进核心清单，禁止自定义插件图标目录、
禁止外链图标与 `data:` 图标。

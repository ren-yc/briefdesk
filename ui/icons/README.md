# ui/icons — 图标目录

本目录是前端唯一的图标来源：[Lucide](https://lucide.dev) 的 vendored 子集
（英文 kebab-case 扁平命名，经 `/icons/<name>.svg` 引用）。

- **选型**：Lucide（线框 stroke-2 风格），2026 年图标库选型对比后确定；
  对比过 Tabler（MIT，等价备选）、Phosphor Fill（叉号/对勾方框化，出局）、
  Remix Line（小尺寸辨识度与语义匹配度稍逊，出局）。
- **许可**：Lucide 采用 [ISC 许可](https://lucide.dev/license)。
  本子集随本项目（MIT）分发，原始版权声明如下：
  > ISC License © 2020-2026 Lucide Contributors
- **单一事实来源**：`ui/icon-manifest.txt`（与目录文件集合必须一致，
  由 `tests/test_icon_manifest.py` 双向守卫）。

## 新增图标流程

1. 从 [lucide.dev](https://lucide.dev)（或 `lucide-static` npm 包）取单个 SVG，
   拷入本目录（英文 kebab-case 命名，禁止整库拷贝、禁止引入第二图标库）；
2. 在 `ui/icon-manifest.txt` 登记一行 `/icons/<name>.svg`；
3. 代码引用：静态写法 `<img src="/icons/<name>.svg">`；动态渲染的图标必须
   同步加入 `ui/app.js` 的 `preloadSvgIcons()` 预取集合（或 `_CAT_ICONS` /
   `_CAT_PALETTE` / `_STATUS_ICONS` 映射），确保内联缓存命中；
4. 跑 `python -m pytest tests/test_icon_manifest.py`，双向断言通过后方可提交。

"""LLM 功能基准测试插件（classify / dedup / merge / title 输出质量评估）。

- Web 插件（WebPlugin）：/api/benchmark/* 路由 + 前端（ui/）——
  设置弹窗可把当前筛选的卡片导出为四类基准用例文件
  cases/*.fromweb.json（classify/dedup/merge/title，文件存储，
  不触碰数据库）；
- 阶段插件（StagePlugin，slot=post_insert）：管道真实处理时点采集
  dedup/merge 判定观察记录（含判重/合并命中的正向用例），经
  /api/benchmark/export-recorded 导出为 cases/<feature>.fromweb.json；
- CLI：python -m briefdesk.plugins.benchmark.cli（文件数据集，含网页导出的
  *.fromweb.json 自动回退）；
- 运行：与生产同引擎同 AI 供应商，临时库经补丁 get_db/get_embed_db 隔离，
  不触碰运行中应用的数据库连接。
"""

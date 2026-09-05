# 安全策略

## 支持的版本

简报台是单机运行的本地应用，不发布独立版本，安全修复直接落在 `master` 分支，请始终使用最新 `master`。

## 如何上报漏洞

请使用 GitHub 私密漏洞报告：仓库页面 **Security → Report a vulnerability**。

- 不要将可能涉及凭据或真实消息内容的漏洞细节开成公开 issue；
- 报告内容同样遵守脱敏要求：不要粘贴真实 Token、Key、手机号、QQ/微信 ID 或聊天记录，密钥请用 `<your-api-key>` 之类的占位符代替。

## 重点风险面

本项目会在本地落库真实群聊消息，重点关注以下方面：

- `.env`、用户配置与系统钥匙串中的消息源 Token（`WEFLOW_API_TOKEN` / `WEFLOW_LEGACY_API_TOKEN` / `QQFLOW_API_TOKEN` 等）与 `AI_API_KEY`；
- SQLite 数据库中的真实聊天记录（`*.sqlite` 不得提交到仓库或随报告外发）；
- OCR extra（`pip install -e ".[ocr]"`）引入的本地推理依赖。

## 处理流程

收到报告后会尽快评估确认，修复以普通 commit 合入 `master`，并在报告中同步结论。

"""weflow 消息源插件包（weflow-server :5033，微信 4.x）。

六文件分层与 weflow-legacy / qqflow 同构：
config（配置存储）/ client（HTTP+SSE 客户端）/ normalize（归一化）/
poller（REST 历史回填）/ sse（实时监听）/ runtime（装配门面）/ plugin（SourcePlugin）。
"""

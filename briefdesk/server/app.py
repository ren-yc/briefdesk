"""FastAPI 应用实例（server 子包）。

独立成模块以避免子包循环导入：`middleware`/`web_plugins`/`routes_*`/
`media`/`static` 均从本模块取 `app` 并在导入时用装饰器注册路由/中间件，
`briefdesk.server.__init__` 按组装顺序导入它们。
"""

from fastapi import FastAPI

app = FastAPI(title="简报台 (BRIEFDESK)")

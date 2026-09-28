"""HTTP 接口层。

    deps.py      依赖注入（get_db）
    schemas.py   响应模型（Pydantic）
    health.py    /api/health
    devices.py   /api/devices, /api/devices/{id}, /api/devices/{id}/latest
    telemetry.py /api/telemetry
    anomalies.py /api/anomalies
    predictions.py /api/predictions

路由清单来自设计文档 §23。注意 §19 给的目录结构里只有
devices/telemetry/anomalies 三个文件，但 §23 要求七个路由——
health 和 predictions 没有对应文件，所以这里补上了。

所有端点都写成**同步 def** 而不是 async def：

    FastAPI 会把同步端点丢进线程池执行，不阻塞事件循环。
    而我们的数据库访问是同步的（SQLAlchemy + SQLite），
    写成 async def 再调同步 DB 才是真的会把事件循环堵住。
    这是"该同步就同步"的一个具体例子，不是偷懒。
"""

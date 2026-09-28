"""IoT 环境监测系统 —— Python 后端。

分层（每一层只依赖它下面那一层）：

    mqtt/       MQTT 接入：paho 客户端 + 入站报文契约（Pydantic）
    db/         持久化：SQLAlchemy 模型 + 仓储（唯一写 SQL 的地方）
    services/   业务编排：把 MQTT 报文变成数据库行
    api/        HTTP 接口：FastAPI 路由 + 响应模型
    ai/         Phase 8/9 占位，本轮不实现（文档 §26-§33）

完整数据流：

    ESP32 ──MQTT──▶ mqtt/consumer ──队列──▶ services ──▶ db/repository ──▶ SQLite
                    （paho 网络线程）  （写库线程）                          │
                                                                           ▼
                                            浏览器 / curl ◀── api/ ◀───────┘

为什么把 MQTT 回调和写库拆成两个线程、中间放一个队列：

    paho 的网络线程同时负责收报文、发 keepalive（PINGREQ）和确认
    （PUBACK）。在它的回调里直接写 SQLite，一次 fsync 就是几毫秒到几十毫秒，
    这期间它没法回 PINGREQ——broker 会在 keepalive×1.5 之后认为本端已死并断开，
    于是触发重连、重投、雪崩。

    回调只做"解析 + 入队"（微秒级）之后，这个问题就不存在了。
    代价是队列满时会丢数据——所以队列深度、丢弃计数都暴露在 /api/health 里，
    让"丢"这件事可见（见 app/stats.py）。

版本号与固件侧对应：固件 v0.4-mqtt 之后就是后端 v0.5-python（文档 §36）。
"""

__version__ = "0.5.0"

"""应用装配：``create_app()`` 工厂 + lifespan。

本文件把三个独立的部件接起来：

    MqttConsumer  ──submit()──▶  TelemetryIngestor  ──▶  Database
        (网络线程)                   (写库线程)              (SQLite)
                                          ▲
                                          │
    FastAPI 路由 ──get_db()───────────────┘
        (请求线程池)

三者的生命周期都由 lifespan 管：启动时按依赖顺序起（先写库线程，
再连 MQTT），关闭时按相反顺序停（先断 MQTT，再把队列里剩的写完）。

★ 为什么提供 create_app(settings) 工厂，而不是模块级直接建 app：

    测试需要注入**临时数据库**和 **mqtt_enabled=False**。
    模块级单例做不到这一点——所有测试会共用同一个 app、
    同一个数据库，互相污染。

    这也是"显式依赖注入"在 Web 框架里的体现，和 deps.get_db
    从 app.state 取 Database 是同一件事的两面。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api import anomalies, devices, health, predictions, telemetry
from app.config import Settings, get_settings
from app.db.database import Database
from app.logging_conf import setup_logging
from app.mqtt.consumer import MqttConsumer
from app.services.retention import RetentionService
from app.services.telemetry_service import TelemetryIngestor
from app.stats import IngestStats, RetentionStats

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    """装配一个完整的应用实例。

    :param settings: 不传则用 get_settings()（读 .env / 环境变量）。
                     测试传自己的 Settings（临时库 + mqtt_enabled=False）。
    """
    settings = settings or get_settings()
    setup_logging(settings.log_level)

    # ── 数据库 ─────────────────────────────────────────────────
    # 先建库再起线程：写库线程一启动就可能要写，表必须已经存在。
    db = Database(settings)
    db.init_schema()

    # ── 入库管道 ───────────────────────────────────────────────
    # stats 只建一个，MQTT 消费者和入库层**记在同一个对象上**，
    # /api/health 读一处就能拿到全部计数。见 app/stats.py。
    stats = IngestStats()
    ingestor = TelemetryIngestor(db, settings, stats=stats)

    # ── 保留策略 ───────────────────────────────────────────────
    # 独立线程，与数据流解耦。未配置保留天数时它什么都不做
    # （start() 里直接返回，连线程都不建）——这是开发阶段的正常状态。
    retention = RetentionService(db, settings, stats=RetentionStats())

    # ── MQTT（可关）────────────────────────────────────────────
    # mqtt_enabled=False 时完全不建消费者。这样单元测试不需要 broker
    # 就能起 app——否则每个测试都得先起一个 Mosquitto。
    consumer: MqttConsumer | None = None
    if settings.mqtt_enabled:
        consumer = MqttConsumer(settings, ingestor.submit, stats=stats)
    else:
        logger.warning("MQTT 已禁用（mqtt_enabled=False），不会收数据")

    # ── 生命周期 ───────────────────────────────────────────────
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        """启动与关闭。

        ★ 顺序有讲究：

          启动：先起写库线程，再起保留策略线程，最后连 MQTT。
            反过来的话，MQTT 一连上就可能来消息、submit 进队列，
            而没人消费——虽然队列能缓冲，但没必要留这个窗口。
            保留策略排在写库线程之后，是因为它一启动就会删一批数据，
            而写库线程可能同时要写——两者都要抢写锁，
            让写库先起来能避免启动瞬间的锁竞争。

          关闭：先断 MQTT，再停保留策略，最后停写库线程。
            反过来的话，写库线程停了而 MQTT 还在收，
            新消息只会堆在队列里然后随进程一起丢。
            保留策略要排在写库线程之前停：它持写锁，
            留着它会让写库线程最后那批数据的落盘被拖住。
        """
        ingestor.start()
        retention.start()
        if consumer is not None:
            consumer.start()
        logger.info(
            "后端已启动：API=%s:%d broker=%s（mqtt=%s，保留策略=%s）",
            settings.api_host,
            settings.api_port,
            settings.mqtt_broker_repr,
            "启用" if consumer is not None else "禁用",
            f"{settings.telemetry_retention_days} 天"
            if retention.enabled
            else "未启用",
        )
        try:
            yield
        finally:
            if consumer is not None:
                consumer.stop()
            retention.stop()
            ingestor.stop()
            db.dispose()
            logger.info("后端已停止")

    app = FastAPI(
        title="IoT 环境监测系统 · 后端",
        description=(
            "ESP32-S3 温湿度节点的数据接入与查询服务。\n\n"
            "对应设计文档 Phase 6（§19-§23）。"
        ),
        version=__version__,
        lifespan=lifespan,
    )

    # 把这些挂到 app.state 上，deps.get_db 和 health 路由从这里取。
    # 用 state 而不是模块级全局，是为了让 create_app 可以造多个实例。
    app.state.settings = settings
    app.state.db = db
    app.state.ingestor = ingestor
    app.state.retention = retention
    app.state.consumer = consumer

    # ── CORS ───────────────────────────────────────────────────
    # 开发阶段全放开（前端还没做，将来 Vue 可能跑在别的端口）。
    # 上线前把 CORS_ALLOW_ORIGINS 收窄到实际域名。
    #
    # allow_credentials 保持 False：配 allow_origins=["*"] 时浏览器
    # 不允许带凭据，写了反而会被静默忽略（Starlette 会在启动时警告）。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=False,
        allow_methods=["GET"],
        allow_headers=["*"],
    )

    # ── 路由 ───────────────────────────────────────────────────
    # 七个路由来自设计文档 §23。注意 §19 给的目录结构里只有
    # devices/telemetry/anomalies 三个文件，但 §23 要求七个路由——
    # health 和 predictions 没有对应文件，所以这里补上了。
    app.include_router(health.router)
    app.include_router(devices.router)
    app.include_router(telemetry.router)
    app.include_router(anomalies.router)
    app.include_router(predictions.router)

    # ── Web 前端（静态文件）────────────────────────────────────
    #
    # ★★ 这个 mount 必须放在**所有 API 路由之后**，这不是风格问题。
    #
    #   Starlette 按**注册顺序**匹配路由，而挂在 "/" 的 StaticFiles
    #   会匹配所有路径。先挂它的话，/api/health 也会被它吃掉并返回
    #   404（静态文件里没有这个名字的文件）——症状是"页面能打开、
    #   但所有数据都是空的"，很容易误判成前端的问题去查 JS。
    #
    # ★ dist 不存在时**不挂载**，只打一条 WARNING。
    #
    #   后端不该因为"前端还没构建"而起不来。API 是独立可用的，
    #   而 /docs（FastAPI 自带的接口文档）也是排查时的工具——
    #   让容器起不来等于把排查手段一起关掉。
    #   本机直跑 uvicorn 时必然走这条路径（前端跑在 Vite dev server 上），
    #   所以这条 WARNING 在本机开发时是正常的。
    dist_dir = settings.web_dist_path
    if dist_dir.is_dir():
        app.mount(
            "/",
            # html=True：请求目录时返回该目录下的 index.html。
            # 也就是 GET / 会拿到 index.html。
            #
            # ★ 一个已知限制：它**不做 SPA 回退**。
            #   本轮是单页、没有客户端路由，所以不影响。
            #   将来加了 vue-router 的 history 模式（/devices、/anomalies
            #   这类路径），直接刷新那些地址会 404——因为磁盘上没有
            #   对应的文件。那时需要一个 catch-all 路由把未知路径
            #   都返回 index.html。
            StaticFiles(directory=str(dist_dir), html=True),
            name="web",
        )
        logger.info("已挂载 Web 前端：%s", dist_dir)
    else:
        logger.warning(
            "未找到 Web 前端产物（%s），本次只提供 API。"
            "容器里出现这条 = Dockerfile 的 COPY --from=web-build 没生效；"
            "本机直跑时出现这条是正常的。",
            dist_dir,
        )

    return app


def __getattr__(name: str) -> Any:
    """延迟创建模块级的 ``app``（PEP 562）。

    ★ 为什么用延迟属性，而不是直接 ``app = create_app()``：

      uvicorn 的 ``app.main:app`` 需要在导入时就能拿到 app 对象。
      但**测试只需要 create_app 工厂**——如果模块一被 import 就
      建好 app，那测试导入 ``from app.main import create_app`` 时
      会顺带把**生产数据库**（/home/ubuntu/iot-data/iot.db）建出来，
      还会把日志配置改掉。测试污染生产数据是最不该发生的事。

      延迟之后：
        uvicorn 取 app.main:app  → 触发创建（正是我们要的）
        测试取 app.main.create_app → 不触发（模块里没有别的副作用）

      这是 PEP 562 的标准用法，不是黑魔法。
    """
    if name == "app":
        instance = create_app()
        # 缓存起来，后续访问不再重建（uvicorn 可能取多次）
        globals()["app"] = instance
        return instance
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

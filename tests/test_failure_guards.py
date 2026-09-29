"""失败守卫的测试——专门覆盖那些**出问题时没有任何症状**的路径。

★ 为什么这个文件单独存在：

  本工程有一类故障的共同形态是"所有健康指标都正常，只有数据不见了"：

    1. 写库线程因未捕获异常退出 → MQTT 连接正常、队列只进不出，
       但数据不再落库
    2. schema 版本不匹配 → 报的是 "no such column: xxx"，
       看起来像代码 bug 而不是"库是旧的"
    3. 库文件放在 DrvFs 上 → WAL 静默降级，开发早期不报错

  这些路径**正常运行永远不会走到**，所以覆盖率天然很低；
  但它们恰恰是最该有测试的地方——没测试的话，等你需要它工作时
  才发现它本来就是坏的。

  换句话说：这个文件测的不是"功能对不对"，而是"出事时会不会
  静默地把事情搞得更糟"。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

import app.main as main_module
from app.config import Settings
from app.db import repository as repo
from app.db.database import Database
from app.main import create_app
from app.services.telemetry_service import TelemetryIngestor
from tests.test_ingestor import make_telemetry


def _wait_until(predicate: Any, *, timeout_s: float = 8.0) -> bool:
    """轮询等待条件成立。

    用轮询而不是固定 sleep：写库线程的时序受调度影响，
    固定 sleep 要么太短（偶发失败）要么太长（每次跑都慢）。
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


# ── 1. 写库线程必须扛住异常 ─────────────────────────────────────────


def test_write_thread_survives_batch_exception(
    db: Database, settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """★ 本文件最重要的用例。

    写库线程一旦因未捕获异常退出，就**再也不会写库**——
    而 MQTT 连接、/api/health 的 connected、队列深度全都正常，
    只有"数据不再增加"这一个症状，而且没有任何错误日志
    （因为线程已经死了，没人打日志）。

    这里让 handle_batch 第一次抛异常、之后正常，断言：
      · 线程还活着
      · **后续的数据照样落库**（丢掉的是那一批，不是之后所有数据）
    """
    ingestor = TelemetryIngestor(db, settings)
    real_handle_batch = ingestor.handle_batch
    calls = {"n": 0}

    def flaky(session: Any, items: Any) -> dict[str, int]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("模拟一次写入失败")
        return real_handle_batch(session, items)

    ingestor.handle_batch = flaky  # type: ignore[method-assign]

    ingestor.start()
    assert ingestor.running is True

    ingestor.submit(make_telemetry(1, ts=1_790_600_000_000, received_ms=1_790_600_000_000))

    # 等第一次（注定失败的那次）跑完
    assert _wait_until(lambda: calls["n"] >= 1)

    # ★ 线程必须还活着
    assert ingestor.running is True, "写库线程被异常杀死了"

    # ★ 第二条必须照样落库
    ingestor.submit(make_telemetry(2, ts=1_790_600_002_000, received_ms=1_790_600_002_000))
    assert _wait_until(
        lambda: _count(db) >= 1, timeout_s=8.0
    ), "异常之后的数据没有被写入——说明线程实际上已经死了"

    ingestor.stop()
    assert ingestor.running is False


def _count(db: Database) -> int:
    with db.session() as s:
        return repo.count_telemetry(s)


def test_running_is_false_before_start_and_after_stop(
    db: Database, settings: Settings
) -> None:
    ingestor = TelemetryIngestor(db, settings)
    assert ingestor.running is False

    ingestor.start()
    assert ingestor.running is True

    ingestor.stop()
    assert ingestor.running is False


# ── 2. schema 版本不匹配必须明确报错 ────────────────────────────────


def test_schema_version_mismatch_raises(tmp_path: Path) -> None:
    """库的 schema 版本与代码不一致 → 启动时抛异常，而不是运行期报 SQL 错。

    不检查版本的话，症状是某次查询报 "no such column: received_at"——
    看起来像代码写错了，实际是库还是旧结构。开发期没有 Alembic，
    正确做法就是删库重建，而那需要先知道"是版本问题"。
    """
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'iot.db'}",
        mqtt_enabled=False,
    )
    database = Database(settings)
    database.init_schema()

    # 手工把版本号改成别的值，模拟"库是旧代码建的"
    with database.engine.begin() as conn:
        conn.execute(text("PRAGMA user_version=999"))

    with pytest.raises(RuntimeError, match="schema 版本不匹配"):
        database.init_schema()

    database.dispose()


def test_init_schema_is_idempotent(tmp_path: Path) -> None:
    """重复调 init_schema 不该报错（容器重启时会再走一遍）。"""
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'iot.db'}",
        mqtt_enabled=False,
    )
    database = Database(settings)
    database.init_schema()
    database.init_schema()  # 第二次：走 "schema vN 已是最新" 分支
    database.dispose()


# ── 3. 数据库路径与内存库 ───────────────────────────────────────────


def test_creates_missing_parent_directory(tmp_path: Path) -> None:
    """库文件的父目录不存在时要自动建。

    不建的话报 "unable to open database file"——看起来像权限问题，
    实际是目录不存在。容器里挂数据卷时最容易碰到。
    """
    db_path = tmp_path / "nested" / "deep" / "iot.db"
    assert not db_path.parent.exists()

    settings = Settings(
        _env_file=None, database_url=f"sqlite:///{db_path}", mqtt_enabled=False
    )
    database = Database(settings)
    database.init_schema()

    assert db_path.parent.exists()
    assert db_path.exists()
    database.dispose()


def test_in_memory_db_skips_wal_check() -> None:
    """内存库不支持 WAL，必须跳过校验而不是抛异常。

    内存库在测试里是方便，但**不能用**——SQLAlchemy 对它用
    SingletonThreadPool，每个线程一个独立的空库，写库线程和测试线程
    看到的是两个库。所以它只是"允许存在"，不是"推荐使用"。
    （见 tests/conftest.py 的说明。）
    """
    settings = Settings(
        _env_file=None, database_url="sqlite:///:memory:", mqtt_enabled=False
    )
    database = Database(settings)  # 不该抛
    database.init_schema()
    database.dispose()


# ── 4. /api/health 的降级分支 ───────────────────────────────────────


def test_health_reports_component_liveness(settings: Settings) -> None:
    """两个后台线程的存活状态必须能从健康检查看到。

    这是"静默死亡"类故障的唯一外部观测点：
    线程没了但进程还在，如果不报出来，你只能靠"数据没涨"去反推。
    """
    app = create_app(settings)
    with TestClient(app) as client:
        body = client.get("/api/health").json()

    assert body["ingest"]["running"] is True
    assert body["retention"]["running"] is False  # 测试夹具里保留天数=0


def test_health_falls_back_when_components_missing(settings: Settings) -> None:
    """组件缺失时 health 要返回降级信息，而不是 500。

    为什么会有这种状态：``create_app`` 里 MQTT 是可关的，
    而 ingestor / retention 将来也可能因为配置或初始化失败而不存在。
    健康检查在这种时候**更**需要能打开——它正是你用来诊断"为什么
    组件没起来"的工具。如果它自己 500 了，就等于把最后一只眼睛也闭上。
    """
    app = create_app(settings)
    with TestClient(app) as client:
        del app.state.ingestor
        del app.state.retention

        body = client.get("/api/health").json()

    assert body["ingest"] == {"enabled": False, "running": False}
    assert body["retention"] == {"enabled": False, "running": False}


def test_health_sections_include_retention(settings: Settings) -> None:
    """retention 段是 Step 5 加的，别在后续改动里被漏掉。"""
    app = create_app(settings)
    with TestClient(app) as client:
        body = client.get("/api/health").json()

    for section in ("status", "now_ms", "mqtt", "ingest", "db",
                    "retention", "data", "settings"):
        assert section in body, f"缺少 {section} 段"


# ── 5. 模块级延迟创建的 app ─────────────────────────────────────────


def test_module_getattr_raises_for_unknown_attribute() -> None:
    """PEP 562 的 ``__getattr__`` 只认 "app"，别的属性要正常抛 AttributeError。

    不抛的话，``hasattr(app.main, "anithing")`` 会返回 True，
    各种"探测属性存在性"的库行为会变得不可预期。
    """
    with pytest.raises(AttributeError, match="has no attribute"):
        _ = main_module.definitely_not_an_attribute


def test_module_getattr_does_not_create_app_on_import() -> None:
    """★ 导入 ``app.main`` 不该顺带创建生产 app。

    这是 PEP 562 延迟属性的**全部意义**：测试只需要 ``create_app``
    工厂，如果模块一被 import 就建 app，测试导入时会连带把
    **生产数据库**（/home/ubuntu/iot-data/iot.db）建出来、
    还会改掉日志配置。

    测试污染生产数据是最不该发生的事，所以这条要锁住。
    """
    assert "app" not in vars(main_module), (
        "导入 app.main 时就把 app 建出来了——生产数据库会被测试创建"
    )


# ── 6. sink 契约 ────────────────────────────────────────────────────

def test_submit_never_blocks_when_queue_full(db: Database) -> None:
    """队列满时 submit() 必须立刻返回，**绝不阻塞**。

    它跑在 paho 的网络线程上。阻塞等待就等于把网络线程交给写库线程的
    进度决定——写库慢一点，PINGREQ 就晚发一点，最后 broker 判定本端
    已死并断开，触发重连、重投、雪崩。

    宁可丢数据也不能阻塞回调。这里不启动写库线程（没人消费），
    把队列灌满，断言 submit 仍然快速返回且丢弃被计数。
    """
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{db.engine.url.database}",
        mqtt_enabled=False,
        ingest_queue_max=10,  # 下限就是 10，用小值把队列灌满
    )
    ingestor = TelemetryIngestor(db, settings)

    start = time.monotonic()
    for i in range(1, 51):
        ingestor.submit(
            make_telemetry(i, ts=1_790_600_000_000 + i, received_ms=1_790_600_000_000 + i)
        )
    elapsed = time.monotonic() - start

    assert elapsed < 1.0, f"submit 阻塞了 {elapsed:.2f}s——回调会被拖死"
    assert ingestor.queue_depth == 10
    assert ingestor.stats.queue_dropped == 40
    # 水位贴着上限，说明"经常接近满"这件事被记录下来了
    assert ingestor.stats.queue_high_watermark == 10


# ── 7. Web 静态文件的挂载 ───────────────────────────────────────────


def _web_settings(tmp_path: Path, *, with_dist: bool) -> Settings:
    """造一份带（或不带）前端产物的配置。"""
    dist = tmp_path / "web" / "dist"
    if with_dist:
        dist.mkdir(parents=True)
        (dist / "index.html").write_text(
            "<!DOCTYPE html><title>iot-web</title><div id=app></div>", encoding="utf-8"
        )
        # 一个带子目录的静态资源，验证嵌套路径也能取到
        assets = dist / "assets"
        assets.mkdir()
        (assets / "app.js").write_text("console.log('ok')", encoding="utf-8")

    return Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'iot.db'}",
        mqtt_enabled=False,
        web_dist_dir=str(dist),
    )


def test_web_mount_does_not_shadow_api_routes(tmp_path: Path) -> None:
    """★ 静态文件挂在 "/" **不能**吃掉 /api 路由。

    Starlette 按注册顺序匹配，而挂在 "/" 的 StaticFiles 会匹配所有路径。
    如果它被注册在 API 路由之前，/api/health 会被它当成"一个不存在的
    静态文件"返回 404 —— 症状是**页面能打开、但所有数据都是空的**，
    极容易被误判成前端 JS 的问题去查半天。

    所以这个用例断言的不是"首页能打开"，而是"首页能打开**并且**
    API 还能用"。只测前者的话，顺序写反了照样通过。
    """
    settings = _web_settings(tmp_path, with_dist=True)
    app = create_app(settings)

    with TestClient(app) as client:
        # 首页：StaticFiles(html=True) 把 / 映射到 index.html
        home = client.get("/")
        assert home.status_code == 200
        assert "iot-web" in home.text

        # 嵌套的静态资源
        assert client.get("/assets/app.js").status_code == 200

        # ★ 关键：API 没有被吃掉
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/devices").status_code == 200

        # FastAPI 自带的接口文档也还在（它是排查时的工具）
        assert client.get("/openapi.json").status_code == 200


def test_web_mount_skipped_when_dist_missing(tmp_path: Path) -> None:
    """★ 前端产物不存在时**不挂载**，但后端必须照常起来。

    后端不该因为"前端还没构建"而起不来——API 是独立可用的，
    而且 /docs 是排查时的工具。让容器起不来等于把排查手段一起关掉。

    本机直跑 uvicorn（前端跑在 Vite dev server 上）时必然走这条路径。
    """
    settings = _web_settings(tmp_path, with_dist=False)
    app = create_app(settings)  # 不该抛

    with TestClient(app) as client:
        # API 正常
        assert client.get("/api/health").status_code == 200
        # 根路径没有东西可提供 → 404（而不是 500 或崩溃）
        assert client.get("/").status_code == 404


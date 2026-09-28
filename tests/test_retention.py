"""保留策略的单元测试。

★ 不依赖 broker、不依赖设备、不依赖真实时钟。

  数据用 ``TelemetryIngestor.handle_batch()`` 造（也就是走真实的入库路径），
  时间用 ``now=`` 参数显式注入——所以"删掉 3 天前的数据"这类断言
  不需要真的等三天，也不需要 mock 时间。

  这一点很重要：如果清理逻辑依赖"当前时间"这个隐式输入，
  测试就只能靠把库造得很旧来绕，而那种测试要么很慢，要么很脆。
  把 ``now`` 变成显式参数之后，"过没过期"变成了一个纯粹的比较。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.clock import now_ms
from app.config import Settings
from app.db.database import Database
from app.db.models import Telemetry
from app.main import create_app
from app.services.retention import MS_PER_DAY, RetentionService
from app.services.telemetry_service import TelemetryIngestor
from app.stats import RetentionStats
from tests.test_ingestor import DEVICE, make_status, make_telemetry

# 造数据的规模。
N_OLD = 250     # 10 天前的 unix 行——应该被删
N_NEW = 4       # 刚刚的 unix 行——应该保留
N_BOOT = 2      # 10 天前的 boot 行——**必须保留**（这是本文件的核心用例）
OLD_DAYS = 10   # 造的数据有多旧
RETENTION_DAYS = 3  # 保留 3 天 → 10 天前的算过期

# 分块大小。取配置允许的最小值（100，见 config.retention_chunk_size）。
#
# ★ 这里刻意不去用"更小的数字好让测试更省事"——那需要绕过 Settings 的
#   字段校验（model_copy 不重新校验），于是测试跑的是一份**生产环境
#   根本配不出来**的参数。250 行旧数据配 chunk=100 正好跨 3 块，
#   既验证了分块，又保证测试用的是真实可用的配置。
CHUNK = 100
EXPECTED_CHUNKS = 3  # 100 + 100 + 50


# ── 夹具与工具 ──────────────────────────────────────────────────────


@pytest.fixture
def ingestor(db: Database, settings: Settings) -> TelemetryIngestor:
    return TelemetryIngestor(db, settings)


def _retention_settings(settings: Settings, **over: object) -> Settings:
    """在测试夹具的基础上改保留策略配置。

    用 ``model_copy(update=...)`` 而不是重新构造 Settings：
    重新构造会丢掉夹具里注入的临时数据库路径（那些是构造参数，
    不在环境变量里）。model_copy 只覆盖指定字段，其余原样保留。

    ★ 注意 model_copy **不重新跑字段校验**（pydantic 的既定行为）。
      所以这里传的值必须自己保证在合法范围内——测试用一份生产环境
      配不出来的参数，等于没测真实行为。
    """
    base = {"telemetry_retention_days": RETENTION_DAYS, "retention_chunk_size": CHUNK}
    base.update(over)
    return settings.model_copy(update=base)


def _seed(db: Database, ingestor: TelemetryIngestor, *, now: int) -> None:
    """造三类数据：旧的 unix 行、新的 unix 行、旧的 boot 行。"""
    old_base = now - OLD_DAYS * MS_PER_DAY
    with db.session() as s:
        ingestor.handle_batch(s, [make_status("online")])

        rows = []
        # 旧的 unix 行：received_at 在 10 天前
        for i in range(N_OLD):
            t = old_base + i * 2000
            rows.append(make_telemetry(i + 1, ts=t, received_ms=t))
        # 新的 unix 行：received_at 就在刚刚
        for i in range(N_NEW):
            t = now - 8000 + i * 2000
            rows.append(make_telemetry(100 + i, ts=t, received_ms=t))
        # ★ 旧的 boot 行：ts 只有几千（开机毫秒），但 received_at 也是 10 天前。
        #   这正是"按 received_at 判过期会误伤它"的场景。
        #   seq 取 900+ 是为了不和上面的 unix 行撞号——虽然
        #   唯一约束是 (device_id, ts, seq)、ts 差得很远本来就不会撞，
        #   但用一眼能分开的号段能让人更快看懂测试数据。
        for i in range(N_BOOT):
            rows.append(
                make_telemetry(
                    900 + i,
                    clock="boot",
                    ts=5000 + i,
                    received_ms=old_base + i,
                )
            )

        ingestor.handle_batch(s, rows)


def _counts(db: Database) -> dict[str, int]:
    """按 clock 分类统计行数。"""
    with db.session() as s:
        total = int(
            s.execute(select(func.count()).select_from(Telemetry)).scalar() or 0
        )
        boot = int(
            s.execute(
                select(func.count()).select_from(Telemetry).where(Telemetry.clock == "boot")
            ).scalar()
            or 0
        )
    return {"total": total, "boot": boot, "unix": total - boot}


# ── 未启用时的行为 ──────────────────────────────────────────────────


def test_disabled_when_retention_days_is_zero(
    db: Database, settings: Settings, ingestor: TelemetryIngestor
) -> None:
    """保留天数为 0 = 不清理，这是默认配置。

    ★ 断言的重点是"一行都不删"，而不是"没报错"。
      把 0 解释成"删掉所有比现在旧的行"（等价于清空）是个很容易
      写出来的实现，而这个用例专门挡住它。
    """
    now = now_ms()
    _seed(db, ingestor, now=now)
    before = _counts(db)

    svc = RetentionService(db, settings)  # 夹具里 retention_days = 0

    assert svc.enabled is False
    assert svc.run_once(now=now) is None
    assert _counts(db) == before


def test_worker_is_not_started_when_disabled(
    db: Database, settings: Settings
) -> None:
    """未启用时连线程都不该建。"""
    svc = RetentionService(db, settings)
    svc.start()
    assert svc.running is False
    svc.stop()  # 幂等，不该抛异常


# ── 正常清理 ────────────────────────────────────────────────────────


def test_prunes_only_rows_older_than_cutoff(
    db: Database, settings: Settings, ingestor: TelemetryIngestor
) -> None:
    now = now_ms()
    _seed(db, ingestor, now=now)

    svc = RetentionService(db, _retention_settings(settings))
    outcome = svc.run_once(now=now)

    assert outcome is not None
    assert outcome.deleted == N_OLD
    # 新的 unix 行 + 旧的 boot 行都要留着
    assert _counts(db) == {"total": N_NEW + N_BOOT, "boot": N_BOOT, "unix": N_NEW}


def test_boot_rows_are_never_pruned(
    db: Database, settings: Settings, ingestor: TelemetryIngestor
) -> None:
    """★ 本文件最重要的用例。

    boot 行的 ts 只有几千（开机毫秒）。如果清理语句按 ts 判"旧"，
    或者忘了带 clock='unix' 条件，它们会被全部删掉——而它们是
    排查"设备为什么没同步上时间"的唯一线索，且每次开机只有几秒的量，
    留着没有任何成本。

    这里特意把它们的 received_at 也造在 10 天前：即使按 received_at 判，
    它们也"过期"了，所以这个断言真的在考验"clock='unix' 条件"这一条。
    """
    now = now_ms()
    _seed(db, ingestor, now=now)

    svc = RetentionService(db, _retention_settings(settings))
    svc.run_once(now=now)

    assert _counts(db)["boot"] == N_BOOT


def test_chunked_delete_spans_multiple_chunks(
    db: Database, settings: Settings, ingestor: TelemetryIngestor
) -> None:
    """分块逻辑真的在分块。

    250 行旧数据 + chunk=100 → 100 / 100 / 50 三批。
    如果实现写成了"一次删干净"，chunks 会是 1——那时这个断言失败，
    提醒我们"每批之间提交"这个设计没有被落实。
    """
    now = now_ms()
    _seed(db, ingestor, now=now)

    svc = RetentionService(db, _retention_settings(settings))
    outcome = svc.run_once(now=now)

    assert outcome is not None
    assert outcome.deleted == N_OLD
    assert outcome.chunks == EXPECTED_CHUNKS


def test_cutoff_matches_retention_days(
    db: Database, settings: Settings
) -> None:
    now = 1_790_000_000_000
    svc = RetentionService(db, _retention_settings(settings, telemetry_retention_days=7))

    assert svc.cutoff_ms(now=now) == now - 7 * MS_PER_DAY


def test_outcome_reports_cutoff_and_timing(
    db: Database, settings: Settings, ingestor: TelemetryIngestor
) -> None:
    now = now_ms()
    _seed(db, ingestor, now=now)

    svc = RetentionService(db, _retention_settings(settings))
    outcome = svc.run_once(now=now)

    assert outcome is not None
    assert outcome.cutoff_ms == now - RETENTION_DAYS * MS_PER_DAY
    assert outcome.at_ms == now
    assert outcome.duration_ms >= 0


def test_second_run_deletes_nothing(
    db: Database, settings: Settings, ingestor: TelemetryIngestor
) -> None:
    """清理是幂等的：第二次跑没有可删的了。

    这条保证"每小时检查一次"这个频率是安全的——不会因为反复跑而
    误删别的东西。
    """
    now = now_ms()
    _seed(db, ingestor, now=now)
    svc = RetentionService(db, _retention_settings(settings))

    first = svc.run_once(now=now)
    second = svc.run_once(now=now)

    assert first is not None and first.deleted == N_OLD
    assert second is not None and second.deleted == 0


# ── 计数器 ──────────────────────────────────────────────────────────


def test_stats_are_updated(
    db: Database, settings: Settings, ingestor: TelemetryIngestor
) -> None:
    now = now_ms()
    _seed(db, ingestor, now=now)

    stats = RetentionStats()
    svc = RetentionService(db, _retention_settings(settings), stats=stats)
    svc.run_once(now=now)

    assert stats.runs == 1
    assert stats.deleted_total == N_OLD
    assert stats.last_deleted == N_OLD
    assert stats.last_cutoff_ms == now - RETENTION_DAYS * MS_PER_DAY
    assert stats.last_run_ms == now

    snap = stats.snapshot()
    assert snap["deleted_total"] == N_OLD
    assert set(snap) == {
        "runs",
        "deleted_total",
        "last_deleted",
        "last_cutoff_ms",
        "last_run_ms",
        "last_duration_ms",
    }


def test_stats_not_touched_when_disabled(
    db: Database, settings: Settings
) -> None:
    stats = RetentionStats()
    svc = RetentionService(db, settings, stats=stats)

    assert svc.run_once(now=now_ms()) is None
    assert stats.runs == 0


# ── 线程 ────────────────────────────────────────────────────────────


def test_worker_starts_and_stops(db: Database, settings: Settings) -> None:
    """线程能起来、能停下来。

    ★ 用短周期（60 秒是允许的最小值），并且只验证"起来了/停下来了"，
      不去等它跑一轮——那会让用例慢一分钟。

      真正验证"它确实会周期性清理"靠的是 run_once() 的用例，
      加上这里对 running 状态的断言。
    """
    svc = RetentionService(db, _retention_settings(settings))
    svc.start()
    assert svc.running is True

    # stop() 必须立刻返回，不能等一整个周期（这正是用 Event.wait 而非 sleep 的意义）
    svc.stop()
    assert svc.running is False


def test_safe_run_once_swallows_exception(
    db: Database, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 异常必须被吞掉。

    保留线程一旦因未捕获异常退出，就**永远不再清理**，而这件事
    没有任何外部症状——直到某天发现磁盘满了。所以循环体必须有兜底。

    这里直接调 _safe_run_once()（线程内部用的就是它），
    避免依赖线程调度时序。
    """

    def boom(*args: object, **kwargs: object) -> int:
        raise RuntimeError("模拟删除失败")

    monkeypatch.setattr("app.db.repository.delete_old_telemetry_chunk", boom)
    svc = RetentionService(db, _retention_settings(settings))

    svc._safe_run_once()  # 不该抛出

    # 失败的一轮不该被记成"成功跑过一次"
    assert svc.stats.runs == 0


# ── /api/health 的 retention 段 ─────────────────────────────────────


def test_health_reports_retention_section(settings: Settings) -> None:
    """健康检查里要有 retention 段，且能反映"未启用"这个正常状态。"""
    app = create_app(settings)
    with TestClient(app) as client:
        body = client.get("/api/health").json()

    assert "retention" in body
    assert body["retention"]["enabled"] is False
    assert body["retention"]["running"] is False
    assert body["retention"]["deleted_total"] == 0


def test_health_reports_retention_enabled(tmp_path: Path) -> None:
    """启用之后，配置项要能通过健康检查看到。

    为什么值得单独测：保留策略"没生效"的症状是磁盘满，
    而那时离根因已经很远。健康检查必须能直接回答"它到底开着没"。

    这里不用 settings/db 夹具，而是自己造一份：需要"库 + 启用了保留策略"
    同时成立，而夹具里的 retention_days 固定是 0。
    """
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'iot.db'}",
        mqtt_enabled=False,
        telemetry_retention_days=RETENTION_DAYS,
        retention_chunk_size=CHUNK,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        body = client.get("/api/health").json()

    assert body["retention"]["enabled"] is True
    assert body["retention"]["running"] is True
    assert body["retention"]["retention_days"] == RETENTION_DAYS
    assert body["retention"]["chunk_size"] == CHUNK

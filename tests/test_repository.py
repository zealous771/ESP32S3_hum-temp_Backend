"""仓储层查询的测试。

★ 重点不是"查询能跑"，而是**每个过滤条件真的生效**。

  ``query_anomalies`` / ``query_predictions`` 现在两张表都是空的——
  按"只测返回值"的写法，它们永远返回 ``(0, [])``，怎么写都通过。
  但 Phase 8 的 AI 层一开始写数据，这些过滤器就会立刻被前端用上：
  到那时才发现"metric 参数没接进 SQL"要比现在贵得多。

  所以这里**手工往表里塞几行**，然后逐个条件验证筛选结果。

★ 为什么可以直接往 anomalies / predictions 塞数据：

  它们是普通表，没有外键依赖（只有 device_id 这个逻辑关联）。
  塞进去之后这些用例就同时充当了"Phase 8 的表结构能不能用"的验证。
"""

from __future__ import annotations

from typing import Any, Sequence

import pytest

from app.config import Settings
from app.db import repository as repo
from app.db.database import Database
from app.db.models import Anomaly, Prediction
from app.services.telemetry_service import TelemetryIngestor
from tests.test_ingestor import make_status, make_telemetry

DEVICE = "esp32s3_001"
OTHER = "esp32s3_002"

# 一个真实的 Unix 毫秒基准。用固定值而不是 now()，
# 这样断言里的时间范围是确定的，不受测试运行时刻影响。
BASE_TS = 1_790_600_000_000


@pytest.fixture
def ingestor_seed(db: Database, settings: Settings) -> None:
    """塞几条真实形态的遥测（走真实的入库路径）。

    用于验证"按设备过滤"这类需要数据的查询。走 handle_batch 而不是
    直接 INSERT，是为了让种子数据和真实数据形态一致。
    """
    ingestor = TelemetryIngestor(db, settings)
    with db.session() as s:
        ingestor.handle_batch(s, [make_status("online")])
        ingestor.handle_batch(
            s,
            [
                make_telemetry(i, ts=BASE_TS + i * 2000, received_ms=BASE_TS + i * 2000)
                for i in range(1, 6)
            ],
        )


def _ensure_devices(db: Database, *device_ids: str) -> None:
    """先建设备行。

    ★ 这一步不能省：``anomalies`` / ``predictions`` 的 device_id 是
      指向 ``devices`` 的**外键**，而 database.py 里开了
      ``PRAGMA foreign_keys=ON``。不先建设备就插异常记录会直接
      IntegrityError——这正是外键该有的行为（实测验证过）。
    """
    with db.session() as s:
        for device_id in device_ids:
            repo.upsert_device(s, device_id, seen_ms=BASE_TS, status="online")


def _seed_anomalies(db: Database) -> None:
    _ensure_devices(db, DEVICE, OTHER)
    with db.session() as s:
        s.add_all(
            [
                Anomaly(device_id=DEVICE, ts=BASE_TS + 1000, metric="temperature",
                        score=0.82, level="warning", reason="zscore 超阈值"),
                Anomaly(device_id=DEVICE, ts=BASE_TS + 2000, metric="humidity",
                        score=0.95, level="critical", reason="变化率异常"),
                Anomaly(device_id=OTHER, ts=BASE_TS + 3000, metric="temperature",
                        score=0.55, level="info", reason="规则命中"),
            ]
        )


def _seed_predictions(db: Database) -> None:
    _ensure_devices(db, DEVICE, OTHER)
    with db.session() as s:
        s.add_all(
            [
                Prediction(device_id=DEVICE, ts=BASE_TS + 1000, horizon=5,
                           metric="temperature", value=33.5),
                Prediction(device_id=DEVICE, ts=BASE_TS + 1000, horizon=10,
                           metric="temperature", value=34.1),
                Prediction(device_id=DEVICE, ts=BASE_TS + 2000, horizon=5,
                           metric="humidity", value=70.0),
                Prediction(device_id=OTHER, ts=BASE_TS + 3000, horizon=5,
                           metric="temperature", value=31.0),
            ]
        )


# ── 薄封装：仓储层收的是 Session，测试里传 Database 更方便 ──────────
#
# 注意这里**不是**在绕开什么：Database.session() 就是一个事务性 Session，
# 和 API 层通过 Depends(get_db) 拿到的是同一种东西。


def _anomalies(db: Database, **kw: Any) -> tuple[int, Sequence[Any]]:
    with db.session() as s:
        return repo.query_anomalies(s, **kw)


def _predictions(db: Database, **kw: Any) -> tuple[int, Sequence[Any]]:
    with db.session() as s:
        return repo.query_predictions(s, **kw)


def _last_received(db: Database, device_id: str | None = None) -> int | None:
    with db.session() as s:
        return repo.last_received_at(s, device_id)


def _gaps(db: Database, **kw: Any) -> Sequence[Any]:
    with db.session() as s:
        return repo.find_receive_gaps(s, **kw)


# ── query_anomalies ─────────────────────────────────────────────────


def test_query_anomalies_no_filter_returns_all(db: Database) -> None:
    _seed_anomalies(db)
    total, rows = _anomalies(db)
    assert total == 3
    assert len(rows) == 3
    # 默认按 ts 倒序 —— "先看最近的异常"是唯一合理的默认
    assert [r.ts for r in rows] == [BASE_TS + 3000, BASE_TS + 2000, BASE_TS + 1000]


def test_query_anomalies_filters_by_device(db: Database) -> None:
    _seed_anomalies(db)
    total, rows = _anomalies(db, device_id=DEVICE)
    assert total == 2
    assert {r.device_id for r in rows} == {DEVICE}


def test_query_anomalies_filters_by_metric_and_level(db: Database) -> None:
    _seed_anomalies(db)
    assert _anomalies(db, metric="temperature")[0] == 2
    assert _anomalies(db, level="critical")[0] == 1
    assert _anomalies(db, metric="temperature", level="info")[0] == 1


def test_query_anomalies_filters_by_time_range(db: Database) -> None:
    """时间范围是**闭区间**（>= start 且 <= end）。

    边界含不含端点值得单独断言一次：前端画曲线时会把"从这个时刻到那个
    时刻"直接传进来，差一个端点就是少一个点，而曲线上看不出来。
    """
    _seed_anomalies(db)
    assert _anomalies(db, start_ms=BASE_TS + 2000)[0] == 2
    assert _anomalies(db, end_ms=BASE_TS + 1000)[0] == 1
    assert _anomalies(db, start_ms=BASE_TS + 1000,
                                end_ms=BASE_TS + 2000)[0] == 2


def test_query_anomalies_filters_by_min_score(db: Database) -> None:
    _seed_anomalies(db)
    assert _anomalies(db, min_score=0.8)[0] == 2
    assert _anomalies(db, min_score=0.9)[0] == 1
    assert _anomalies(db, min_score=0.99)[0] == 0


def test_query_anomalies_paginates(db: Database) -> None:
    """total 是**过滤后的总数**，不是本页条数。

    分页组件要靠 total 算总页数；返回本页长度的话页数永远是 1。
    """
    _seed_anomalies(db)
    total, rows = _anomalies(db, limit=2, offset=0)
    assert total == 3 and len(rows) == 2

    total, rows = _anomalies(db, limit=2, offset=2)
    assert total == 3 and len(rows) == 1


# ── query_predictions ───────────────────────────────────────────────


def test_query_predictions_no_filter_returns_all(db: Database) -> None:
    _seed_predictions(db)
    total, rows = _predictions(db)
    assert total == 4 and len(rows) == 4


def test_query_predictions_filters_by_horizon(db: Database) -> None:
    """horizon 是"预测多远"（分钟）。

    ★ 注意 horizon 与 ts 的语义容易混：ts 是**做出预测的时刻**，
      被预测的时刻 = ts + horizon。这里断言的是"按 horizon 过滤"
      这个动作本身正确，不涉及那个换算——换算在 ai/forecast.py 里。
    """
    _seed_predictions(db)
    assert _predictions(db, horizon=5)[0] == 3
    assert _predictions(db, horizon=10)[0] == 1


def test_query_predictions_filters_by_device_metric_and_time(db: Database) -> None:
    _seed_predictions(db)
    assert _predictions(db, device_id=DEVICE)[0] == 3
    assert _predictions(db, metric="temperature")[0] == 3
    assert _predictions(db, device_id=DEVICE, metric="humidity")[0] == 1
    assert _predictions(db, start_ms=BASE_TS + 2000)[0] == 2
    assert _predictions(db, end_ms=BASE_TS + 1000)[0] == 2


def test_query_predictions_paginates(db: Database) -> None:
    _seed_predictions(db)
    total, rows = _predictions(db, limit=1, offset=3)
    assert total == 4 and len(rows) == 1


# ── 按设备过滤的另外两个查询 ────────────────────────────────────────


def test_last_received_at_can_be_scoped_to_device(
    db: Database, ingestor_seed: None
) -> None:
    """``device_id`` 参数把"最后一条"限定到某台设备。

    /api/health 用的是全局版（不传 device_id）；按设备过滤是给
    "某台设备还活着吗"用的。多设备场景下两者结果不同，所以要分开测。
    """
    del ingestor_seed
    all_devices = _last_received(db)
    scoped = _last_received(db, DEVICE)

    assert all_devices is not None
    assert scoped is not None
    assert scoped <= all_devices


def test_find_receive_gaps_can_be_scoped(db: Database, ingestor_seed: None) -> None:
    """断层检测支持按设备、按起始时间过滤。

    实测场景：后端停了一晚，第二天启动时数据里出现一个 17 小时的断层。
    按设备过滤是为了多设备时不被别的设备的正常间隔干扰；
    since_ms 是为了只看最近一段时间，不让历史断层一直占着列表。
    """
    del ingestor_seed

    gaps_all = _gaps(db, min_gap_ms=1)
    gaps_scoped = _gaps(db, device_id=DEVICE, min_gap_ms=1)
    gaps_since = _gaps(db, min_gap_ms=1, since_ms=2**62)

    assert len(gaps_scoped) <= len(gaps_all)
    # since_ms 设成一个未来时刻 → 没有起点晚于它的行 → 没有断层
    assert gaps_since == []

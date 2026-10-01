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
from app.db.models import Anomaly, Prediction, Telemetry
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


# ── 降采样：query_telemetry_bucketed ────────────────────────────────
#
# ★ 这个函数存在的理由（值得写在测试里，因为"为什么要降采样"很容易忘）：
#
#   设备 2 秒一条，而 query_telemetry 的分页上限是 MAX_LIMIT = 1000
#   —— 只能覆盖 **33 分钟**。实测过：半小时窗口内温度跨度 0.0℃
#   （室内环境，DHT11 分辨率 1℃ 确实没变），而 24 小时跨度 2.0℃。
#   **要看温度变化就必须拉长时间窗口，而拉长必然超过 1000 条。**

BUCKET_MS = 10_000


def _seed_bucketed(db: Database) -> None:
    """塞入**温度递增**的记录，让每个桶的平均值可预测。

    ★ 不能用 ``make_telemetry``：它的温度来自 REAL_PAYLOAD 的固定值
      （32 / 73），每个桶的 avg 都一样，就验证不出聚合对不对。
      所以这里直接构造 ``Telemetry`` 行。

    时间布局（``bucket_ms = 10 秒``，BASE_TS 恰好是 10 秒的整数倍）：

      桶 0  [BASE_TS,        BASE_TS+10_000)  3 条  温度 30/31/32
      桶 1  [BASE_TS+10_000, BASE_TS+20_000)  2 条  温度 34/36

    另外各塞一条**别的设备**和一条 **boot 行**，用来验证过滤。
    """
    _ensure_devices(db, DEVICE, OTHER)

    def row(*, ts: int, temp: float, hum: float, rssi: int | None, seq: int,
            device_id: str = DEVICE, clock: str = "unix") -> Telemetry:
        return Telemetry(
            device_id=device_id,
            ts=BASE_TS + ts,
            received_at=BASE_TS + ts,
            temperature=temp,
            humidity=hum,
            rssi=rssi,
            seq=seq,
            clock=clock,
        )

    with db.session() as s:
        s.add_all(
            [
                row(ts=0, temp=30.0, hum=60.0, rssi=-40, seq=1),
                row(ts=2_000, temp=31.0, hum=62.0, rssi=-42, seq=2),
                row(ts=4_000, temp=32.0, hum=64.0, rssi=-44, seq=3),
                row(ts=10_000, temp=34.0, hum=70.0, rssi=-50, seq=4),
                row(ts=12_000, temp=36.0, hum=72.0, rssi=-52, seq=5),
                # 别的设备：温度 99，如果过滤失效会被平均进来
                row(ts=0, temp=99.0, hum=99.0, rssi=-99, seq=1, device_id=OTHER),
                # boot 行：温度 88，默认应被排除
                row(ts=0, temp=88.0, hum=88.0, rssi=-88, seq=6, clock="boot"),
            ]
        )


def test_bucketed_groups_by_time_window(db: Database) -> None:
    """同一桶内的多条聚合成一条，且 ts 是**桶起点**。"""
    _seed_bucketed(db)
    with db.session() as s:
        total, rows = repo.query_telemetry_bucketed(
            s, device_id=DEVICE, bucket_ms=BUCKET_MS
        )

    assert total == 2 and len(rows) == 2

    b0, b1 = rows
    # ★ ts 必须是 bucket_ms 的整数倍（桶起点），不是桶内某条记录的时刻
    assert b0["ts"] % BUCKET_MS == 0
    assert b1["ts"] % BUCKET_MS == 0
    assert b1["ts"] - b0["ts"] == BUCKET_MS
    assert b0["samples"] == 3
    assert b1["samples"] == 2


def test_bucketed_averages_values(db: Database) -> None:
    """桶内的值是 AVG —— 不是 MIN、MAX，也不是"随便挑一条"。"""
    _seed_bucketed(db)
    with db.session() as s:
        _, rows = repo.query_telemetry_bucketed(
            s, device_id=DEVICE, bucket_ms=BUCKET_MS
        )

    b0, b1 = rows
    assert b0["temperature"] == pytest.approx((30 + 31 + 32) / 3)
    assert b0["humidity"] == pytest.approx((60 + 62 + 64) / 3)
    assert b0["rssi"] == pytest.approx((-40 - 42 - 44) / 3)
    assert b1["temperature"] == pytest.approx((34 + 36) / 2)


def test_bucketed_excludes_boot_rows_by_default(db: Database) -> None:
    """boot 行默认不参与聚合。

    boot 行的 ts 是开机毫秒（只有几千），混进时间范围查询会污染结果 ——
    它会被塞进最左边那个桶里，把平均值拉得莫名其妙。
    """
    _seed_bucketed(db)
    with db.session() as s:
        _, without = repo.query_telemetry_bucketed(
            s, device_id=DEVICE, bucket_ms=BUCKET_MS
        )
        _, with_boot = repo.query_telemetry_bucketed(
            s, device_id=DEVICE, bucket_ms=BUCKET_MS, include_boot=True
        )

    assert without[0]["samples"] == 3
    assert with_boot[0]["samples"] == 4, "include_boot=True 时 boot 行应被算进去"
    # 88 混进来之后平均值被拉高 —— 这正是默认要排除它的原因
    assert with_boot[0]["temperature"] > without[0]["temperature"]


def test_bucketed_respects_device_filter(db: Database) -> None:
    """device_id 过滤生效（否则别的设备的 99℃ 会混进同一个桶）。"""
    _seed_bucketed(db)
    with db.session() as s:
        _, scoped = repo.query_telemetry_bucketed(
            s, device_id=DEVICE, bucket_ms=BUCKET_MS
        )
        _, merged = repo.query_telemetry_bucketed(s, bucket_ms=BUCKET_MS)

    assert all(r["temperature"] < 90 for r in scoped)
    assert merged[0]["samples"] > scoped[0]["samples"]


def test_bucketed_without_device_filter_merges_devices(db: Database) -> None:
    """★ 不传 device_id 时，多台设备在同一个时间桶里会被**平均到一起**。

    这是刻意的：不传设备就是"看整体"，合并平均正是"整体"的含义。

    但它和 ``query_telemetry`` 的行为**不同** —— 那个是各设备各返回
    一行，不做合并。所以在这里显式锁住，免得以后有人以为是 bug 改掉。
    """
    _seed_bucketed(db)
    with db.session() as s:
        _, rows = repo.query_telemetry_bucketed(s, bucket_ms=BUCKET_MS)

    # 两台设备各自在桶 0 有记录 → 合并成**一个**桶，样本数是两者之和
    assert rows[0]["samples"] == 4  # DEVICE 3 条 + OTHER 1 条


def test_bucketed_respects_time_range(db: Database) -> None:
    """start / end 过滤生效。"""
    _seed_bucketed(db)
    with db.session() as s:
        total, rows = repo.query_telemetry_bucketed(
            s, device_id=DEVICE, bucket_ms=BUCKET_MS, start_ms=BASE_TS + 10_000
        )

    assert total == 1
    assert rows[0]["samples"] == 2


def test_bucketed_empty_range_returns_zero(db: Database) -> None:
    """空区间返回 ``(0, [])``，不是报错（和 query_telemetry 一致）。"""
    _seed_bucketed(db)
    with db.session() as s:
        total, rows = repo.query_telemetry_bucketed(
            s, device_id=DEVICE, bucket_ms=BUCKET_MS, start_ms=2**62
        )

    assert total == 0
    assert rows == []


def test_bucketed_rssi_all_null_stays_null(db: Database) -> None:
    """整桶 rssi 都是 NULL 时结果仍是 NULL，**不是 0**。

    ★ 填 0 会被前端画成"信号极强"（0 dBm 是合法且极强的读数），
      那是把"没有数据"伪装成"数据很好" —— 最坏的一类图表谎言。
    """
    _ensure_devices(db, DEVICE)
    with db.session() as s:
        s.add(
            Telemetry(
                device_id=DEVICE,
                ts=BASE_TS,
                received_at=BASE_TS,
                temperature=30.0,
                humidity=60.0,
                rssi=None,
                seq=1,
                clock="unix",
            )
        )
    with db.session() as s:
        _, rows = repo.query_telemetry_bucketed(
            s, device_id=DEVICE, bucket_ms=BUCKET_MS
        )

    assert rows[0]["rssi"] is None


def test_larger_bucket_yields_fewer_points(db: Database) -> None:
    """桶宽越大、桶数越少 —— 这就是降采样的本质。"""
    _seed_bucketed(db)
    with db.session() as s:
        _, narrow = repo.query_telemetry_bucketed(
            s, device_id=DEVICE, bucket_ms=2_000
        )
        _, wide = repo.query_telemetry_bucketed(
            s, device_id=DEVICE, bucket_ms=60_000
        )

    assert len(wide) < len(narrow)
    assert len(wide) == 1, "60 秒的桶能装下这 12 秒里的全部记录"

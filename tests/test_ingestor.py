"""入库逻辑的单元测试。

★ 这些测试**不依赖 broker、不依赖真实设备**。

  因为 TelemetryIngestor.handle_batch() 只接收"一批消息 + 一个 Session"，
  不认识 paho、不认识网络。所以可以直接构造 InboundMessage 喂给它。

  这正是把"传输"和"业务"分开的最大收益：设备不稳的时候后端照样能验证。
  （实测踩过：调后端时设备正好静默停发，如果没有这层隔离，
   根本分不清是后端的问题还是设备的问题。）
"""

from __future__ import annotations

import pytest

from app.clock import now_ms
from app.db import repository as repo
from app.db.database import Database
from app.mqtt.consumer import InboundMessage
from app.mqtt.schemas import (
    StatusMessage,
    TelemetryMessage,
)
from app.services.telemetry_service import TelemetryIngestor

DEVICE = "esp32s3_001"

# 真实抓包原文（.tmp/mqtt_capture.txt）——用真实报文而不是编造的，
# 这样固件改了字段格式测试会立刻失败。
REAL_PAYLOAD = {
    "device_id": "esp32s3_001",
    "ts": 1790576501292,
    "seq": 258,
    "temperature_c": 32,
    "humidity_rh": 73,
    "wifi": True,
    "rssi": -23,
    "clock": "unix",
}


def make_telemetry(
    seq: int,
    *,
    ts: int | None = None,
    clock: str = "unix",
    received_ms: int | None = None,
    rssi: int | None = -23,
    device_id: str = DEVICE,
) -> InboundMessage:
    """构造一条已校验的遥测消息。"""
    now = now_ms()
    base = ts if ts is not None else (now if clock == "unix" else 5000)
    payload = {
        **REAL_PAYLOAD,
        "device_id": device_id,
        "ts": base,
        "seq": seq,
        "clock": clock,
        "rssi": rssi,
    }
    msg = TelemetryMessage.model_validate(payload)
    return InboundMessage(
        topic=f"iot/{device_id}/telemetry",
        device_id=device_id,
        kind="telemetry",
        received_ms=received_ms if received_ms is not None else now,
        telemetry=msg,
    )


def make_status(state: str, *, device_id: str = DEVICE) -> InboundMessage:
    return InboundMessage(
        topic=f"iot/{device_id}/status",
        device_id=device_id,
        kind="status",
        received_ms=now_ms(),
        status=StatusMessage(state=state),
    )


@pytest.fixture
def ingestor(settings, db: Database) -> TelemetryIngestor:
    return TelemetryIngestor(db, settings)


# ── 基础：报文真的落库了 ────────────────────────────────────────────


def test_real_payload_is_persisted(ingestor, db):
    """真实抓包原文能入库，且字段一一对应。"""
    with db.session() as s:
        result = ingestor.handle_batch(s, [make_telemetry(1)])

    assert result["inserted"] == 1
    assert result["duplicates"] == 0

    with db.session() as s:
        row = repo.get_latest_telemetry(s, DEVICE)
    assert row is not None
    assert row.temperature == 32.0
    assert row.humidity == 73.0
    assert row.rssi == -23
    assert row.seq == 1
    assert row.clock == "unix"


def test_device_is_auto_registered(ingestor, db):
    """第一次见到某设备时自动建设备行——否则 /api/devices 永远空。"""
    with db.session() as s:
        assert repo.get_device(s, DEVICE) is None
        ingestor.handle_batch(s, [make_telemetry(1)])
        device = repo.get_device(s, DEVICE)
    assert device is not None
    assert device.device_id == DEVICE


# ── ★ 两条时间轴 ────────────────────────────────────────────────────


def test_received_at_is_backend_clock(ingestor, db):
    """★ received_at 必须是**后端**时钟，不是设备的 ts。

    这是"发现设备静默停发"的唯一手段。构造一条 ts 很旧的报文
    （模拟设备时钟不准或数据迟到），received_at 仍应是"现在"。
    """
    old_ts = 1_600_000_000_000  # 2020 年
    before = now_ms()
    with db.session() as s:
        ingestor.handle_batch(s, [make_telemetry(1, ts=old_ts)])
    after = now_ms()

    with db.session() as s:
        row = repo.get_latest_telemetry(s, DEVICE)

    assert row.ts == old_ts, "ts 应保持设备报的值不变"
    assert before <= row.received_at <= after, "received_at 应是后端当前时间"


def test_received_at_preserved_from_consumer(ingestor, db):
    """received_at 用 consumer 收到那一刻的时间，不是写库线程处理的时间。

    为什么要这样：写库线程可能有队列积压（几百毫秒到几秒）。
    如果 received_at 取写库时刻，"入库间隔"就会被队列抖动污染，
    用它做断层检测会误报。取收到时刻才是链路真实的样子。
    """
    stamp = now_ms() - 60_000  # 一分钟前收到的
    with db.session() as s:
        ingestor.handle_batch(s, [make_telemetry(1, received_ms=stamp)])

    with db.session() as s:
        row = repo.get_latest_telemetry(s, DEVICE)
    assert row.received_at == stamp


def test_gap_detection_finds_silent_stall(ingestor, db):
    """★ 断层检测：造一段"设备静默停发"，必须能查出来。

    这是 received_at 这一列存在的核心理由。
    """
    base = now_ms() - 3_600_000
    with db.session() as s:
        # 前 3 条正常，然后停 30 分钟，再来 3 条
        ingestor.handle_batch(
            s, [make_telemetry(i, ts=base + i * 2000, received_ms=base + i * 2000)
                for i in range(1, 4)]
        )
        ingestor.handle_batch(
            s,
            [
                make_telemetry(
                    i,
                    ts=base + 1800_000 + i * 2000,
                    received_ms=base + 1800_000 + i * 2000,
                )
                for i in range(4, 7)
            ],
        )

    with db.session() as s:
        gaps = repo.find_receive_gaps(s, min_gap_ms=60_000)

    assert len(gaps) == 1
    gap = gaps[0]
    assert gap["device_id"] == DEVICE
    # 1800 秒 = 30 分钟
    assert gap["gap_ms"] == pytest.approx(1800_000, abs=10_000)


# ── QoS1 重投去重 ───────────────────────────────────────────────────


def test_duplicate_messages_are_deduplicated(ingestor, db):
    """QoS1 是"至少一次"，重投必须被幂等处理。"""
    batch = [make_telemetry(i) for i in range(1, 4)]

    with db.session() as s:
        first = ingestor.handle_batch(s, batch)
    with db.session() as s:
        second = ingestor.handle_batch(s, batch)  # 同一批再喂一次

    assert first["inserted"] == 3
    assert second["inserted"] == 0
    assert second["duplicates"] == 3

    with db.session() as s:
        assert repo.count_telemetry(s) == 3
    assert ingestor.stats.duplicates_skipped == 3


# ── 可空字段 ────────────────────────────────────────────────────────


def test_null_rssi_is_stored_as_null(ingestor, db):
    """未联网时 rssi 是 JSON null，必须存成 SQL NULL。

    ★ 不能存 0——0 dBm 是合法且极强的读数，用它表示"读不到"
      会把真实数据污染成"没有数据"。
    """
    with db.session() as s:
        ingestor.handle_batch(s, [make_telemetry(1, rssi=None)])

    with db.session() as s:
        row = repo.get_latest_telemetry(s, DEVICE)
    assert row.rssi is None


# ── boot 行 ─────────────────────────────────────────────────────────


def test_boot_rows_persisted_by_default(ingestor, db):
    """默认**入库** boot 行。

    为什么不丢：如果设备侧 NTP 没配（受支持的用法），所有报文都是 boot，
    丢弃会让数据库一直是空的，而系统看起来一切正常——最难查的故障。
    """
    with db.session() as s:
        ingestor.handle_batch(s, [make_telemetry(1, clock="boot", ts=5000)])

    with db.session() as s:
        assert repo.count_telemetry(s) == 1
    assert ingestor.stats.boot_rows_persisted == 1


def test_boot_rows_excluded_from_default_queries(ingestor, db):
    """boot 行入库了，但默认查询查不到（ts 不是绝对时间）。"""
    with db.session() as s:
        ingestor.handle_batch(
            s, [make_telemetry(1), make_telemetry(2, clock="boot", ts=5000)]
        )

    with db.session() as s:
        total_default, _ = repo.query_telemetry(s, limit=100)
        total_all, _ = repo.query_telemetry(s, limit=100, include_boot=True)
        latest = repo.get_latest_telemetry(s, DEVICE)

    assert total_default == 1
    assert total_all == 2
    assert latest.clock == "unix", "latest 不能返回 boot 行"


def test_boot_rows_can_be_dropped_by_config(settings, db):
    """显式配置成丢弃时才丢，且要计数。"""
    strict = settings.model_copy(update={"persist_boot_rows": False})
    ingestor = TelemetryIngestor(db, strict)

    with db.session() as s:
        ingestor.handle_batch(s, [make_telemetry(1, clock="boot", ts=5000)])
        assert repo.count_telemetry(s) == 0
    assert ingestor.stats.boot_rows_dropped == 1


def test_device_registered_even_when_telemetry_dropped(settings, db):
    """★ 遥测被丢弃时，设备仍然要注册。

    丢遥测 ≠ 丢存活信号。否则一台 NTP 没配的设备（所有报文都是 boot
    且被丢）会在 /api/devices 里"从未出现过"，而它其实一直在稳定上报——
    那会让人以为是设备的问题，实际是后端的统计口径错了。
    """
    strict = settings.model_copy(update={"persist_boot_rows": False})
    ingestor = TelemetryIngestor(db, strict)

    with db.session() as s:
        ingestor.handle_batch(s, [make_telemetry(1, clock="boot", ts=5000)])
        assert repo.get_device(s, DEVICE) is not None, "设备必须被注册"


# ── 在线状态 ────────────────────────────────────────────────────────


def test_status_message_updates_device(ingestor, db):
    with db.session() as s:
        ingestor.handle_batch(s, [make_status("online")])
        device = repo.get_device(s, DEVICE)
        assert device.status == "online"
        assert repo.is_online(device, now_ms=now_ms()) is True

    with db.session() as s:
        ingestor.handle_batch(s, [make_status("offline")])
        device = repo.get_device(s, DEVICE)
        assert device.status == "offline"
        assert repo.is_online(device, now_ms=now_ms()) is False


def test_telemetry_does_not_reset_status(ingestor, db):
    """★ 一条遥测不该把设备刚报的 offline 冲掉。

    如果 upsert_device 在 status=None 时写成 'unknown'，就会出现
    "设备明明离线了，一条迟到的遥测又把它标成未知"。
    """
    with db.session() as s:
        ingestor.handle_batch(s, [make_status("offline")])
    with db.session() as s:
        ingestor.handle_batch(s, [make_telemetry(1)])
    with db.session() as s:
        device = repo.get_device(s, DEVICE)
    assert device.status == "offline"


def test_last_seen_uses_latest_in_batch(ingestor, db):
    """同一批里同一设备多条时，last_seen 取**最后**一条的时间。

    取第一条的话，一批数据入库后 last_seen 会倒退，
    在线判定就会莫名其妙地变 False。
    """
    t0 = now_ms() - 10_000
    with db.session() as s:
        ingestor.handle_batch(
            s,
            [
                make_telemetry(1, received_ms=t0),
                make_telemetry(2, received_ms=t0 + 2000),
                make_telemetry(3, received_ms=t0 + 4000),
            ],
        )
    with db.session() as s:
        device = repo.get_device(s, DEVICE)
    assert device.last_seen == t0 + 4000


# ── seq 跳号 ────────────────────────────────────────────────────────


def test_seq_gap_is_detected(ingestor, db):
    with db.session() as s:
        ingestor.handle_batch(s, [make_telemetry(1), make_telemetry(2)])
    with db.session() as s:
        ingestor.handle_batch(s, [make_telemetry(5)])  # 3、4 丢了

    assert ingestor.stats.seq_gap_events == 1
    assert ingestor.stats.seq_missing_messages == 2


def test_seq_restart_is_not_reported_as_gap(ingestor, db):
    """★ 设备重启（seq 回到 1）不能报成"丢了 5000 条"。

    只看"比上次小就是异常"的话，每次设备重启都会报一个巨大缺口，
    把真正的丢包淹没掉。
    """
    with db.session() as s:
        ingestor.handle_batch(s, [make_telemetry(5000)])
    with db.session() as s:
        ingestor.handle_batch(s, [make_telemetry(1)])  # 设备重启

    assert ingestor.stats.seq_gap_events == 0
    assert ingestor.stats.seq_missing_messages == 0


# ── 多设备 ──────────────────────────────────────────────────────────


def test_multiple_devices_are_isolated(ingestor, db):
    with db.session() as s:
        ingestor.handle_batch(
            s,
            [
                make_telemetry(1, device_id="dev_a"),
                make_telemetry(1, device_id="dev_b"),
                make_telemetry(2, device_id="dev_a"),
            ],
        )

    with db.session() as s:
        assert repo.count_devices(s) == 2
        total_a, _ = repo.query_telemetry(s, device_id="dev_a", limit=10)
        total_b, _ = repo.query_telemetry(s, device_id="dev_b", limit=10)
    assert total_a == 2
    assert total_b == 1
    # 两台设备各自的 seq 独立跟踪，不该互相干扰
    assert ingestor.stats.seq_gap_events == 0


# ── 队列 ────────────────────────────────────────────────────────────


def test_queue_full_drops_without_blocking(ingestor, db, settings):
    """★ 队列满时**丢数据但不阻塞**——回调在 paho 网络线程上，阻塞它
    会导致 broker 判定本端已死。"""
    ingestor._queue = __import__("queue").Queue(maxsize=2)  # noqa: SLF001

    for i in range(5):
        ingestor.submit(make_telemetry(i + 1))

    assert ingestor.stats.queue_dropped == 3
    assert ingestor.queue_depth == 2

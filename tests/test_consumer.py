"""MQTT 消费者的测试。

★ 这些测试**不连 broker、不起网络线程**。

  做法是把 paho 的回调当普通方法直接调（``consumer._on_connect(...)``），
  客户端传一个假对象。这样能精确构造那些"真跑起来很难复现"的场景：

    - broker 拒绝连接（密码错 / client_id 被拒）
    - **重连后必须重新订阅**（漏了就是"首次能收、重连后再也收不到"）
    - **回调里抛异常必须被吞掉**（否则 paho 网络线程退出，
      表现为连接正常但再也收不到数据，日志里一条错误都没有）

  这三条都是"静默无数据"类故障，恰恰是最难在真机上复现的——
  它们不报错、连接看着正常，只是数据不见了。

★ 为什么假客户端而不是 ``unittest.mock.MagicMock``：

  ``subscribe()`` 的返回值被解包成 ``result, mid``，用 MagicMock 的话
  解包出来是两个 MagicMock，``result == mqtt.MQTT_ERR_SUCCESS`` 为 False，
  于是代码走进"订阅失败"分支——测试通过但测的是错的分支。
  写一个显式的小假类，返回什么一目了然。
"""

from __future__ import annotations

import json
import time
from typing import Any

import paho.mqtt.client as mqtt
import pytest
from paho.mqtt.client import convert_connack_rc_to_reason_code

from app.config import Settings
from app.mqtt.consumer import InboundMessage, MqttConsumer
from app.mqtt.schemas import (
    MAX_BOOT_MS,
    MIN_UNIX_MS,
    TOPIC_KIND_STATUS,
    TOPIC_KIND_TELEMETRY,
    TelemetryMessage,
    parse_topic,
)
from app.stats import IngestStats

DEVICE = "esp32s3_001"

# 一条真实抓包原文（.tmp/mqtt_capture.txt）——用真实报文而不是编造的，
# 固件改了字段格式时这里会立刻失败。
REAL_PAYLOAD = {
    "device_id": DEVICE,
    "ts": 1790576501292,
    "seq": 258,
    "temperature_c": 32,
    "humidity_rh": 73,
    "wifi": True,
    "rssi": -23,
    "clock": "unix",
}


# ── 假对象 ──────────────────────────────────────────────────────────


class FakeClient:
    """记录调用、返回可控结果的 paho 客户端替身。"""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.subscribed: list[tuple[str, int]] = []
        self.username: tuple[str, str | None] | None = None
        self.reconnect_delay: dict[str, Any] | None = None
        self.connected_to: tuple[str, int, int] | None = None
        self.loop_running = False
        self.disconnected = False
        # 让测试能模拟"订阅失败"
        self.subscribe_rc = mqtt.MQTT_ERR_SUCCESS

    def username_pw_set(self, username: str, password: str | None = None) -> None:
        self.username = (username, password)

    def reconnect_delay_set(self, **kwargs: Any) -> None:
        self.reconnect_delay = kwargs

    def connect_async(self, host: str, port: int, keepalive: int) -> None:
        self.connected_to = (host, port, keepalive)

    def loop_start(self) -> None:
        self.loop_running = True

    def loop_stop(self) -> None:
        self.loop_running = False

    def disconnect(self) -> None:
        self.disconnected = True

    def subscribe(self, topic: str, qos: int = 0) -> tuple[int, int]:
        self.subscribed.append((topic, qos))
        return self.subscribe_rc, len(self.subscribed)


class FakeMessage:
    """paho 的 MQTTMessage 替身。代码只用到 topic 和 payload。"""

    def __init__(self, topic: str, payload: bytes) -> None:
        self.topic = topic
        self.payload = payload


def _ok_reason() -> Any:
    """连接成功的 CONNACK 返回码。

    走 paho 自己的转换函数，而不是手搓 ReasonCode——
    这样"paho 改了内部映射"时测试会跟着一起变，而不是测一个假前提。
    """
    return convert_connack_rc_to_reason_code(0)


def _fail_reason() -> Any:
    """连接失败（MQTT 3.1.1 的 4 = 用户名或密码错）。"""
    return convert_connack_rc_to_reason_code(4)


# ── 夹具 ────────────────────────────────────────────────────────────


@pytest.fixture
def collected() -> list[InboundMessage]:
    return []


@pytest.fixture
def stats() -> IngestStats:
    return IngestStats()


@pytest.fixture
def consumer(
    settings: Settings, collected: list[InboundMessage], stats: IngestStats
) -> MqttConsumer:
    """sink 只把消息收进列表，不写库——测的是消费者，不是入库。"""
    return MqttConsumer(settings, collected.append, stats=stats)


def _topic(kind: str, device_id: str = DEVICE) -> str:
    return f"iot/{device_id}/{kind}"


def _feed(consumer: MqttConsumer, kind: str, payload: Any, *, device_id: str = DEVICE) -> None:
    """把一条报文喂给消费者（走 _handle_message，和真实路径一致）。"""
    body = payload if isinstance(payload, str) else json.dumps(payload)
    consumer._handle_message(_topic(kind, device_id), body.encode())


# ── parse_topic ─────────────────────────────────────────────────────


def test_parse_topic_extracts_device_and_kind() -> None:
    parsed = parse_topic("iot/esp32s3_001/telemetry")
    assert parsed is not None
    assert parsed.device_id == "esp32s3_001"
    assert parsed.kind == TOPIC_KIND_TELEMETRY

    parsed = parse_topic("iot/esp32s3_001/status")
    assert parsed is not None and parsed.kind == TOPIC_KIND_STATUS


@pytest.mark.parametrize(
    "topic",
    [
        "iot/esp32s3_001/cmd",          # 将来会有，但现在不认
        "iot/esp32s3_001",              # 少一层
        "iot/esp32s3_001/telemetry/x",  # 多一层
        "iot//telemetry",               # device_id 为空
        "IOT/esp32s3_001/telemetry",    # 大小写敏感
        "other/esp32s3_001/telemetry",
        "",
        "$SYS/broker/uptime",           # broker 自己的 topic
        # ★ device_id 的字符集校验（2026-09-29 补的缺口）：
        #   topic 正则里的 [^/]+ 只排除斜杠，所以这些能通过第一层匹配，
        #   必须在第二层被拦下。
        "iot/esp32;drop/telemetry",
        "iot/esp32 s3/telemetry",       # 空格
        "iot/设备001/telemetry",         # 非 ASCII
        "iot/../etc/telemetry",         # 路径穿越
        "iot/esp32%2F001/telemetry",    # URL 编码的斜杠
    ],
)
def test_parse_topic_rejects_non_matching(topic: str) -> None:
    """不匹配一律返回 None（**这是正常情况，不是错误**）。

    将来加 iot/<id>/cmd 时后端会收到自己不认识的 topic，
    调用方应该计数后忽略，而不是报错——所以这里断言的是
    "返回 None"而不是"抛异常"。
    """
    assert parse_topic(topic) is None


# ── _on_connect ─────────────────────────────────────────────────────


def test_on_connect_subscribes_to_all_configured_topics(
    consumer: MqttConsumer, settings: Settings
) -> None:
    client = FakeClient()
    consumer._on_connect(client, None, None, _ok_reason())

    assert consumer.connected is True
    assert [t for t, _ in client.subscribed] == settings.subscriptions
    assert all(qos == settings.mqtt_qos for _, qos in client.subscribed)


def test_on_connect_resubscribes_on_reconnect(
    consumer: MqttConsumer, settings: Settings
) -> None:
    """★ 本文件最重要的用例之一。

    clean_session=True 时 broker 在断开时会丢掉本端**全部**订阅，
    重连后不会自动恢复。所以 ``_on_connect`` 里必须重新订阅。

    漏了的症状是"首次能收到数据，重连后再也收不到"——
    而连接状态显示完全正常，日志里也没有错误。这是最难查的一类故障。

    断言方式是"连两次 → 订阅请求翻倍"，而不是只看"订阅过"。
    """
    client = FakeClient()
    consumer._on_connect(client, None, None, _ok_reason())
    first_round = len(client.subscribed)

    consumer._on_disconnect(client, None, None, _ok_reason())
    consumer._on_connect(client, None, None, _ok_reason())

    assert first_round == len(settings.subscriptions)
    assert len(client.subscribed) == 2 * len(settings.subscriptions)


def test_on_connect_failure_does_not_subscribe_or_mark_connected(
    consumer: MqttConsumer,
) -> None:
    """broker 拒绝连接时不能订阅，也不能把自己标成已连接。

    如果这里误标了 connected=True，/api/health 会显示"已连接"而实际
    收不到任何数据——排查时会把注意力引向错误的方向。
    """
    client = FakeClient()
    consumer._on_connect(client, None, None, _fail_reason())

    assert consumer.connected is False
    assert client.subscribed == []
    assert consumer.snapshot()["connects"] == 0


def test_on_connect_logs_subscribe_failure(
    consumer: MqttConsumer, caplog: pytest.LogCaptureFixture
) -> None:
    """订阅失败要打 ERROR，但仍然算连接成功。

    为什么仍然算成功：TCP/CONNACK 层面确实连上了，只是订阅没成。
    把它判成"未连接"会让 paho 反复重连，而重连解决不了"订阅被拒"
    （通常是 ACL 配置问题）。
    """
    client = FakeClient()
    client.subscribe_rc = mqtt.MQTT_ERR_NO_CONN

    with caplog.at_level("ERROR"):
        consumer._on_connect(client, None, None, _ok_reason())

    assert consumer.connected is True
    assert any("订阅失败" in r.message for r in caplog.records)


def test_on_connect_resets_disconnect_counter(
    consumer: MqttConsumer,
) -> None:
    """重连成功后断线计数归零。

    不归零的话，一次长时间断网之后计数会一直很大，
    后面的断线日志全被限速掉——而"刚恢复又断了"恰恰是最该看到的。
    """
    client = FakeClient()
    consumer._on_disconnect(client, None, None, _ok_reason())
    consumer._on_disconnect(client, None, None, _ok_reason())
    assert consumer.snapshot()["disconnects"] == 2

    consumer._on_connect(client, None, None, _ok_reason())
    assert consumer.snapshot()["disconnects"] == 0


# ── _on_disconnect ──────────────────────────────────────────────────


def test_on_disconnect_clears_connected(consumer: MqttConsumer) -> None:
    client = FakeClient()
    consumer._on_connect(client, None, None, _ok_reason())
    assert consumer.connected is True

    consumer._on_disconnect(client, None, None, _ok_reason())

    assert consumer.connected is False
    assert consumer.snapshot()["disconnects"] == 1


# ── _on_message：异常必须被吞掉 ─────────────────────────────────────


def test_on_message_swallows_sink_exception(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """★ 本文件最重要的用例。

    回调由 paho 的网络线程直接调用。未捕获的异常会让**那个线程退出**，
    而 paho 不会重启它——结果连接看起来还在（socket 没关），
    但再也没有消息被处理，日志里一条错误都没有。

    这是最典型的"静默无数据"故障，所以宁可吞掉异常也要保住线程。

    这里让 sink 抛异常，断言 ``_on_message`` 不向外抛。
    """

    def exploding_sink(item: InboundMessage) -> None:
        raise RuntimeError("模拟入库层炸了")

    consumer = MqttConsumer(settings, exploding_sink, stats=IngestStats())
    msg = FakeMessage(_topic(TOPIC_KIND_TELEMETRY), json.dumps(REAL_PAYLOAD).encode())

    with caplog.at_level("ERROR"):
        consumer._on_message(None, None, msg)  # 不该抛

    assert any("未捕获异常" in r.message for r in caplog.records)


# ── _handle_message：遥测 ───────────────────────────────────────────


def test_telemetry_happy_path(
    consumer: MqttConsumer, collected: list[InboundMessage]
) -> None:
    before = int(time.time() * 1000)
    _feed(consumer, TOPIC_KIND_TELEMETRY, REAL_PAYLOAD)
    after = int(time.time() * 1000)

    assert len(collected) == 1
    item = collected[0]
    assert item.device_id == DEVICE
    assert item.kind == TOPIC_KIND_TELEMETRY
    assert item.telemetry is not None and item.telemetry.seq == 258
    assert item.status is None
    # ★ received_ms 必须是**后端时钟**，不是报文里的 ts。
    #   这是"两条时间轴"的物理落点：ts 是设备采样时刻（可能失真），
    #   received_ms 是后端收到时刻（永远可靠），只有它能发现静默停发。
    assert before <= item.received_ms <= after
    assert item.received_ms != item.telemetry.ts


def test_telemetry_rssi_null_is_preserved(
    consumer: MqttConsumer, collected: list[InboundMessage]
) -> None:
    """rssi=null 必须原样传下去，**不能变成 0**。

    0 dBm 是合法且极强的信号读数（贴着路由器才有）。用 0 表示"未知"
    会把真实数据污染成"没有数据"，而且事后无法区分。
    """
    _feed(consumer, TOPIC_KIND_TELEMETRY, {**REAL_PAYLOAD, "rssi": None})

    assert len(collected) == 1
    assert collected[0].telemetry is not None
    assert collected[0].telemetry.rssi is None


def test_telemetry_unknown_field_is_ignored(
    consumer: MqttConsumer, collected: list[InboundMessage]
) -> None:
    """固件加字段时后端不该丢整条数据（extra="ignore"）。

    这条是**前向兼容**的保证：固件升级加个 battery_v，后端不用同步改、
    不用协调部署顺序。代价是拼错字段名会被静默忽略——所以必填字段
    没有默认值，缺了直接报错。
    """
    _feed(consumer, TOPIC_KIND_TELEMETRY, {**REAL_PAYLOAD, "battery_v": 3.9})

    assert len(collected) == 1


def test_telemetry_device_id_mismatch_is_rejected(
    consumer: MqttConsumer, collected: list[InboundMessage], stats: IngestStats
) -> None:
    """topic 里的 device_id 与 payload 里的不一致 → 拒收。

    两者不一致说明是固件 bug 或有人在伪造。而且"该信哪个"本身说不清，
    所以直接不要，并单独计数（与"报文格式错"区分开——
    前者指向固件 bug，后者指向协议不匹配）。
    """
    _feed(consumer, TOPIC_KIND_TELEMETRY, REAL_PAYLOAD, device_id="other_device")

    assert collected == []
    assert stats.mismatched_device_id == 1
    assert stats.invalid_messages == 0


def test_telemetry_clock_contradiction_is_rejected(
    consumer: MqttConsumer, collected: list[InboundMessage], stats: IngestStats
) -> None:
    """clock 与 ts 量级矛盾 → 拒收。

    clock=unix 而 ts=5000（5 秒）如果照存，会变成 1970-01-01 那条记录，
    永久污染所有时序查询：按时间范围查会捞出来、算平均值会被拉偏，
    而它看起来完全合法（就是个很小的数）。
    """
    _feed(consumer, TOPIC_KIND_TELEMETRY, {**REAL_PAYLOAD, "clock": "unix", "ts": 5000})

    assert collected == []
    assert stats.invalid_messages == 1


def test_telemetry_missing_required_field_is_rejected(
    consumer: MqttConsumer, collected: list[InboundMessage], stats: IngestStats
) -> None:
    """必填字段缺失 → 拒收（而不是静默变成 0.0）。

    temperature_c / humidity_rh 故意没有默认值：有默认值的话，
    固件把字段名写错会变成"温度永远是 0 度"，而且一路存进库。
    """
    payload = {k: v for k, v in REAL_PAYLOAD.items() if k != "temperature_c"}
    _feed(consumer, TOPIC_KIND_TELEMETRY, payload)

    assert collected == []
    assert stats.invalid_messages == 1


def test_illegal_device_id_in_topic_is_rejected(
    consumer: MqttConsumer, collected: list[InboundMessage], stats: IngestStats
) -> None:
    """★ 写测试时发现的缺口：topic 里的 device_id 之前**没做字符集校验**。

    telemetry 那条路径靠"topic 与 payload 不一致"间接挡住了，
    但 **status 报文里没有 device_id 字段**，只能从 topic 取——
    于是 ``iot/esp32;drop/status`` 会被正常接收，
    非法 id 一路写进 ``devices`` 表，再出现在 ``/api/devices/{id}``
    的 URL 路径、日志和将来的前端路由里。

    修法是在 ``parse_topic`` 里也校验一次（见 schemas.py 的说明）。
    这里断言的是"两条路径都拦住了"。
    """
    # status 路径（真正有漏洞的那条）：payload 里没有 id 可校验
    _feed(consumer, TOPIC_KIND_STATUS, {"state": "online"}, device_id="esp32;drop")
    # telemetry 路径
    _feed(consumer, TOPIC_KIND_TELEMETRY, REAL_PAYLOAD, device_id="esp32;drop")

    assert collected == []
    assert stats.devices_seen == set()  # 非法 id 绝不能进 devices_seen
    assert stats.topics_ignored == 2
    assert stats.mismatched_device_id == 0  # 不是"不一致"，是"topic 不合法"


def test_telemetry_illegal_device_id_in_payload_is_rejected(
    consumer: MqttConsumer, collected: list[InboundMessage], stats: IngestStats
) -> None:
    """payload 侧的 device_id 由 pydantic 校验器拦（这一层一直是对的）。

    ★ 构造这条用例要注意顺序：``_handle_telemetry`` 里是
      **先 model_validate 再比一致性**，而 ``parse_topic`` 又会先拦掉
      非法的 topic id。所以要让 payload 校验器真正被执行，必须
      用**合法 topic + 非法 payload**。
    """
    _feed(
        consumer,
        TOPIC_KIND_TELEMETRY,
        {**REAL_PAYLOAD, "device_id": "esp32;drop"},
        device_id=DEVICE,
    )

    assert collected == []
    assert stats.invalid_messages == 1
    assert stats.topics_ignored == 0  # topic 本身是合法的


def test_telemetry_registers_device_in_stats(
    consumer: MqttConsumer, stats: IngestStats
) -> None:
    _feed(consumer, TOPIC_KIND_TELEMETRY, REAL_PAYLOAD)

    assert stats.devices_seen == {DEVICE}
    assert stats.messages_received == 1


# ── _handle_message：status ─────────────────────────────────────────


def test_status_happy_path(
    consumer: MqttConsumer, collected: list[InboundMessage]
) -> None:
    _feed(consumer, TOPIC_KIND_STATUS, {"state": "online"})

    assert len(collected) == 1
    item = collected[0]
    assert item.kind == TOPIC_KIND_STATUS
    assert item.status is not None and item.status.state == "online"
    assert item.telemetry is None


def test_status_offline_from_lwt_is_accepted(
    consumer: MqttConsumer, collected: list[InboundMessage]
) -> None:
    """broker 代发的 LWT（offline）走的是同一条路径。"""
    _feed(consumer, TOPIC_KIND_STATUS, {"state": "offline"})

    assert len(collected) == 1
    assert collected[0].status is not None
    assert collected[0].status.state == "offline"


def test_status_invalid_state_is_rejected(
    consumer: MqttConsumer, collected: list[InboundMessage], stats: IngestStats
) -> None:
    _feed(consumer, TOPIC_KIND_STATUS, {"state": "unknown"})

    assert collected == []
    assert stats.invalid_messages == 1


def test_status_uses_topic_device_id(
    consumer: MqttConsumer, collected: list[InboundMessage]
) -> None:
    """status 报文里**没有** device_id 字段，只能从 topic 取。

    与遥测不同：遥测两边都有、要求一致；status 只有 topic 一侧。
    所以 status 不做一致性检查（没有可比的对象）。
    """
    _feed(consumer, TOPIC_KIND_STATUS, {"state": "online"}, device_id="another_dev")

    assert len(collected) == 1
    assert collected[0].device_id == "another_dev"


# ── _handle_message：坏报文 ─────────────────────────────────────────


def test_unknown_topic_is_counted_not_errored(
    consumer: MqttConsumer, collected: list[InboundMessage], stats: IngestStats
) -> None:
    consumer._handle_message("iot/esp32s3_001/cmd", b'{"cmd":"reboot"}')

    assert collected == []
    assert stats.topics_ignored == 1
    assert stats.invalid_messages == 0  # 不是"坏报文"，只是我们不关心


def test_invalid_json_is_counted(
    consumer: MqttConsumer, collected: list[InboundMessage], stats: IngestStats
) -> None:
    _feed(consumer, TOPIC_KIND_TELEMETRY, "{not json")

    assert collected == []
    assert stats.invalid_messages == 1


def test_json_array_is_rejected(
    consumer: MqttConsumer, collected: list[InboundMessage], stats: IngestStats
) -> None:
    """JSON 合法但不是对象 → 拒收。

    契约是"一个 JSON 对象"。合法的 JSON 数组/数字/字符串都算坏报文，
    不能因为 json.loads 成功就往下走——``raw["device_id"]`` 在数组上
    会抛 TypeError，那会冒泡到 _on_message 的兜底里被吞掉，
    表现为"这条数据不见了但没有任何计数"。
    """
    _feed(consumer, TOPIC_KIND_TELEMETRY, "[1,2,3]")

    assert collected == []
    assert stats.invalid_messages == 1


def test_last_message_timestamp_is_recorded(
    consumer: MqttConsumer,
) -> None:
    assert consumer.snapshot()["last_message_ms"] == 0
    assert consumer.snapshot()["last_message_age_ms"] is None

    _feed(consumer, TOPIC_KIND_STATUS, {"state": "online"})

    snap = consumer.snapshot()
    assert snap["last_message_ms"] > 0
    assert isinstance(snap["last_message_age_ms"], int)


# ── 生命周期 ────────────────────────────────────────────────────────


def test_start_configures_client_and_is_idempotent(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``start()`` 只建一次客户端，且不阻塞。

    调两次只建一个客户端很重要：多建一个就是多一个 MQTT 连接，
    而它们用同一个 client_id，会互相踢下线。
    """
    created: list[FakeClient] = []

    def factory(**kwargs: Any) -> FakeClient:
        client = FakeClient(**kwargs)
        created.append(client)
        return client

    monkeypatch.setattr("app.mqtt.consumer.mqtt.Client", factory)

    consumer = MqttConsumer(settings, lambda item: None)
    consumer.start()
    consumer.start()

    assert len(created) == 1
    client = created[0]
    # paho 2.x 必须显式传 callback_api_version，不传直接 ValueError
    assert client.kwargs["callback_api_version"] is mqtt.CallbackAPIVersion.VERSION2
    assert client.kwargs["client_id"] == settings.mqtt_client_id
    assert client.connected_to == (
        settings.mqtt_host,
        settings.mqtt_port,
        settings.mqtt_keepalive,
    )
    assert client.loop_running is True
    assert client.reconnect_delay == {
        "min_delay": settings.mqtt_reconnect_min_s,
        "max_delay": settings.mqtt_reconnect_max_s,
    }


def test_start_sets_username_when_configured(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """配了用户名才调 username_pw_set；没配不调（匿名）。"""
    created: list[FakeClient] = []
    monkeypatch.setattr(
        "app.mqtt.consumer.mqtt.Client",
        lambda **kw: created.append(FakeClient(**kw)) or created[-1],
    )

    # 不配用户名
    MqttConsumer(settings, lambda i: None).start()
    assert created[0].username is None

    # 配用户名（空密码要传 None 而不是空串）
    authed = settings.model_copy(update={"mqtt_username": "u", "mqtt_password": ""})
    MqttConsumer(authed, lambda i: None).start()
    assert created[1].username == ("u", None)


def test_stop_before_start_is_noop(settings: Settings) -> None:
    """没启动过就 stop 不该抛异常（lifespan 里无条件调用 stop）。"""
    consumer = MqttConsumer(settings, lambda i: None)
    consumer.stop()  # 不该抛
    assert consumer.connected is False


def test_stop_disconnects_and_stops_loop(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    created: list[FakeClient] = []
    monkeypatch.setattr(
        "app.mqtt.consumer.mqtt.Client",
        lambda **kw: created.append(FakeClient(**kw)) or created[-1],
    )

    consumer = MqttConsumer(settings, lambda i: None)
    consumer.start()
    consumer._on_connect(created[0], None, None, _ok_reason())
    assert consumer.connected is True

    consumer.stop()

    assert created[0].disconnected is True
    assert created[0].loop_running is False
    assert consumer.connected is False


def test_stop_survives_disconnect_exception(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """disconnect() 抛异常时仍必须 loop_stop()。

    否则 paho 的网络线程会泄漏——进程里留着一个不再被管理的线程，
    反复重启（uvicorn --reload）时会累积。
    """

    class BadClient(FakeClient):
        def disconnect(self) -> None:
            raise OSError("socket 已经没了")

    created: list[BadClient] = []
    monkeypatch.setattr(
        "app.mqtt.consumer.mqtt.Client",
        lambda **kw: created.append(BadClient(**kw)) or created[-1],
    )

    consumer = MqttConsumer(settings, lambda i: None)
    consumer.start()
    consumer.stop()  # 不该抛

    assert created[0].loop_running is False


# ── snapshot ────────────────────────────────────────────────────────


def test_snapshot_shape(consumer: MqttConsumer, settings: Settings) -> None:
    """/api/health 直接把它塞进响应体，键名就是对外契约。"""
    snap = consumer.snapshot()

    assert set(snap) == {
        "enabled",
        "connected",
        "host",
        "port",
        "client_id",
        "subscriptions",
        "connects",
        "disconnects",
        "last_message_ms",
        "last_message_age_ms",
    }
    assert snap["enabled"] is True
    assert snap["host"] == settings.mqtt_host
    assert snap["subscriptions"] == settings.subscriptions
    # 不含密码之类的敏感项
    assert "password" not in json.dumps(snap)


# ── resolved_clock 的边界 ───────────────────────────────────────────


def _msg(**over: Any) -> TelemetryMessage:
    return TelemetryMessage.model_validate({**REAL_PAYLOAD, **over})


def test_resolved_clock_accepts_consistent_values() -> None:
    now = int(time.time() * 1000)
    assert _msg(clock="unix", ts=now - 1000).resolved_clock(now_ms=now) == "unix"
    assert _msg(clock="boot", ts=5000).resolved_clock(now_ms=now) == "boot"


def test_resolved_clock_rejects_contradictions() -> None:
    now = int(time.time() * 1000)
    # 说 unix 但 ts 只有 5 秒
    assert _msg(clock="unix", ts=5000).resolved_clock(now_ms=now) is None
    # 说 unix 但 ts 在 2000 年之前
    assert _msg(clock="unix", ts=MIN_UNIX_MS - 1).resolved_clock(now_ms=now) is None
    # 说 unix 但超前超过 1 天
    assert _msg(clock="unix", ts=now + 2 * 86_400_000).resolved_clock(now_ms=now) is None
    # 说 boot 但 ts 已经超过 3 年连续开机
    assert _msg(clock="boot", ts=MAX_BOOT_MS + 1).resolved_clock(now_ms=now) is None


def test_resolved_clock_falls_back_to_magnitude_when_missing() -> None:
    """clock 字段缺失时按量级推断。

    这是唯一一处用阈值猜基准的地方，而且只在字段缺失时发生——
    固件 0729abc 之后一定会带 clock，所以这条路径实际只在用
    mqtt_print 之类的手工工具造数据时走到。
    """
    now = int(time.time() * 1000)

    assert _msg(clock=None, ts=now - 1000).resolved_clock(now_ms=now) == "unix"
    assert _msg(clock=None, ts=5000).resolved_clock(now_ms=now) == "boot"
    # 量级不对：既不像 Unix 时间，又超过 boot 上限
    assert _msg(clock=None, ts=MAX_BOOT_MS + 1).resolved_clock(now_ms=now) is None
    # 像 Unix 时间但超前太多
    assert (
        _msg(clock=None, ts=now + 2 * 86_400_000).resolved_clock(now_ms=now) is None
    )

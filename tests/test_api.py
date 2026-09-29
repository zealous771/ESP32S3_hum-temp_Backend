"""七个 API 路由的测试（设计文档 §23）。

★ 用 ``TestClient``，不启真的 uvicorn、不连 broker、不连真数据库。

  fixture 把 ``mqtt_enabled=False`` 和临时数据库注进去，
  数据用 ingestor.handle_batch() 直接造——所以这些测试**完全不依赖
  设备和网络**，随时能跑。
"""

from __future__ import annotations

from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from app.clock import now_ms
from app.config import Settings
from app.db.database import Database
from app.main import create_app
from tests.test_ingestor import DEVICE, make_status, make_telemetry


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    """起了 lifespan 的测试客户端。

    ★ 用 ``with`` 而不是直接 ``TestClient(app)``：
      只有 with 才会触发 lifespan（startup/shutdown）。
      不触发的话写库线程不会启动——读接口照样能用，但那就不是在
      测真实运行状态了。
    """
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def seeded(client: TestClient, settings: Settings) -> TestClient:
    """塞几条真实形态的数据进去，供读接口断言。"""
    db: Database = client.app.state.db
    ingestor = client.app.state.ingestor

    base = now_ms() - 10_000
    with db.session() as s:
        ingestor.handle_batch(s, [make_status("online")])
        ingestor.handle_batch(
            s,
            [
                make_telemetry(i, ts=base + i * 2000, received_ms=base + i * 2000)
                for i in range(1, 6)
            ],
        )
    return client


# ── /api/health ─────────────────────────────────────────────────────


def test_health_returns_all_sections(client: TestClient) -> None:
    r = client.get("/api/health")
    assert r.status_code == 200

    body = r.json()
    # retention 是 Step 5 加的第五段。写在这里是为了让"少了一段"
    # 立刻失败——/api/health 的段名就是对外契约，前端按段读。
    for section in ("status", "now_ms", "mqtt", "ingest", "db",
                    "retention", "data", "settings"):
        assert section in body, f"缺少 {section} 段"


def test_health_http_status_is_always_200(client: TestClient) -> None:
    """★ MQTT 断开时 HTTP 码仍是 200。

    因为它同时被 Docker 的 HEALTHCHECK 用。返回 503 会让容器被判成
    不健康并重启——而重启解决不了 broker 的问题，还会丢掉写库队列里
    的数据。"MQTT 断开"通过响应体表达，不通过状态码。
    """
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["mqtt"]["enabled"] is False  # 测试里 mqtt_enabled=False


def test_health_reports_wal_mode(seeded: TestClient) -> None:
    """WAL 必须真的生效——这是启动时就校验过的，这里确认接口也报出来了。"""
    body = seeded.get("/api/health").json()
    assert body["db"]["journal_mode"] == "wal"
    assert body["db"]["telemetry_rows"] == 5
    assert body["db"]["devices"] == 1


def test_health_reports_data_liveness(seeded: TestClient) -> None:
    """★ data 段是"数据流还活着吗"的答案——为"设备静默停发"设计的。"""
    body = seeded.get("/api/health").json()
    data = body["data"]
    assert data["last_received_at"] is not None
    assert data["age_ms"] is not None and data["age_ms"] >= 0
    assert data["stale"] is False, "刚写进去的数据不该被判成停发"
    assert isinstance(data["recent_gaps"], list)


def test_health_detects_stale_stream(settings: Settings) -> None:
    """★ 造一段"设备静默停发"，health 必须报出来。"""
    app = create_app(settings)
    with TestClient(app) as c:
        db: Database = c.app.state.db
        ingestor = c.app.state.ingestor
        old = now_ms() - 3_600_000  # 一小时前
        with db.session() as s:
            ingestor.handle_batch(
                s,
                [make_telemetry(i, ts=old + i * 2000, received_ms=old + i * 2000)
                 for i in range(1, 4)],
            )

        body = c.get("/api/health").json()
        assert body["data"]["stale"] is True
        assert body["data"]["age_ms"] > 3_000_000


# ── /api/devices ────────────────────────────────────────────────────


def test_list_devices(client: TestClient) -> None:
    body = client.get("/api/devices").json()
    assert body["total"] == 0
    assert body["items"] == []


def test_list_devices_after_seed(seeded: TestClient) -> None:
    body = seeded.get("/api/devices").json()
    assert body["total"] == 1

    d = body["items"][0]
    assert d["device_id"] == DEVICE
    assert d["status"] == "online"
    # status 和 online 都要给：LWT 只在异常断连时触发，
    # 固件卡死时 status 会永远停在 online
    assert "online" in d and d["online"] is True


def test_device_detail_has_stats(seeded: TestClient) -> None:
    body = seeded.get(f"/api/devices/{DEVICE}").json()
    assert body["device_id"] == DEVICE
    assert body["telemetry_count"] == 5
    assert body["last_seq"] == 5
    assert body["first_ts"] is not None and body["last_ts"] is not None
    assert body["first_ts"] < body["last_ts"]


def test_unknown_device_returns_404(client: TestClient) -> None:
    """未知设备必须 404，不能返回"200 + 空对象"。

    否则前端分不清"这台设备没见过"和"见过但暂时没数据"，
    很容易把前者误显示成"设备离线"。
    """
    r = client.get("/api/devices/does-not-exist")
    assert r.status_code == 404
    assert "does-not-exist" in r.json()["detail"]


# ── /api/devices/{id}/latest ────────────────────────────────────────


def test_latest_keys_match_doc_exactly(seeded: TestClient) -> None:
    """★ 键集合必须**恰好**等于文档 §23 示例里的 5 个。

    多一个少一个都失败——这个接口会被 Web 首页每 5 秒调一次
    （文档 §25），响应体越小越好；而且契约固定之后前端才能放心用。
    """
    body = seeded.get(f"/api/devices/{DEVICE}/latest").json()
    assert set(body.keys()) == {"device_id", "temperature", "humidity", "rssi", "ts"}
    assert body["device_id"] == DEVICE
    assert body["ts"] == body["ts"]  # int


def test_latest_404_when_no_telemetry(client: TestClient) -> None:
    r = client.get(f"/api/devices/{DEVICE}/latest")
    assert r.status_code == 404


# ── /api/telemetry ──────────────────────────────────────────────────


def test_telemetry_pagination(seeded: TestClient) -> None:
    body = seeded.get("/api/telemetry", params={"limit": 2}).json()
    assert body["total"] == 5, "total 是符合条件的总数，不是当前页条数"
    assert len(body["items"]) == 2
    assert body["limit"] == 2


def test_telemetry_order_asc_desc(seeded: TestClient) -> None:
    desc = seeded.get("/api/telemetry", params={"limit": 5}).json()["items"]
    asc = seeded.get("/api/telemetry", params={"limit": 5, "order": "asc"}).json()["items"]
    assert desc[0]["ts"] > desc[-1]["ts"], "默认 desc（最新的在前）"
    assert asc[0]["ts"] < asc[-1]["ts"]
    assert [r["seq"] for r in asc] == [1, 2, 3, 4, 5]


def test_telemetry_time_range_filter(seeded: TestClient) -> None:
    all_items = seeded.get("/api/telemetry", params={"limit": 100, "order": "asc"}).json()
    items = all_items["items"]
    mid = items[2]["ts"]

    body = seeded.get(
        "/api/telemetry", params={"start": mid, "limit": 100, "order": "asc"}
    ).json()
    assert body["total"] == 3, "闭区间：start 那条也在内"


def test_telemetry_empty_range_is_not_an_error(seeded: TestClient) -> None:
    """start > end 是合法的空区间，返回空数组而不是报错。"""
    body = seeded.get(
        "/api/telemetry", params={"start": 9_999_999_999_999, "end": 1}
    ).json()
    assert body["total"] == 0
    assert body["items"] == []


def test_telemetry_excludes_boot_rows_by_default(settings: Settings) -> None:
    """boot 行的 ts 不是绝对时间，默认不能被时间范围查询捞出来。"""
    app = create_app(settings)
    with TestClient(app) as c:
        db: Database = c.app.state.db
        ingestor = c.app.state.ingestor
        with db.session() as s:
            ingestor.handle_batch(s, [make_telemetry(1)])
            ingestor.handle_batch(s, [make_telemetry(2, clock="boot", ts=5000)])

        default = c.get("/api/telemetry", params={"limit": 100}).json()
        with_boot = c.get(
            "/api/telemetry", params={"limit": 100, "include_boot": "true"}
        ).json()

        assert default["total"] == 1
        assert with_boot["total"] == 2


def test_telemetry_limit_upper_bound(seeded: TestClient) -> None:
    """limit 上限 1000。超了返回 422（FastAPI 的参数校验），
    而不是让调用方把内存打爆。"""
    assert seeded.get("/api/telemetry", params={"limit": 99999}).status_code == 422
    assert seeded.get("/api/telemetry", params={"limit": 0}).status_code == 422


def test_telemetry_invalid_order_rejected(seeded: TestClient) -> None:
    assert seeded.get("/api/telemetry", params={"order": "random"}).status_code == 422


# ── /api/anomalies 与 /api/predictions（本轮恒空）────────────────────


def test_anomalies_empty_but_valid(seeded: TestClient) -> None:
    """本轮 AI 还没实现，所以恒为空——但接口必须是完整的、参数可用的。"""
    body = seeded.get("/api/anomalies").json()
    assert body["total"] == 0
    assert body["items"] == []

    # 参数校验要生效（说明接口真的接上了，不是空壳）
    assert seeded.get("/api/anomalies", params={"level": "critical"}).status_code == 200
    assert seeded.get("/api/anomalies", params={"level": "bogus"}).status_code == 422


def test_predictions_empty_but_valid(seeded: TestClient) -> None:
    body = seeded.get("/api/predictions").json()
    assert body["total"] == 0
    assert body["items"] == []

    assert seeded.get("/api/predictions", params={"horizon": 300}).status_code == 200


# ── OpenAPI ─────────────────────────────────────────────────────────


def test_openapi_lists_all_seven_routes(client: TestClient) -> None:
    """设计文档 §23 要求的七个路由必须全部存在。

    这条测试的价值：将来有人重构路由前缀时，会立刻失败，
    而不是等前端联调时才发现少了一个。
    """
    paths = client.get("/openapi.json").json()["paths"]
    expected = {
        "/api/health",
        "/api/devices",
        "/api/devices/{device_id}",
        "/api/devices/{device_id}/latest",
        "/api/telemetry",
        "/api/anomalies",
        "/api/predictions",
    }
    assert expected <= set(paths), f"缺少路由：{expected - set(paths)}"

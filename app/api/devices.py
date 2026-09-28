"""``/api/devices`` 系列路由（设计文档 §23）。

    GET /api/devices                  设备列表
    GET /api/devices/{id}             单设备详情 + 统计
    GET /api/devices/{id}/latest      最新一条遥测
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status

from app.api.deps import DbSession
from app.api.schemas import DeviceDetailOut, DeviceOut, LatestTelemetryOut, Page
from app.clock import now_ms
from app.db import repository as repo

router = APIRouter(prefix="/api/devices", tags=["devices"])


@router.get("", response_model=Page[DeviceOut], summary="设备列表")
def list_devices(
    db: DbSession,
    limit: int = Query(100, ge=1, le=repo.MAX_LIMIT, description="每页条数"),
    offset: int = Query(0, ge=0, description="跳过条数"),
) -> Page[DeviceOut]:
    """列出所有见过的设备，按最近活跃排序。

    ``online`` 是**读时计算**的，不是库里的一列——理由见 schemas.DeviceOut
    的说明（LWT 只在异常断连时触发，固件卡死时 status 会永远停在 online）。
    """
    total = repo.count_devices(db)
    rows = repo.list_devices(db, limit=limit, offset=offset)
    now = now_ms()
    items = [
        DeviceOut(
            device_id=d.device_id,
            name=d.name,
            status=d.status,
            online=repo.is_online(d, now_ms=now),
            last_seen=d.last_seen,
            created_at=d.created_at,
        )
        for d in rows
    ]
    return Page(total=total, limit=limit, offset=offset, items=items)


@router.get("/{device_id}", response_model=DeviceDetailOut, summary="单设备详情")
def get_device(device_id: str, db: DbSession) -> DeviceDetailOut:
    """设备基本信息 + 几个从库里算出来的统计量。

    未知设备返回 **404**，而不是"200 + 空对象"。

    为什么：404 让调用方能明确区分"这台设备我没见过"和"这台设备见过
    但暂时没数据"。返回空对象的话，前端要自己判断所有字段是不是 null，
    而且很容易把"设备不存在"误显示成"设备离线"。
    """
    device = repo.get_device(db, device_id)
    if device is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"设备不存在：{device_id}",
        )

    stats = repo.get_device_stats(db, device_id)
    return DeviceDetailOut(
        device_id=device.device_id,
        name=device.name,
        status=device.status,
        online=repo.is_online(device, now_ms=now_ms()),
        last_seen=device.last_seen,
        created_at=device.created_at,
        telemetry_count=stats["telemetry_count"],
        first_ts=stats["first_ts"],
        last_ts=stats["last_ts"],
        last_seq=stats["last_seq"],
    )


@router.get(
    "/{device_id}/latest",
    response_model=LatestTelemetryOut,
    summary="最新一条遥测",
)
def latest_telemetry(
    device_id: str,
    db: DbSession,
    include_boot: bool = Query(
        False,
        description="是否把 clock='boot' 的行也纳入（ts 不是绝对时间，默认排除）",
    ),
) -> LatestTelemetryOut:
    """取该设备最新的一条遥测。

    ★ 响应字段**恰好**是设计文档 §23 示例里的 5 个
      （device_id / temperature / humidity / rssi / ts），
      刻意不多给——Web 首页每 5 秒调一次（文档 §25），响应体越小越好。
      要更多字段用 /api/telemetry。有测试盯住这个键集合。

    默认排除 boot 行：它的 ts 是开机毫秒（只有几千），在 1970 年附近，
    前端画图会把它画到最左边。设备刚开机、NTP 还没同步时库里可能
    只有这种行——那时"最新"取到它会很迷惑。

    没有任何遥测时返回 **404**（设备可能刚注册还没上报）。
    """
    row = repo.get_latest_telemetry(db, device_id, include_boot=include_boot)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"设备 {device_id} 还没有遥测数据"
            + ("" if include_boot else "（可试 include_boot=true 查 clock='boot' 的行）"),
        )

    return LatestTelemetryOut(
        device_id=row.device_id,
        temperature=row.temperature,
        humidity=row.humidity,
        rssi=row.rssi,
        ts=row.ts,
    )

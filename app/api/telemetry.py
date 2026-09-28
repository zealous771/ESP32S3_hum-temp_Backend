"""``GET /api/telemetry`` —— 历史遥测查询（设计文档 §23）。

这是 Web 曲线图的数据源（文档 §24 的"温度曲线 / 湿度曲线"）。
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.api.deps import DbSession
from app.api.schemas import Page, TelemetryOut
from app.db import repository as repo

router = APIRouter(prefix="/api/telemetry", tags=["telemetry"])


@router.get("", response_model=Page[TelemetryOut], summary="历史遥测查询")
def query_telemetry(
    db: DbSession,
    device_id: str | None = Query(None, description="按设备过滤；不传则查所有设备"),
    start: int | None = Query(
        None, ge=0, description="起始时刻（Unix 毫秒，闭区间）"
    ),
    end: int | None = Query(None, ge=0, description="结束时刻（Unix 毫秒，闭区间）"),
    limit: int = Query(repo.DEFAULT_LIMIT, ge=1, le=repo.MAX_LIMIT),
    offset: int = Query(0, ge=0),
    order: str = Query(
        "desc",
        pattern="^(asc|desc)$",
        description="按 ts 排序方向。desc（默认）适合看最新，asc 适合画曲线",
    ),
    include_boot: bool = Query(
        False,
        description="是否包含 clock='boot' 的行（ts 不是绝对时间，默认排除）",
    ),
) -> Page[TelemetryOut]:
    """按设备 + 时间范围查历史遥测。

    ── 参数命名的选择 ──────────────────────────────────────────────

    用 ``start`` / ``end`` 而不是 ``from`` / ``to``：
    ``from`` 是 Python 关键字，FastAPI 里要写成
    ``Query(alias="from")`` 才能对上，多一层间接。
    （如果将来确实要和某个前端约定 ``from``，加 alias 即可。）

    ── 时间格式 ────────────────────────────────────────────────────

    Unix **毫秒**整数，与固件报文、数据库、文档 §23 的示例完全一致。
    不用 ISO 字符串：毫秒整数没有时区歧义，前端 `new Date(ms)` 一行就够。

    ── 边界行为 ────────────────────────────────────────────────────

    - ``start > end`` 返回空数组（合法的空区间查询，不是错误）
    - 不传 ``device_id`` 就是查所有设备（多设备对比曲线的用法）
    - ★ 默认带 ``clock='unix'`` 条件。这不只是正确性要求
      （boot 行的 ts 是开机毫秒，混进时间范围查询会污染结果），
      也是性能要求——部分索引只在带这个条件时才用得上。
      要查 boot 行得显式传 include_boot=true。
    """
    total, rows = repo.query_telemetry(
        db,
        device_id=device_id,
        start_ms=start,
        end_ms=end,
        limit=limit,
        offset=offset,
        descending=(order == "desc"),
        include_boot=include_boot,
    )
    return Page(
        total=total,
        limit=limit,
        offset=offset,
        items=[TelemetryOut.model_validate(r) for r in rows],
    )

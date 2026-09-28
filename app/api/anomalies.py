"""``GET /api/anomalies`` —— 异常记录查询（设计文档 §23）。

★ 本轮**恒返回空列表**（total=0, items=[]），因为 AI 层还没实现
  （文档 §26-§30 排在最后，用户也明确要求 AI 最后做）。

  那为什么现在就把路由、参数、响应模型全做好？

      为了把"表结构和接口形状"与"算法实现"解耦。算法会换
      （规则 → 变化率 → Rolling Z-Score → Isolation Forest），
      但接口不该跟着变。前端可以先把异常列表页搭起来，
      Phase 8 一开始写数据，页面立刻就有内容。

  这不是"预留代码"，是**已经完成的接口**——只是暂时没有数据而已。
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.api.deps import DbSession
from app.api.schemas import AnomalyOut, Page
from app.db import repository as repo

router = APIRouter(prefix="/api/anomalies", tags=["anomalies"])


@router.get("", response_model=Page[AnomalyOut], summary="异常记录查询")
def query_anomalies(
    db: DbSession,
    device_id: str | None = Query(None, description="按设备过滤"),
    start: int | None = Query(None, ge=0, description="起始时刻（Unix 毫秒）"),
    end: int | None = Query(None, ge=0, description="结束时刻（Unix 毫秒）"),
    metric: str | None = Query(
        None, description="按指标过滤：temperature / humidity / rssi / ..."
    ),
    level: str | None = Query(
        None, pattern="^(info|warning|critical)$", description="按严重级别过滤"
    ),
    min_score: float | None = Query(
        None,
        description="只要分数 >= 这个值的。★ 不同算法量纲不同"
        "（Z-Score 是标准差倍数、Isolation Forest 是 0~1），"
        "所以这个参数只在同一算法内比较才有意义",
    ),
    limit: int = Query(repo.DEFAULT_LIMIT, ge=1, le=repo.MAX_LIMIT),
    offset: int = Query(0, ge=0),
) -> Page[AnomalyOut]:
    """按条件查异常记录。本轮库里没有数据，返回空页。"""
    total, rows = repo.query_anomalies(
        db,
        device_id=device_id,
        start_ms=start,
        end_ms=end,
        metric=metric,
        level=level,
        min_score=min_score,
        limit=limit,
        offset=offset,
    )
    return Page(
        total=total,
        limit=limit,
        offset=offset,
        items=[AnomalyOut.model_validate(r) for r in rows],
    )

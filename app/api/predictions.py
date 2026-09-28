"""``GET /api/predictions`` —— 预测记录查询（设计文档 §23）。

★ 本轮**恒返回空列表**，理由同 anomalies.py：Phase 9 才实现预测
  （文档 §31），但接口现在就定好，前端可以先把页面搭起来。

★ 一个必须在 Phase 9 之前定死的语义（否则将来没法回溯预测准不准）：

    ``ts``      是**预测生成时刻**，不是被预测的时刻
    ``horizon`` 是向前预测多少秒

    被预测的时刻 = ``ts + horizon * 1000``。

    为什么这样定：预测是"在某时刻做出的一个关于未来的陈述"。
    同一个未来时刻可能被预测多次（模型改进后重算），
    如果 ts 存的是被预测时刻，两次不同时间做出的预测会混在一起，
    分不清哪个是后来修正的。存生成时刻才能按"预测批次"回溯。
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.api.deps import DbSession
from app.api.schemas import Page, PredictionOut
from app.db import repository as repo

router = APIRouter(prefix="/api/predictions", tags=["predictions"])


@router.get("", response_model=Page[PredictionOut], summary="预测记录查询")
def query_predictions(
    db: DbSession,
    device_id: str | None = Query(None, description="按设备过滤"),
    metric: str | None = Query(None, description="按指标过滤"),
    horizon: int | None = Query(
        None, ge=1, description="按预测跨度过滤（秒）。例：300 = 预测 5 分钟后"
    ),
    start: int | None = Query(
        None, ge=0, description="预测**生成**时刻的下界（Unix 毫秒）"
    ),
    end: int | None = Query(None, ge=0, description="预测**生成**时刻的上界"),
    limit: int = Query(repo.DEFAULT_LIMIT, ge=1, le=repo.MAX_LIMIT),
    offset: int = Query(0, ge=0),
) -> Page[PredictionOut]:
    """按条件查预测记录。本轮库里没有数据，返回空页。"""
    total, rows = repo.query_predictions(
        db,
        device_id=device_id,
        metric=metric,
        horizon=horizon,
        start_ms=start,
        end_ms=end,
        limit=limit,
        offset=offset,
    )
    return Page(
        total=total,
        limit=limit,
        offset=offset,
        items=[PredictionOut.model_validate(r) for r in rows],
    )

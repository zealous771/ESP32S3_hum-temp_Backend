"""AI 占位层的测试（Phase 6 阶段的**契约测试**）。

★ 为什么现在就要测一堆"还没实现"的东西：

  本轮的契约是"**调用即抛 NotImplementedError**"，而不是"返回空结果"。
  这两者的差别很实际：

    返回 []  → 页面显示"没有异常"，和"AI 说没有异常"长得一模一样，
               没人知道功能还没做
    抛异常   → 任何误接线都会立刻炸出来

  所以"抛异常"这件事本身是需要被测试锁住的——哪天有人为了"先跑起来"
  把某个入口改成 `return []`，这个测试会失败，逼他解释为什么。

★ 常量也是接口的一部分：

  ``FEATURE_COLUMNS`` 的顺序、``SPLIT_RATIOS`` 的和、
  ``HORIZONS_MINUTES`` 的取值——训练和推理两侧必须用**完全相同的
  特征顺序**，顺序错了模型会静默读错列（不报错、只是预测不准）。
  把顺序写进测试，等于给这个不变量加了一道锁。
"""

from __future__ import annotations

import pytest

from app.ai import anomaly, features, forecast


# ── features ────────────────────────────────────────────────────────


def test_feature_columns_are_unique_and_ordered() -> None:
    """特征名不能重复——重复了模型会把同一列读两次，而且看不出来。"""
    cols = features.FEATURE_COLUMNS
    assert len(cols) == len(set(cols))
    assert "temperature" in cols and "humidity" in cols


def test_rolling_window_is_30_minutes() -> None:
    """窗口 = 900 点 × 2 秒 = 30 分钟（文档 §29 的建议值）。

    这个换算写死在测试里是有意的：窗口点数依赖采样周期，
    而采样周期是固件侧的配置（CONFIG_BOARD_SAMPLE_INTERVAL_MS）。
    哪天有人把采样周期改成 1 秒，900 点就变成 15 分钟——
    这个断言会失败，提醒他"窗口的物理含义变了"。
    """
    sample_interval_s = 2
    assert features.ROLLING_WINDOW_POINTS * sample_interval_s == 30 * 60


def test_split_ratios_sum_to_one() -> None:
    """切分比例必须精确等于 1。

    不等于 1 的话，切出来的三段要么重叠要么漏掉数据——
    而漏掉的那段会静默地不进任何集合，指标照样算得出来。
    """
    assert sum(features.SPLIT_RATIOS) == pytest.approx(1.0)
    # 文档 §32 的 7:1.5:1.5
    assert features.SPLIT_RATIOS == (0.70, 0.15, 0.15)


def test_build_features_not_implemented() -> None:
    with pytest.raises(NotImplementedError, match="Phase 8"):
        features.build_features([])


def test_time_ordered_split_not_implemented() -> None:
    with pytest.raises(NotImplementedError, match="Phase 8"):
        features.time_ordered_split(None)  # type: ignore[arg-type]


# ── anomaly ─────────────────────────────────────────────────────────


def test_anomaly_levels_and_methods() -> None:
    assert anomaly.LEVELS == ("info", "warning", "critical")
    # 四种方法名对应文档 §27-§30 的四版实现，顺序就是实现顺序
    assert {
        anomaly.METHOD_RULES,
        anomaly.METHOD_DELTA,
        anomaly.METHOD_ZSCORE,
        anomaly.METHOD_ISOLATION_FOREST,
    } == {"rules", "delta", "zscore", "iforest"}


def test_anomaly_hit_fields_match_table() -> None:
    """``AnomalyHit`` 的字段要和 anomalies 表的列对得上。

    对不上的后果是 Phase 8 落库时才发现少一列，而那时改的是
    "表结构 + 算法 + 接口"三处。
    """
    hit = anomaly.AnomalyHit(
        ts=1, metric="temperature", score=0.9, level="warning", reason="test"
    )
    assert {f for f in hit.__dataclass_fields__} == {
        "ts", "metric", "score", "level", "reason"
    }


def test_detect_not_implemented() -> None:
    with pytest.raises(NotImplementedError, match="Phase 8"):
        anomaly.detect([])


# ── forecast ────────────────────────────────────────────────────────


def test_forecast_horizons_and_metrics() -> None:
    # 文档 §33 的输出示例里给了 t+5m / t+10m
    assert 5 in forecast.HORIZONS_MINUTES and 10 in forecast.HORIZONS_MINUTES
    # rssi 故意不在里面：它由无线电环境决定，和温湿度的时间序列无关
    assert forecast.FORECAST_METRICS == ("temperature", "humidity")
    assert "rssi" not in forecast.FORECAST_METRICS


def test_forecast_point_semantics() -> None:
    """``made_at_ms`` 是**做出预测的时刻**，不是被预测的时刻。

    ★ 这个语义容易混，混了会让画出来的曲线整体错位一段时间，
      而曲线看起来还是"很像"的，不容易发现。
      被预测的时刻 = made_at_ms + horizon * 60_000。
    """
    point = forecast.ForecastPoint(
        metric="temperature", horizon=5, value=33.0, made_at_ms=1_790_600_000_000
    )
    predicted_for = point.made_at_ms + point.horizon * 60_000
    assert predicted_for - point.made_at_ms == 300_000  # 5 分钟


def test_forecast_not_implemented() -> None:
    with pytest.raises(NotImplementedError, match="Phase 8"):
        forecast.forecast([])


# ── 整个包 ──────────────────────────────────────────────────────────


def test_ai_package_exports_nothing_yet() -> None:
    """``app/ai/__init__.py`` 的 ``__all__`` 是空的——有意为之。

    里面还有未实现的模块，导出它们会让人以为"可以直接用"。
    等 Phase 8 真正实现时再把入口放出来。
    """
    import app.ai as ai_pkg

    assert ai_pkg.__all__ == []

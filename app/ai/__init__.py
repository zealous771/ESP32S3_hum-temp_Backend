"""AI 层 —— **本轮不实现**。

设计文档把 AI 排在最后（§42 明确列在"现在不要做的事情"里），
理由是 §26 的第一原则：

    先有数据，再训练模型。
    不要在没有历史数据时直接训练 LSTM。
    建议先积累：至少几天 / 最好几周 的真实传感器数据。

所以本目录目前只有占位。**但占位是"签名写全、实现抛异常"，
不是空文件**——这样接口形状先定下来，算法可以换（规则 → Z-Score →
Isolation Forest），而调用方不用跟着改：

    features.py   FEATURE_COLUMNS / ROLLING_WINDOW_POINTS / SPLIT_RATIOS
                  build_features() / time_ordered_split()
    anomaly.py    LEVELS / METHOD_* / AnomalyHit / detect()
    forecast.py   HORIZONS_MINUTES / FORECAST_METRICS / ForecastPoint / forecast()

★ 实现一律 `raise NotImplementedError`，不返回空列表。

  这是刻意的：返回空列表会让"AI 还没实现"看起来像"AI 说没有异常"，
  而这两件事在页面上长得一模一样。抛异常则任何误接线都会立刻暴露。

等数据攒够了，按下面顺序实现
（每一版都能独立验收，见 §27-§31）：

    第一版  §27  规则检测   温度<0/>50、湿度<5/>95       —— 文档自己说"这不算真正的 AI"
    第二版  §28  变化率     delta_temperature / delta_humidity
    第三版  §29  Rolling Z-Score   z = (x - mean) / std，窗口约 30 分钟
    第四版  §30  Isolation Forest  特征见 §30 的清单
    预测    §31  先树模型 / 梯度提升，深入再做 GRU / LSTM / Transformer

★ 一条必须记住的禁忌（§32）：**时间序列绝不能随机切分**。
  必须按时间序 前 70% Train / 中 15% Val / 后 15% Test。
  随机切分会让模型"看到未来"，离线指标好看得上天，上线就废。

本轮明确不引入的依赖：numpy / pandas / scikit-learn（等真正开工时再加）。
"""

__all__: list[str] = []

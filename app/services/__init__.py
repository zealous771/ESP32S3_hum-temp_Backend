"""业务编排层。

    telemetry_service.py   TelemetryIngestor：把一批 MQTT 报文变成数据库行

这一层存在的理由：MQTT 回调不该知道数据库长什么样，API 也不该知道
MQTT 报文长什么样。两者都只认识这一层提供的接口。

它同时是**测试的主要入口**——直接调 `TelemetryIngestor.handle_batch()`
就能覆盖"报文 → 数据库"的全部逻辑，不需要起 broker、不需要 paho。
"""

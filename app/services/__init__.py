"""业务编排层。

    telemetry_service.py   TelemetryIngestor：把一批 MQTT 报文变成数据库行
    retention.py           RetentionService：定期删除过期遥测

这一层存在的理由：MQTT 回调不该知道数据库长什么样，API 也不该知道
MQTT 报文长什么样。两者都只认识这一层提供的接口。

它同时是**测试的主要入口**——直接调 `TelemetryIngestor.handle_batch()`
就能覆盖"报文 → 数据库"的全部逻辑，不需要起 broker、不需要 paho；
`RetentionService.run_once()` 同理，不需要等定时器。

★ 这两个服务都各自拥有一个后台线程，而且**失败模式的处理方式完全相同**：
  循环体包在 try/except 里。一次未捕获的异常会让线程退出，之后
  再也不做它该做的事，而**外面看不出任何异常**——写库线程退出表现为
  "连接正常但没有数据"，保留线程退出表现为"磁盘某天满了"。
  这类"静默死亡"是本工程最难查的故障形态，所以两处都写了同样的兜底。
"""

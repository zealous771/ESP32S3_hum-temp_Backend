"""MQTT 接入层。

两个文件：

    schemas.py   入站报文契约（Pydantic 模型）+ topic 解析
    consumer.py  paho 客户端、订阅、回调、队列

设计要点见各文件顶部。
"""

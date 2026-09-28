"""pytest 公共 fixture。

★ 关键：测试用**临时文件**数据库，不用 ``:memory:``。

  为什么：SQLAlchemy 对 ``sqlite:///:memory:`` 用 SingletonThreadPool，
  也就是**每个线程一个独立的空内存库**。而本工程的写库线程和测试线程
  是两个线程——于是测试里"写入"和"查询"看到的是两个不同的库，
  表现为"明明写了却查不到"，而且不报任何错。

  用临时文件就没这个问题：所有线程看到同一个文件。
  （WAL 模式下多线程共享一个文件也是正确用法。）
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest

from app.config import Settings
from app.db.database import Database


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """测试用配置：临时库 + 不连 MQTT。

    ``mqtt_enabled=False`` 让单元测试不需要 broker 就能跑。
    """
    return Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'iot.db'}",
        mqtt_enabled=False,
    )


@pytest.fixture
def db(settings: Settings) -> Iterator[Database]:
    database = Database(settings)
    database.init_schema()
    yield database
    database.dispose()

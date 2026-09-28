"""FastAPI 依赖注入。

目前只有一个依赖 ``get_db``，但它是**测试能注入临时数据库**的关键。

为什么不直接 import 一个全局的 Database：

    全局变量只能有一个。而测试需要每个用例一个独立的临时库
    （见 tests/conftest.py 的 fixture）。把 Database 挂在
    ``app.state`` 上之后，``create_app(settings)`` 就能造出互不干扰的实例。

    这也是"显式依赖"而不是"隐式全局"在 Web 框架里的具体体现。
"""

from __future__ import annotations

from typing import Annotated, Iterator

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.db.database import Database


def get_db(request: Request) -> Iterator[Session]:
    """从 app.state 取出 Database，开一个 Session，请求结束关闭。

    用 ``yield`` 的依赖在 FastAPI 里会被包成 context manager：
    请求处理完（包括抛异常）之后回到这里执行 ``finally``。
    所以不需要在路由里手动 close。

    注意这里**不提交**：读接口不需要，写接口（保留策略清理）
    自己用 ``db.session()`` 那个 context manager。
    让依赖"只负责开和关"、把事务边界留给业务代码，责任更清楚。
    """
    database: Database = request.app.state.db
    session = database.open_session()
    try:
        yield session
    finally:
        session.close()


# 类型别名。写成别名而不是每个路由都写一遍 Depends(get_db)，
# 是为了让函数签名保持可读：`db: DbSession` 比
# `db: Session = Depends(get_db)` 短得多，参数多的时候差别明显。
DbSession = Annotated[Session, Depends(get_db)]

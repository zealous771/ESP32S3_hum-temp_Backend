"""持久化层。

三个文件：

    database.py   engine / Session 工厂 / PRAGMA 设置 / init_db()
    models.py     四张表的 ORM 模型（文档 §21）
    repository.py 所有查询与写入（**唯一写 SQL 的地方**）

为什么把 SQL 全部收在 repository.py：

    api/ 和 services/ 都不直接拼查询。这样将来从 SQLite 换到 PostgreSQL
    （文档 §22 的计划）时，要改的只有这一个文件；
    而且"哪些查询没有过滤 clock='unix'"这种审计只需要看一个地方。
"""

# 数据库结构

只保留一个 SQL 文件：`schema.sql`，描述当前 MySQL 业务表结构。

- 当前表：`agent_session`、`agent_message`、`todos`。
- 数据库结构变化时同步更新该文件；历史版本通过 Git 和 Alembic revision 追溯。
- 已有数据库使用 `uv run alembic upgrade head` 升级，不重复执行建表 SQL。
- SQL 不包含旧运行归档表，也不管理 Alembic 版本号。

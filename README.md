# Mini Agent

Python 3.12 + FastAPI 服务骨架，使用 uv 管理依赖。沿用参考项目的
`server/routes`、`server/store`、独立 LLM 模块以及 `AGENT_` 环境变量命名。

基础设施：SQLAlchemy 2 异步 MySQL 连接池（asyncmy）、Alembic 数据库迁移、
可选 Redis、OpenAI 兼容协议大模型客户端（普通响应和 SSE 流式响应）。

## 安装与启动

```bash
uv sync --locked
# 首次拉取项目时复制配置；不要覆盖已经填写的本地配置。
cp .env.development.example .env.development
```

在 `.env.development` 中填写 MySQL 地址、账号、密码和数据库名。
数据库需要提前创建，例如：

```sql
CREATE DATABASE mini_agent_dev CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE DATABASE mini_agent_test CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
```

分别为开发和测试库配置有权限的账号。服务不会自动创建数据库或业务表。
启动开发环境：

```bash
bash run_local.sh
# 等价命令：APP_ENV=development uv run python main.py
```

PyCharm 选择 `.venv/bin/python`，运行 `main.py`，环境变量设置 `APP_ENV=development`。
命令行也支持 `uv run uvicorn main:app --reload`，但直接使用 Uvicorn 时，
host、port、reload 等进程参数由命令行决定；`python main.py` 会读取这些配置。

- 开发文档：http://127.0.0.1:8000/docs
- 进程存活：http://127.0.0.1:8000/health
- 依赖就绪：http://127.0.0.1:8000/health/ready

连接按需建立。MySQL 未就绪时，存活探针仍返回 200，就绪探针对启用但不可用的
MySQL/Redis 返回 503。就绪检查不会调用模型 API。
暂时只调试 HTTP 服务时，可在开发环境设置 `AGENT_MYSQL_ENABLED=false`。

## 三套环境配置

| 环境 | APP_ENV | 公开默认配置 | 本地密钥配置 |
| --- | --- | --- | --- |
| 开发 | development（默认） | config/development.toml | .env.development |
| 测试 | testing | config/testing.toml | .env.testing |
| 生产 | production | config/production.toml | .env.production |

`APP_ENV` 必须在启动前设置；不从 dotenv 中选择环境。只加载选中环境的 dotenv，
不读取通用 `.env`，也不回退加载其他环境的文件。配置优先级：
显式初始化参数 > 系统环境变量 > 当前环境 dotenv > secrets source > 当前环境 TOML > 字段默认值。
配置加载后缓存，修改配置需重启进程。

TOML 与 `.env.*.example` 可以提交到 Git；`.env.development`、`.env.testing`、
`.env.production` 已忽略。密码和 Key 使用 `SecretStr`，配置展示时会隐藏。
环境变量与字段对应，例如 `mysql_host` → `AGENT_MYSQL_HOST`。
列表配置使用 JSON，例如 `AGENT_CORS_ORIGINS='["https://app.example.com"]'`。

测试环境默认关闭 MySQL、Redis，使用 echo 模型。需要集成测试服务时，
复制 `.env.testing.example` 为 `.env.testing` 并启用对应服务；只使用专用测试库。
单元测试会隔离本地 dotenv 和 `AGENT_` 环境变量，不会访问真实服务。

生产环境默认关闭热重载和接口文档，要求配置 MySQL 地址、账号、密码以及真实模型。
缺失必要配置会在启动时抛错。部署时填写 `.env.production`，或由部署系统直接注入变量：

```bash
uv sync --locked --no-dev
APP_ENV=production uv run --no-dev python main.py
```

生产迁移作为独立部署步骤执行，不在每个服务进程启动时自动执行。

## 大模型接入

开发与测试默认 `AGENT_LLM_PROVIDER=echo`，不访问网络。
调用真实模型时设置：

```dotenv
AGENT_LLM_PROVIDER=openai_compatible
AGENT_LLM_BASE_URL=https://your-provider.example/compatible-mode/v1
AGENT_LLM_API_KEY=填写实际密钥
AGENT_LLM_MODEL=填写实际模型名称
AGENT_LLM_TIMEOUT=60
```

Base URL 填写供应商的 API 根路径，客户端追加 `chat/completions`。
模型名称由供应商决定。可对接使用该协议的国内外模型服务，不包含供应商特有接口适配。
HTTP 客户端按参考项目设置 `trust_env=False`，默认直连，不读取系统代理。
接口错误与超时向调用方抛出，不自动重试或回退，避免重复生成和意外计费。

业务路由可通过 `Depends(server.dependencies.get_llm)` 获取客户端；调用方式：

```python
from server.llm import Message

messages = [Message(role="user", content="你好")]
text = await llm.complete(messages)
async for chunk in llm.stream(messages):
    ...
```

当前只提供内部客户端，没有公开的模型调用 HTTP 路由。模型客户端、数据库连接池、
Redis 客户端由应用 lifespan 创建并在退出时关闭。

## MySQL 与迁移

连接参数集中在 `server/config.py`，连接池使用 `utf8mb4`、断线检测、连接回收，
密码通过 SQLAlchemy URL 对象传递，可包含 `@`、`/`、`#` 等特殊字符。
路由通过 `Depends(server.store._db.get_session)` 获取异步 session；
请求成功提交事务，抛出异常时回滚。

ORM 模型继承 `server.store.base.Base`，新增模型后在 `migrations/env.py` 中导入模型模块，
再生成、检查并应用迁移：

```bash
APP_ENV=development uv run alembic revision --autogenerate -m "create example table"
APP_ENV=development uv run alembic upgrade head
APP_ENV=development uv run alembic current
```

目前未定义业务模型，迁移目录为空。测试、生产迁移使用各自的 `APP_ENV`，
并提前核对目标库。测试环境需显式开启 `AGENT_MYSQL_ENABLED=true`。

## Redis

默认关闭；需要时设置 `AGENT_REDIS_ENABLED=true`，填写 `AGENT_REDIS_HOST`、
`AGENT_REDIS_PORT`、`AGENT_REDIS_DB`、`AGENT_REDIS_PASSWORD`。
TLS 服务启用 `AGENT_REDIS_SSL=true`。开发和测试默认使用不同的 Redis DB。
业务路由通过 `Depends(server.dependencies.get_redis)` 获取客户端。

## 开发检查

```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
```

添加依赖使用 `uv add 包名`，开发依赖使用 `uv add --dev 包名`。
提交 `pyproject.toml` 与 `uv.lock`，以便复现相同依赖版本。

## 目录

```text
config/                    # 开发、测试、生产公开默认配置
server/
  app.py                   # 应用工厂、客户端生命周期、CORS
  config.py                # 配置加载、校验与 SecretStr
  dependencies.py          # LLM、Redis 路由依赖
  cache.py                 # Redis 客户端
  llm.py                   # 普通/流式模型调用、开发 echo
  routes/health.py         # 存活和就绪探针
  store/
    _db.py                 # MySQL 连接池与 session 依赖
    base.py                # ORM 基类
migrations/                # Alembic 迁移环境及版本目录
tests/                     # 环境隔离、配置、LLM、生命周期测试
.env.*.example             # 各环境配置填写示例
main.py                    # 保持兼容的应用入口
run_local.sh               # 本地启动
pyproject.toml
uv.lock
```

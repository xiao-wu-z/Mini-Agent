# Mini Agent

Python 3.12 + FastAPI 服务骨架，使用 uv 管理依赖。沿用参考项目的
`server/routes`、`server/store`、独立 LLM 模块以及 `AGENT_` 环境变量命名。

基础设施：SQLAlchemy 2 异步 MySQL 连接池（asyncmy）、Alembic 数据库迁移、
可选 Redis、原生 Anthropic Messages 大模型客户端，以及自研 tool-use Agent Loop。

## 安装与启动

准备 Python/uv 和已启动的 Docker（含 Compose v2）。MySQL、Redis 由 Compose 运行，
Python 应用在宿主机运行，容器与应用共用 `.env.development` 中的连接参数。

```bash
uv sync --locked
# 首次拉取项目时复制配置；不要覆盖已经填写的本地配置。
cp .env.development.example .env.development
# 修改示例密码后启动 MySQL、Redis，等待健康检查通过。
docker compose --env-file .env.development up -d --wait --wait-timeout 180
bash run_local.sh
```

默认使用 MySQL 8.4 和 Redis 7.4。MySQL 首次启动自动创建 `mini_agent_dev`
及应用账号，无需手工建库；Redis 启用密码和 AOF 持久化。
应用配置中的数据库密码和 Redis 密码同时用于初始化对应容器。
`DOCKER_MYSQL_ROOT_PASSWORD` 仅用于容器管理，应用使用独立的 `AGENT_MYSQL_USER`。
示例中的密码只用于本地开发，生产必须替换。

默认只向宿主机 `127.0.0.1` 映射端口：MySQL `3306`、Redis `6379`。
端口冲突时修改 `.env.development` 中的 `AGENT_MYSQL_PORT` / `AGENT_REDIS_PORT`，
Compose 与 Python 应用会读取同一组值；容器内部仍使用 `3306` / `6379`。
应用在宿主机运行，因此 `AGENT_MYSQL_HOST` / `AGENT_REDIS_HOST` 保持 `127.0.0.1`。

## 容器管理

```bash
# 查看状态、日志
docker compose --env-file .env.development ps
docker compose --env-file .env.development logs -f mysql redis

# 停止并移除容器；保留数据卷
docker compose --env-file .env.development down

# 再次启动，继续使用原数据
docker compose --env-file .env.development up -d --wait
```

MySQL 和 Redis 分别使用命名卷 `mysql_data` / `redis_data`，按 Compose 项目名隔离。
不要对需要保留的数据执行 `down -v`，它会删除数据卷。
MySQL 的建库、建用户和初始密码只对空数据卷生效；已有数据时修改 dotenv
不会修改库内密码，需要执行 SQL 变更账号并同步应用配置。

测试容器使用独立项目名、数据卷和宿主机端口（MySQL `3307`、Redis `6380`）：

```bash
cp .env.testing.example .env.testing
docker compose --env-file .env.testing up -d --wait --wait-timeout 180
# 开启测试服务连接后，启动测试环境应用
APP_ENV=testing AGENT_MYSQL_ENABLED=true AGENT_REDIS_ENABLED=true uv run python main.py
```

测试 MySQL 自动创建 `mini_agent_test`，使用独立测试账号；单元测试仍默认隔离外部服务。
生产可使用同一 Compose 文件和 `.env.production`，需要填写完整账号、密码及宿主机连接地址。
各环境通过 `COMPOSE_PROJECT_NAME` 隔离数据，若在同机同时运行，需配置不同宿主机端口。
部署在不同主机的应用需要单独配置网络访问；当前端口映射仅用于同机访问。

## 应用运行

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
AGENT_LLM_PROVIDER=anthropic
AGENT_LLM_BASE_URL=https://your-provider.example/anthropic
AGENT_LLM_API_KEY=填写实际密钥
AGENT_LLM_MODEL=填写实际模型名称
AGENT_LLM_TIMEOUT=60
AGENT_LLM_MAX_TOKENS=4096
AGENT_LLM_AUTH_HEADER=x-api-key
```

Base URL 填写供应商的 Anthropic API 根路径，客户端追加 `/v1/messages`。
如果 Base URL 已以 `/v1` 结尾，则只追加 `/messages`。直连 Anthropic 时根路径为
`https://api.anthropic.com`，也支持供应商提供的 Anthropic 兼容地址。
默认使用 `x-api-key`；兼容服务若要求 Bearer Token，将 `AGENT_LLM_AUTH_HEADER` 设为
`authorization`。模型名称由供应商决定。
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

当前提供内部客户端和命令行 Agent 示例。模型客户端、数据库连接池、
Redis 客户端由应用 lifespan 创建并在退出时关闭。

## Agent Loop（第一阶段）

`server/agent_loop.py` 的主流程是自行实现的 `while` 循环，使用 Anthropic 原生内容块：

```text
用户输入 + 历史消息
  → LLM.run_turn(messages, tools, system)
  → 保存 assistant 完整 content blocks
  → 有 tool_use：校验参数、执行全部工具
  → 把全部 tool_result 放入紧随其后的 user 消息
  → 下一轮 LLM；无工具调用则返回最终文本
```

工具使用 `name`、`description`、`input_schema` 注册，Schema 由 Pydantic 参数模型生成。
沿用 mtagent 的组织方式：`server/tools.py` 维护注册表和默认注册，`server/skill_runner.py`
统一处理执行、超时和错误回灌，具体能力放在 `skills/<工具名>/handler.py`。
第一阶段的 `skills/calculator/handler.py` 支持两数加、减、乘、除。
增加工具时添加对应 handler 并注册，不需要修改 Loop。

工具调用与回灌消息示例：

```json
[
  {
    "role": "assistant",
    "content": [
      {"type": "tool_use", "id": "toolu_1", "name": "calculator",
       "input": {"operation": "multiply", "left": 128, "right": 36}}
    ]
  },
  {
    "role": "user",
    "content": [
      {"type": "tool_result", "tool_use_id": "toolu_1",
       "content": "{\"ok\": true, \"data\": {\"result\": 4608}}"}
    ]
  }
]
```

填写真实模型配置后运行：

```bash
uv run python -m scripts.run_agent '计算 128 乘以 36'
```

`echo` 模式仅用于检查入口，不会自主调用工具。测试中的 Mock 模型覆盖完整工具闭环，
不需要真实 Key，也不依赖 MySQL 或 Redis。

代码调用与追问：

```python
from server.agent_loop import AgentLoop
from server.tools import create_default_tools

loop = AgentLoop(llm, create_default_tools(), max_rounds=8, tool_timeout=10)
first = await loop.run("计算 128 乘以 36")
followup = await loop.run("再除以 2", history=first.messages)
```

每次调用独立持有消息副本，通过 `history` 显式接续；后续 Session 层负责按用户和窗口持久化历史。
默认最多调用模型 8 轮；同一轮的多工具按顺序执行并集中回灌。工具执行超时、未知工具、
参数不合法及执行异常均转为 `is_error=true` 的结果，供模型修正。
达到轮次上限返回 `max_rounds`；模型输出截断返回 `max_tokens`，不会执行截断响应中的工具。
LLM 网络错误、协议错误向调用方抛出；任务取消会继续传播，不会伪装成普通工具失败。

完整 assistant 内容块原样回灌，包括供应商返回的 thinking/signature，保证协议完整性；
对用户只返回 text 块。每次运行返回 `run_id`、`rounds`、`messages` 和 `tool_traces`，
日志记录工具名称、调用 ID、耗时及错误状态，不输出模型内部推理或工具参数。

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

开发环境默认启用 Docker Redis；其他环境通过 `AGENT_REDIS_ENABLED` 控制，填写 `AGENT_REDIS_HOST`、
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
docker-compose.yml         # MySQL / Redis、健康检查、独立持久化卷
server/
  app.py                   # 应用工厂、客户端生命周期、CORS
  config.py                # 配置加载、校验与 SecretStr
  dependencies.py          # LLM、Redis 路由依赖
  cache.py                 # Redis 客户端
  llm.py                   # Anthropic 客户端与 Message、LLMTurn、ToolUse
  agent_loop.py            # 自研 while 循环和工具执行 trace
  tools.py                 # 工具定义、注册表、默认注册
  skill_runner.py          # 参数校验、工具执行、超时、错误回灌
  routes/health.py         # 存活和就绪探针
  store/
    _db.py                 # MySQL 连接池与 session 依赖
    base.py                # ORM 基类
migrations/                # Alembic 迁移环境及版本目录
skills/
  calculator/
    handler.py             # 计算器参数与实现
tests/
  conftest.py              # 测试环境隔离
  unit/                    # Loop、LLM、配置、健康检查、生命周期测试
scripts/run_agent.py       # 最小 Agent 命令行示例
.env.*.example             # 各环境配置填写示例
main.py                    # 保持兼容的应用入口
run_local.sh               # 本地启动
pyproject.toml
uv.lock
```

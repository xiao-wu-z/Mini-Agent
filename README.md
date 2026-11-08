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
# 创建业务表（首次安装或新增迁移后执行）
APP_ENV=development uv run alembic upgrade head
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
新增工具时添加对应 handler 并注册，不需要修改 Loop。当前工具如下：

| 工具 | 参数 | 行为 |
| --- | --- | --- |
| calculator | operation、left、right | 两数加减乘除，拒绝除零、非有限数值和任意代码 |
| search | query、limit | 搜索固定资料集；返回 `is_mock=true` 和 `mock://` 来源 |
| weather | city、day | 五座城市今天/明天的固定模拟天气，返回 `is_mock=true` |
| todo | action、title、todo_id、status、limit、offset | MySQL 持久化待办，按用户和会话隔离 |
| read_tool_result | ref、offset、limit | 分页读取当前会话的结果原文，默认 1024、最多 2048 字符 |

`weather` 支持北京、上海、杭州、深圳、广州，`day` 为 `today` / `tomorrow`。
Mock 数据用于验证工具闭环，不可作为实际天气预报或实时网络搜索结果。

Todo 的 `action` 支持 `create`、`list`、`complete`、`delete`。
新增时填写 `title`；完成和删除时，`todo_id` 必须来自之前的工具结果。
列表支持 `pending` / `completed` 过滤和 `limit` / `offset` 分页，返回 `has_more`。

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
# 两次不传 session-id 的调用各自创建新窗口，并打印会话 ID
uv run python -m scripts.run_agent --user-id user-a --session-title 天气 '查杭州明天天气，记待办带伞'
uv run python -m scripts.run_agent --user-id user-a --session-title 周报 '记待办：周五提交周报'
# 用第一次打印的真实会话 ID 接着聊
uv run python -m scripts.run_agent --user-id user-a --session-id '<第一次返回的ID>' '列出我的待办'
```

命令行身份参数仅用于本地演示，不构成认证。不传身份时使用本地演示用户。
默认通过会话服务自动读取和保存历史；使用 `--session-id` 恢复已有会话，归属不匹配返回错误。
只调试 Loop 时显式使用 `--stateless`，此模式不保存对话历史。

`echo` 模式仅用于检查入口，不会自主调用工具。测试中的 Mock 模型覆盖完整工具闭环，
不需要真实 Key，也不依赖 MySQL 或 Redis。

代码调用与追问：

```python
from server.agent_loop import AgentLoop
from server.tools import create_default_tools
from server.store.todos import TodoStore
from skills._shared.context import ToolContext

tools = create_default_tools(todo_store=TodoStore(database.session_factory))
loop = AgentLoop(llm, tools, max_rounds=8, tool_timeout=10)
context = ToolContext(user_id="user-a", session_id="window-1")
first = await loop.run("计算 128 乘以 36", context=context)
followup = await loop.run("再除以 2", history=first.messages, context=context)
```

全部 handler 接收 `(arguments, context)`。上下文中的用户和 Session ID 由调用方提供，
Loop 为执行补充 run_id / tool_use_id / operation_id，模型不能通过业务参数改变归属。
未提供身份或未注入数据库时，Todo 返回明确错误，不退化为内存待办。
Todo 创建使用稳定 operation_id 去重；直接调用 handler 未提供该标识时使用运行和工具调用标识。
这不代表同一自然语言请求跨任务自动去重。
在应用路由中可以通过 `Depends(server.dependencies.get_tools)` 获取已装配的工具集合。

直接调用 Loop 时通过 `history` 显式接续；HTTP 和默认 CLI 使用 ConversationService 自动持久化历史。
默认最多调用模型 8 轮；同一轮的多工具按顺序执行并集中回灌。工具执行超时、未知工具、
参数不合法及执行异常均转为 `is_error=true` 的结果。只读工具错误回灌模型修正；
写工具超时或普通执行异常按结果未知暂停，先核实副作用。
达到轮次上限返回 `max_rounds`；模型输出截断返回 `max_tokens`，不会执行截断响应中的工具。
LLM 网络错误、协议错误向调用方抛出；任务取消会继续传播，不会伪装成普通工具失败。

完整 assistant 内容块原样回灌，包括供应商返回的 thinking/signature，保证协议完整性；
对用户只返回 text 块。每次运行返回 `run_id`、`rounds`、`messages` 和 `tool_traces`，
日志记录工具名称、调用 ID、耗时及错误状态，不输出模型内部推理或工具参数。

## 会话与对话历史

### 请求预算、纠错与任务恢复

一个 Session 同时保留一个当前任务，检查点存于 `agent_session.task_state` JSON。
任务可以跨多次用户请求；每次请求重新分配预算，任务累计用量和错误记录继续保留。
已有未完成任务时，新任务请求返回 409，必须显式继续或取消，不能静默覆盖。

```dotenv
AGENT_REQUEST_MAX_TOKENS=64000
AGENT_REQUEST_MAX_OUTPUT_TOKENS=4096
AGENT_REQUEST_MAX_STEPS=8
AGENT_TOKEN_SAFETY_MARGIN=256
AGENT_AGENT_TOOL_TIMEOUT_SECONDS=10
AGENT_LLM_COUNT_TOKENS=true
```

请求总预算累计每次模型调用的输入和输出，含重复输入、缓存输入以及复盘调用。
调用前预留输入与输出预算，响应后按 usage 结算。优先使用供应商 count_tokens；
不支持计数接口时用 UTF-8 请求字节数保守估算，未知 usage 或中断调用保留预留消耗，
通过 `usage_estimated` 标识。供应商估算偏差无法提供绝对零超支保证；预算不足不再请求模型。
单次输出受请求剩余额度及 `AGENT_LLM_MAX_TOKENS` 同时限制。

同错按工具、动作、错误码和字段约束累计：第 2 次追加纠错提示，第 3 次调用当前模型
做一次结构化复盘，第 4 次仍失败则暂停。复盘与主决策共享 token、步骤和超时预算，
每个错误簇最多尝试一次；格式错误使用明确反馈，不追加隐藏模型调用。
复盘可以要求用户补充信息。同工具动作成功后关闭相应错误记录，无关工具成功不会清零。

运行状态包括 running、paused、waiting_user、completed、cancelled。
预算耗尽、轮次上限、输出截断和同错熔断均暂停任务，不表示任务完成。
普通模型 end_turn 表示本次任务执行结束，不提供独立的语义完成验证。

工具执行前保存稳定 operation_id 和 unknown 状态，执行后保存结果；已完成操作不重复执行。
Todo 创建用 operation_id 去重，完成操作可重复确认，删除恢复会明确区分“已不存在”与“本次删除”。
只有声明为 retry_safe 的工具可以恢复未知调用；其他写工具的未知结果需要先核实，不能直接重放。
模型失败、超时、用户断开或进程重启后可从检查点继续；持久化数据库不可用时不承诺最新步骤已保存。

```bash
# 创建任务（记录打印的 session_id）
uv run python -m scripts.run_agent '查杭州天气并记待办'
# 同一任务继续或补充信息
uv run python -m scripts.run_agent --session-id '<ID>' --continue-task '补充信息或继续'
# 显式取消后可以创建新任务
uv run python -m scripts.run_agent --session-id '<ID>' --cancel-task '取消'
```

HTTP 使用 `POST /sessions/{id}/chat`：新任务 body 为 `{"message":"目标"}`，
继续为 `{"action":"continue","message":"补充信息或继续"}`，取消为 `{"action":"cancel"}`。取消只停止后续执行，不撤销已经完成的工具操作。
通过 `GET /sessions/{id}/task` 查询目标、状态、用量和操作状态，不暴露原始推理或检查点消息。
若写操作结果为 unknown，先核实外部系统；仅在确认未执行后，继续请求可传
`"confirmed_not_executed":["<operation_id>"]` 明确允许重试该操作。
CLI 对应 `--continue-task --retry-operation '<operation_id>'`，不会自动重放结果未知的写操作。
成功任务的完整消息一次性提交到历史；暂停任务的未配对工具块只保存在检查点，恢复配对后再调用模型。
完成检查点与历史提交之间中断时，下次请求先补提交历史，不重复执行模型或工具。
当前仍按单 worker 部署，不提供跨进程调度或所有外部服务的 exactly-once 保证。

参考 mt-agent 的基本链路：校验会话归属 → 读取历史 → 组装 context → 执行 Loop → 保存整轮消息。

业务只使用两张会话相关表：`agent_session` 保存用户归属、标题和消息数量水位，
`agent_message` 保存有序消息，通过 `turn_id` 标识一次用户输入及其全部工具调用。
MySQL 保存完整历史，Redis 缓存历史读取结果；缓存丢失或不可用时回源 MySQL，聊天仍可继续。
缓存按消息数量水位区分版本，避免旧读取覆盖新历史。

### Context 压缩与结果引用

模型输入采用“开头原文 + 中段滚动摘要 + 最近原文”，当前任务的未完成工具交互不参与压缩。
首尾按安全消息单元保留：普通消息是一个单元，含多个 tool_use 的完整 assistant 与紧邻的
整批 tool_result 合为一个单元。两侧切点均校验 ID，无孤立结果、缺失结果或重复 ID；
保留区的 thinking/signature 原样回灌，摘要模型不接收这些内部推理字段。
当前任务目标被摘要覆盖时，会在工作视图中额外保留其用户原文。

每次主模型调用前计算 system、工具 Schema、摘要和消息的总输入量；预留输出与安全余量后，
达到软阈值可同步压缩，达到硬阈值必须压缩。首尾不足以提供安全中段、摘要失败或压缩无效时，
硬阈值下暂停，保留旧状态，不硬截参数或结果。软阈值下压缩失败可继续使用容量允许的旧视图。
摘要输入过长时按完整单元分批；每次合并旧摘要与新增中段，所有摘要调用共享请求预算、
模型请求次数和总超时。没有后台压缩或额外隐藏的收尾请求。

配置支持嵌套环境变量（双下划线），默认值如下：

```dotenv
AGENT_CONTEXT__WINDOW_TOKENS=32000
AGENT_CONTEXT__KEEP_HEAD_UNITS=2
AGENT_CONTEXT__KEEP_TAIL_UNITS=6
AGENT_CONTEXT__SOFT_RATIO=0.70
AGENT_CONTEXT__HARD_RATIO=0.85
AGENT_CONTEXT__SUMMARY_MAX_TOKENS=1024
AGENT_CONTEXT__MIN_INTERVAL_SECONDS=60
AGENT_TOOL_RESULT__INLINE_MAX_BYTES=8192
AGENT_TOOL_RESULT__PREVIEW_CHARS=256
AGENT_TOOL_RESULT__CACHE_TTL_SECONDS=3600
```

WINDOW_TOKENS 按实际部署模型设置，不由模型名猜测。默认输出预留为 4096 Token，
软硬比例基于扣除输出预留与安全余量后的输入容量。原 `AGENT_CONTEXT_RECENT_TURNS` 已移除。

大结果先完整保存到 `agent_tool_result`，然后工作视图中的 tool_result 保留原调用 ID、
错误标识，content 换成 ref、大小、明确标记不完整的预览和读取提示。
`read_tool_result(ref, offset, limit)` 按 Unicode 字符分页，返回 next_offset、has_more 与
original_is_error；有界读取不再被外置成新指针。原始参数与结果仍完整保存在消息历史中。

Redis 只缓存读取结果，丢失或不可用时回源 MySQL。ref 与用户、Session 绑定，不能跨窗口读取；
引用原文独立于当前 checkpoint，取消或新建任务不会使旧 ref 失效。
结果不存在时返回 NOT_FOUND，Loop 暂停等待用户处理，不继续让模型自动重跑原工具。
未配置持久化的 stateless 模式遇到大结果会暂停，
不会向模型发布失效引用。当前原文没有自动清理策略，不依赖 Redis TTL 维持恢复能力。

`agent_session.context_state` 保存摘要、覆盖边界与大结果视图；内部使用零起始右开区间，
查询任务时转换为原始消息的 sequence 覆盖范围，
已提交历史与未提交任务使用同一追加序列。`task_state` 保存完整原文和当前任务独立原文，
生成模型工作视图不会修改它们。任务完成时，原始消息、摘要状态和 history_saved 原子提交。
checkpoint 元信息包括 id、version、phase、updated_at，只保留最新恢复点；恢复原样沿用摘要、
操作 ID 和失败记录。进程在大结果落库后、检查点更新前退出时，可按稳定操作 ID 找回结果，
无需重新执行工具。原文尚未落库的未知写操作仍需核实。

成功的一轮消息在短事务中一次性保存，未配对工具块只存在于任务检查点。
完整原始历史不删除，不依赖向量库；语义摘要可能丢失细节，不能代替工具执行状态或精确原文。
同窗口在单应用进程内用 asyncio.Lock 避免并发；基础版本使用单 worker 部署。
此锁不提供跨进程互斥，不宣称写工具具有跨进程 exactly-once 保证。

| 方法 | 路径 | 功能 |
| --- | --- | --- |
| POST | /sessions | 创建会话，body 为 `{"title":"天气"}` |
| GET | /sessions | 当前用户的会话列表，支持 limit/offset |
| GET | /sessions/{id} | 校验归属并读取会话 |
| GET | /sessions/{id}/messages | 历史分页，支持 after/limit |
| POST | /sessions/{id}/chat | SSE 流式聊天，body 为 `{"message":"继续"}` |

开发环境通过 X-User-ID 指定身份；这是可信调用方协议，不是登录认证。
生产入口由可信网关注入身份并提供 AGENT_API_TOKEN 对应的 Bearer Token。
公开历史过滤 thinking/signature，内部模型回灌保留完整协议块。

```bash
curl -X POST http://127.0.0.1:8000/sessions \
  -H 'X-User-ID: user-a' -H 'Content-Type: application/json' \
  -d '{"title":"天气"}'
curl -N -X POST 'http://127.0.0.1:8000/sessions/<返回的ID>/chat' \
  -H 'X-User-ID: user-a' -H 'Content-Type: application/json' \
  -d '{"message":"查杭州天气并记待办带伞"}'
```

Chat 返回 `Content-Type: text/event-stream`，使用 POST + fetch 的流读取或 curl -N；
浏览器原生 EventSource 不支持这个带 JSON body 的 POST 接口。事件格式为 `event: 名称` 加
`data: JSON`，使用空行分帧：

| 事件 | 内容 |
| --- | --- |
| start | session_id、trace_id |
| text_delta | 主模型公开文本增量 text、当前模型调用轮次 round |
| tool_use | 工具名、tool_use_id、running 状态与 mark |
| tool_result | 相同 tool_use_id、结果状态、耗时、是否复用与 mark |
| heartbeat | 空对象，默认每 15 秒空闲发送一次，可配置 AGENT_SSE_HEARTBEAT_SECONDS |
| done | 原 ChatResponse 的完整字段，包含最终 text、task_status、用量和 trace_id |
| error | 稳定 code 与可公开 message，作为失败终止事件，不再发送 done |

工具标记参考 mt-agent，前端按 tool_use_id 更新同一张卡，安全渲染 mark：

```text
event: tool_use
data: {"name":"calculator","tool_use_id":"call_1","status":"running","mark":"[工具调用中 · calculator]"}

event: tool_result
data: {"name":"calculator","tool_use_id":"call_1","status":"success","is_error":false,"duration_ms":1.2,"reused":false,"mark":"[tool=calculator · 工具 OK]"}
```

失败标记为“工具失败”，未知写结果标记为“结果待确认”，不能显示成确定成功或失败。
恢复命中已有结果时只推送结果事件，不伪造一次新的工具调用。mark 只用于展示，不写入原始消息，
也不回灌给模型；参数、内部推理和供应商签名不通过工具事件下发。

真实模型请求使用 Anthropic 流式协议：text_delta 实时转发，同时收集完整的内容块、工具 JSON、
thinking/signature 与累计 usage；整轮结束并校验后才执行工具。摘要和 Reflexion 文本不对外推送。
流中可能包含工具调用前的说明，客户端应以 done.text 为最终答案，不要再次追加造成重复。
已推送文本在收到 done 前均为未确认完整的输出；暂停也返回 done，请检查 task_status 和 stop_reason。

鉴权、参数和会话归属错误在建立 SSE 前返回常规 HTTP 错误；窗口忙碌、任务冲突和运行异常等
建立流之后的错误通过 error 事件返回。响应关闭会取消执行并等待 checkpoint、Trace 收尾；
可通过查询任务并显式 continue 恢复。没有基于 Last-Event-ID 的自动重放或自动重试写操作。
事件队列有界并提供反压，响应设置 no-cache 与 X-Accel-Buffering: no。

0003 迁移将旧消息的 run_id 改为 turn_id，保留原消息，将旧运行表改名为
agent_runs_archive 仅供审计，不参与当前代码。旧迁移与 SQL 留痕保留不变。
因为新对话不再创建运行记录，恢复旧外键模型需要人工数据映射，0003 不提供自动 downgrade。

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

当前首个迁移创建 `todos` 表；所有查询和写操作均限定 user_id / session_id。
第二个迁移是原运行模型；第三个迁移收敛为基础两表并保留旧运行记录归档。当前结构统一保存在
[`migrations/sql/schema.sql`](migrations/sql/schema.sql)，历史变更由 Git 和 Alembic revision 留痕。
SQL 文件用于审查留痕，常规升级只执行 Alembic，不重复执行 SQL 快照。
业务账号和 Session ID 使用区分大小写的表排序规则，避免不同身份被视为相同。
测试、生产迁移使用各自的 `APP_ENV`，提前核对目标库；测试环境需显式开启
`AGENT_MYSQL_ENABLED=true`。

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

常规测试包含临时 SQLite 上的工具持久化测试，不连接真实 MySQL。
真实 MySQL 集成测试默认跳过；需要验证时先启动测试容器并迁移专用测试库：

```bash
docker compose --env-file .env.testing up -d --wait
APP_ENV=testing AGENT_MYSQL_ENABLED=true uv run alembic upgrade head
# 替换 TEST_PASSWORD；密码含 URL 保留字符时需要 URL 编码。
MINI_AGENT_TEST_MYSQL_URL='mysql+asyncmy://mini_agent_test:TEST_PASSWORD@127.0.0.1:3307/mini_agent_test?charset=utf8mb4' \
  uv run pytest -q tests/integration/test_todo_mysql.py
# 同时验证会话与 Redis；替换两种测试密码。
MINI_AGENT_TEST_MYSQL_URL='mysql+asyncmy://mini_agent_test:TEST_PASSWORD@127.0.0.1:3307/mini_agent_test?charset=utf8mb4' \
MINI_AGENT_TEST_REDIS_URL='redis://:TEST_REDIS_PASSWORD@127.0.0.1:6380/1' \
  uv run pytest -q tests/integration
```

集成测试只接受以 `_test` 结尾的数据库名，使用本次生成的独立用户标识并清理自己的数据。

添加依赖使用 `uv add 包名`，开发依赖使用 `uv add --dev 包名`。
提交 `pyproject.toml` 与 `uv.lock`，以便复现相同依赖版本。

## Trace 与执行步骤查询

使用自研 `TraceRecorder` 显式埋点，不依赖 Langfuse SDK、Agent 框架或全局可变上下文。
`agent_trace` 保存一次已通过会话归属校验并取得窗口锁的聊天请求，`agent_span` 保存步骤。
鉴权失败和窗口忙碌的请求不会创建 Trace。新任务、继续和取消分别创建独立 trace_id；
继续请求沿用 task_id，并记录请求开始时使用的 checkpoint_id。聊天响应返回 trace_id，CLI 同时打印。

步骤按 parent_span_id 组织，type 为 span、generation 或 event：

```text
Trace（一次请求）
├── context.prepare
│   ├── generation.summary（发生压缩时）
│   └── context.compressed（压缩前后输入量、摘要版本和覆盖边界）
├── loop.round
│   ├── generation.decision
│   └── tool.calculator / tool.todo 等
├── generation.reflection（需要复盘时）
└── history.commit / task.paused / task.completed 等
```

轮次 Span 在其工具批次处理完后关闭。恢复时使用 loop.resume；命中已保存结果只记录
`tool.result_reused` 事件，不新增实际工具执行 Span，也不重复追加旧的 ToolTrace。
工具参数校验、超时和业务错误记录稳定错误码；工具失败后模型成功修正，整次 Trace 仍可成功。
预算耗尽或等待补充信息分别记录 paused / waiting_user，普通执行错误为 error，取消为 cancelled。
未收尾记录保留 running 与 incomplete=true，表示可能仍在执行或进程已中断，不伪造完成状态。

每个 generation 记录普通输入、输出、缓存创建和缓存读取 Token，用量只向请求汇总一次，
父 Span 不重复汇总。缺少 usage 或模型请求中断时记录预留值并标记 usage_estimated。
继续请求仅统计本次新增调用，完成答案复用和取消请求的模型消耗为零。
开始/结束时间使用 UTC，耗时使用单调时钟，不依赖系统时钟差值。

采集参数：

```dotenv
AGENT_TRACING__ENABLED=true
AGENT_TRACING__WRITE_TIMEOUT_SECONDS=0.25
AGENT_TRACING__PREVIEW_CHARS=1024
AGENT_TRACING__MAX_SPANS=512
```

开始和结束分别使用独立短事务，写入失败不回滚业务、不触发工具重放。
本请求首次写入失败或超时后，后续写入直接降级，下个请求重新尝试；没有后台队列或自动重试。
禁用采集不影响读取历史 Trace。采集数量超过上限时增加 dropped_spans，请求模型用量仍继续累计。
默认只存有界脱敏摘要，过滤已知凭据字段、常见凭据表达式和 thinking/signature 内容块。
模型输入记录消息数、工具名称与预算概况；大工具结果只记录体积，持久化成功后以事件关联 ref。
这些摘要用于排障，不是完整 Prompt 快照，无法保证重放；原始结果仍由原有结果存储管理。
埋点数据当前没有自动清理策略。

查询接口复用现有网关令牌和 X-User-ID 身份，必须拥有对应窗口：

| 方法 | 路径 | 功能 |
| --- | --- | --- |
| GET | /sessions/{id}/traces | 按开始时间倒序分页；支持 task_id、limit、offset |
| GET | /sessions/{id}/traces/{trace_id} | 返回 trace、按 sequence 排序的 spans 和嵌套 tree |

详情查询最多读取 1000 个步骤，超过时返回 truncated=true；正常采集上限不超过此数量。
其他窗口或用户的 Trace 返回 404，查询不修改运行记录，也不会恢复或重放任务。

## 目录

```text
config/                    # 开发、测试、生产公开默认配置
docker-compose.yml         # MySQL / Redis、健康检查、独立持久化卷
server/
  app.py                   # 应用工厂、客户端生命周期、CORS
  conversations.py         # 读取历史、调用 Loop、保存整轮消息
  config.py                # 配置加载、校验与 SecretStr
  dependencies.py          # LLM、Redis 路由依赖
  context.py               # 工具安全单元、首尾视图与滚动摘要边界
  cache.py                 # Redis 历史缓存和客户端
  llm.py                   # Anthropic 客户端与 Message、LLMTurn、ToolUse
  agent_loop.py            # 自研 while 循环和显式步骤埋点
  tracing.py               # 请求采集器、有界脱敏和故障隔离
  sse.py                   # SSE 帧、工具 marker、心跳与断连收尾
  tools.py                 # 五个工具定义、注册表、默认注册
  skill_runner.py          # 参数校验、工具执行、超时、错误回灌
  routes/health.py         # 存活和就绪探针
  store/
    _db.py                 # MySQL 连接池与 session 依赖
    base.py                # ORM 基类
    todos.py               # 待办模型与作用域隔离的持久化访问
    sessions.py            # 会话、原始消息与摘要状态的短事务访问
    tool_results.py        # 独立结果原文和 Redis 读取缓存
    traces.py              # Trace/Span 实体、短事务及步骤树查询
migrations/                # Alembic 迁移环境及版本目录
skills/
  _shared/                 # 可信工具上下文和业务错误
  calculator/
    handler.py             # 计算器参数与实现
  search/handler.py        # Mock 资料搜索
  todo/handler.py          # 待办增删改查
  weather/handler.py       # Mock 天气
  read_tool_result/handler.py # 结果引用的有界分页读取
tests/
  conftest.py              # 测试环境隔离
  unit/                    # Loop、LLM、配置、健康检查、生命周期测试
  integration/             # 显式启用的 MySQL 待办持久化验证
scripts/run_agent.py       # 最小 Agent 命令行示例
.env.*.example             # 各环境配置填写示例
main.py                    # 保持兼容的应用入口
run_local.sh               # 本地启动
pyproject.toml
uv.lock
```

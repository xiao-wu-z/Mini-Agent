-- Mini Agent current schema (MySQL 8.4)
-- Existing databases must use Alembic migrations; legacy archives are excluded.

CREATE TABLE agent_session (
    id VARCHAR(32) NOT NULL,
    user_id VARCHAR(64) NOT NULL,
    title VARCHAR(200) NOT NULL,
    version INTEGER NOT NULL,
    task_state JSON,
    context_state JSON,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    CONSTRAINT pk_agent_session PRIMARY KEY (id)
)CHARSET=utf8mb4 COLLATE utf8mb4_bin;

CREATE INDEX ix_agent_sessions_user_updated ON agent_session (user_id, updated_at, id);

CREATE TABLE todos (
    id VARCHAR(32) NOT NULL,
    user_id VARCHAR(64) NOT NULL,
    session_id VARCHAR(64) NOT NULL,
    title VARCHAR(500) NOT NULL,
    status VARCHAR(16) NOT NULL,
    create_key VARCHAR(64) NOT NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    CONSTRAINT pk_todos PRIMARY KEY (id),
    CONSTRAINT uq_todos_scope_create_key UNIQUE (user_id, session_id, create_key)
)CHARSET=utf8mb4 COLLATE utf8mb4_bin;

CREATE INDEX ix_todos_scope_created ON todos (user_id, session_id, created_at, id);

CREATE TABLE agent_message (
    id VARCHAR(32) NOT NULL,
    session_id VARCHAR(32) NOT NULL,
    turn_id VARCHAR(32) NOT NULL,
    sequence INTEGER NOT NULL,
    `role` VARCHAR(16) NOT NULL,
    content JSON NOT NULL,
    created_at DATETIME NOT NULL,
    CONSTRAINT pk_agent_message PRIMARY KEY (id),
    CONSTRAINT uq_conversation_messages_sequence UNIQUE (session_id, sequence),
    CONSTRAINT fk_conversation_messages_session_id_agent_sessions FOREIGN KEY(session_id) REFERENCES agent_session (id)
)CHARSET=utf8mb4 COLLATE utf8mb4_bin;

CREATE INDEX ix_conversation_messages_turn ON agent_message (turn_id);

CREATE TABLE agent_tool_result (
    id VARCHAR(32) NOT NULL,
    user_id VARCHAR(64) NOT NULL,
    session_id VARCHAR(32) NOT NULL,
    result JSON NOT NULL,
    CONSTRAINT pk_agent_tool_result PRIMARY KEY (id),
    CONSTRAINT fk_agent_tool_result_session_id_agent_session FOREIGN KEY(session_id) REFERENCES agent_session (id)
)CHARSET=utf8mb4 COLLATE utf8mb4_bin;

CREATE INDEX ix_agent_tool_result_scope ON agent_tool_result (user_id, session_id);

CREATE TABLE agent_trace (
    id VARCHAR(32) NOT NULL,
    user_id VARCHAR(64) NOT NULL,
    session_id VARCHAR(32) NOT NULL,
    task_id VARCHAR(32),
    checkpoint_id VARCHAR(32),
    action VARCHAR(32) NOT NULL,
    status VARCHAR(32) NOT NULL,
    stop_reason VARCHAR(100),
    started_at DATETIME NOT NULL,
    ended_at DATETIME,
    duration_ms FLOAT,
    input_summary TEXT,
    output_summary TEXT,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cache_creation_input_tokens INTEGER NOT NULL,
    cache_read_input_tokens INTEGER NOT NULL,
    request_tokens INTEGER NOT NULL,
    usage_estimated BOOL NOT NULL,
    dropped_spans INTEGER NOT NULL,
    CONSTRAINT pk_agent_trace PRIMARY KEY (id),
    CONSTRAINT fk_agent_trace_session_id_agent_session FOREIGN KEY(session_id) REFERENCES agent_session (id)
)CHARSET=utf8mb4 COLLATE utf8mb4_bin;

CREATE INDEX ix_agent_trace_scope_started ON agent_trace (user_id, session_id, started_at, id);

CREATE INDEX ix_agent_trace_task_started ON agent_trace (task_id, started_at, id);

CREATE TABLE agent_span (
    id VARCHAR(32) NOT NULL,
    trace_id VARCHAR(32) NOT NULL,
    parent_span_id VARCHAR(32),
    sequence INTEGER NOT NULL,
    type VARCHAR(32) NOT NULL,
    name VARCHAR(100) NOT NULL,
    status VARCHAR(32) NOT NULL,
    started_at DATETIME NOT NULL,
    ended_at DATETIME,
    duration_ms FLOAT,
    input_summary TEXT,
    output_summary TEXT,
    error_code VARCHAR(100),
    stop_reason VARCHAR(100),
    model VARCHAR(200),
    tool_use_id VARCHAR(200),
    operation_id VARCHAR(32),
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cache_creation_input_tokens INTEGER NOT NULL,
    cache_read_input_tokens INTEGER NOT NULL,
    usage_estimated BOOL NOT NULL,
    attributes JSON NOT NULL,
    CONSTRAINT pk_agent_span PRIMARY KEY (id),
    CONSTRAINT uq_agent_span_trace_sequence UNIQUE (trace_id, sequence),
    CONSTRAINT fk_agent_span_trace_id_agent_trace FOREIGN KEY(trace_id) REFERENCES agent_trace (id)
)CHARSET=utf8mb4 COLLATE utf8mb4_bin;

CREATE INDEX ix_agent_span_parent ON agent_span (trace_id, parent_span_id);

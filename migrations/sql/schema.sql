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

-- AgentWorkOS schema v1. SQLite (WAL). All timestamps are ISO-8601 UTC strings ("2026-11-05T10:00:00Z").
-- JSON columns hold TEXT produced by json.dumps.

CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);

-- Raw observations produced by sensors. Deduplicated by fingerprint.
CREATE TABLE IF NOT EXISTS signals (
    id            TEXT PRIMARY KEY,
    source        TEXT NOT NULL,              -- sensor name: git, files, agent_history, gmail, notion, zotero, manual ...
    external_id   TEXT,                       -- id inside the source system
    fingerprint   TEXT NOT NULL UNIQUE,       -- dedupe key; change it when the underlying thing materially changes
    title         TEXT NOT NULL,
    body          TEXT,                       -- bounded excerpt (<= 4000 chars), already redacted
    url           TEXT,
    occurred_at   TEXT,
    observed_at   TEXT NOT NULL,
    payload       TEXT,                       -- json
    status        TEXT NOT NULL DEFAULT 'new',-- new | triaged | ignored | error
    task_id       TEXT,
    triage_note   TEXT
);
CREATE INDEX IF NOT EXISTS ix_signals_status ON signals(status, observed_at);

-- Unified task center. Inbox items, sub-tasks and decisions are all tasks.
CREATE TABLE IF NOT EXISTS tasks (
    id              TEXT PRIMARY KEY,
    parent_id       TEXT REFERENCES tasks(id),
    root_id         TEXT,
    title           TEXT NOT NULL,
    description     TEXT,
    kind            TEXT NOT NULL DEFAULT 'todo',      -- todo | issue | opportunity | decision | info
    category        TEXT NOT NULL DEFAULT 'other',     -- see models.CATEGORIES
    priority        INTEGER NOT NULL DEFAULT 2,        -- 0 (P0, urgent) .. 3 (P3, someday)
    level           INTEGER NOT NULL DEFAULT 1,        -- assessed autonomy level 1..5
    level_cap       INTEGER,                           -- policy cap (effective = min(level, level_cap))
    level_reason    TEXT,
    status          TEXT NOT NULL DEFAULT 'inbox',     -- see models.TaskStatus
    status_reason   TEXT,
    executor        TEXT,                              -- codex | claude | pi | dsh | internal ; NULL = router decides
    project         TEXT,
    due_at          TEXT,
    source          TEXT,                              -- manual | sensor:<name> | subtask | system
    source_signal_id TEXT,
    dedupe_key      TEXT UNIQUE,
    acceptance      TEXT,                              -- acceptance criteria
    plan            TEXT,                              -- json
    result          TEXT,                              -- json (last structured result)
    summary         TEXT,                              -- human readable outcome / briefing
    attempts        INTEGER NOT NULL DEFAULT 0,
    max_attempts    INTEGER NOT NULL DEFAULT 3,
    next_attempt_at TEXT,
    snoozed_until   TEXT,
    lease_owner     TEXT,
    lease_expires_at TEXT,
    workspace       TEXT,
    cost_usd        REAL NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    started_at      TEXT,
    finished_at     TEXT,
    meta            TEXT                               -- json: free-form (tags, links, decision history...)
);
CREATE INDEX IF NOT EXISTS ix_tasks_status ON tasks(status, priority, created_at);
CREATE INDEX IF NOT EXISTS ix_tasks_parent ON tasks(parent_id);

CREATE TABLE IF NOT EXISTS task_deps (
    task_id     TEXT NOT NULL REFERENCES tasks(id),
    depends_on  TEXT NOT NULL REFERENCES tasks(id),
    PRIMARY KEY (task_id, depends_on)
);

-- One execution attempt of an agent / LLM call (triage, plan, execute, verify, sensor, context, action).
CREATE TABLE IF NOT EXISTS runs (
    id            TEXT PRIMARY KEY,
    task_id       TEXT,
    purpose       TEXT NOT NULL,              -- triage | plan | execute | verify | sensor | context | action | digest
    attempt       INTEGER NOT NULL DEFAULT 1,
    executor      TEXT NOT NULL,
    model         TEXT,
    status        TEXT NOT NULL DEFAULT 'queued', -- queued | running | succeeded | failed | timeout | cancelled | interrupted
    pid           INTEGER,
    cwd           TEXT,
    command       TEXT,                       -- json argv (no secrets)
    prompt_path   TEXT,
    log_path      TEXT,
    started_at    TEXT,
    heartbeat_at  TEXT,
    ended_at      TEXT,
    exit_code     INTEGER,
    cost_usd      REAL NOT NULL DEFAULT 0,
    tokens_in     INTEGER NOT NULL DEFAULT 0,
    tokens_out    INTEGER NOT NULL DEFAULT 0,
    final_text    TEXT,
    structured    TEXT,                       -- json
    error         TEXT,
    error_class   TEXT                        -- transient | permanent | policy | budget | cancelled
);
CREATE INDEX IF NOT EXISTS ix_runs_task ON runs(task_id, started_at);
CREATE INDEX IF NOT EXISTS ix_runs_status ON runs(status);

-- Fine grained run events: messages, tool calls, tool results, stderr lines, errors.
CREATE TABLE IF NOT EXISTS run_events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id  TEXT NOT NULL,
    ts      TEXT NOT NULL,
    kind    TEXT NOT NULL,                    -- message | tool_call | tool_result | reasoning | stderr | error | status | usage
    name    TEXT,
    data    TEXT                              -- json, truncated to ~8KB
);
CREATE INDEX IF NOT EXISTS ix_run_events_run ON run_events(run_id, id);

-- HITL Outbox: actions / decisions that need the human.
CREATE TABLE IF NOT EXISTS approvals (
    id              TEXT PRIMARY KEY,
    task_id         TEXT,
    run_id          TEXT,
    kind            TEXT NOT NULL,            -- action | decision | review | permission | budget
    action_type     TEXT,                     -- send_email | post_message | publish | git_push | git_commit | open_mr | apply_to_project | calendar_write | notion_write | delete | spend | other
    title           TEXT NOT NULL,
    context_md      TEXT,                     -- background the human needs
    recommendation  TEXT,                     -- AI recommendation (option key + why)
    options         TEXT NOT NULL,            -- json [{key,label,description}]
    risk            TEXT NOT NULL DEFAULT 'medium', -- low | medium | high
    payload         TEXT,                     -- json: exactly what will be executed if approved
    status          TEXT NOT NULL DEFAULT 'pending', -- pending | approved | rejected | expired | cancelled | executing | executed | failed
    decision        TEXT,                     -- chosen option key
    decision_note   TEXT,                     -- free text from the human (edits, instructions)
    decided_via     TEXT,                     -- dashboard | cli | link | auto
    default_on_expiry TEXT,                   -- option key applied on expiry (NULL = just expire)
    token_hash      TEXT,
    created_at      TEXT NOT NULL,
    expires_at      TEXT,
    decided_at      TEXT,
    executed_at     TEXT,
    result          TEXT,                     -- json
    notified_at     TEXT
);
CREATE INDEX IF NOT EXISTS ix_approvals_status ON approvals(status, created_at);

CREATE TABLE IF NOT EXISTS artifacts (
    id          TEXT PRIMARY KEY,
    task_id     TEXT,
    run_id      TEXT,
    kind        TEXT NOT NULL,                -- report | code | draft | data | patch | link | file
    title       TEXT NOT NULL,
    path        TEXT,                         -- absolute local path
    uri         TEXT,
    sha256      TEXT,
    bytes       INTEGER,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_artifacts_task ON artifacts(task_id);

CREATE TABLE IF NOT EXISTS notifications (
    id          TEXT PRIMARY KEY,
    channel     TEXT NOT NULL,
    ref_type    TEXT,
    ref_id      TEXT,
    title       TEXT NOT NULL,
    body        TEXT,
    status      TEXT NOT NULL DEFAULT 'pending', -- pending | sent | failed | suppressed
    error       TEXT,
    created_at  TEXT NOT NULL,
    sent_at     TEXT
);

-- Append-only audit / history log for every state transition and human intervention.
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    entity_type TEXT NOT NULL,                -- task | run | approval | signal | sensor | context | system
    entity_id   TEXT,
    type        TEXT NOT NULL,                -- e.g. status_changed, created, retried, approved
    actor       TEXT NOT NULL DEFAULT 'system',-- system | user | agent:<executor> | sensor:<name>
    data        TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_entity ON events(entity_type, entity_id, id);

CREATE TABLE IF NOT EXISTS sensor_state (
    name                  TEXT PRIMARY KEY,
    enabled               INTEGER NOT NULL DEFAULT 1,
    last_run_at           TEXT,
    last_ok_at            TEXT,
    next_run_at           TEXT,
    cursor                TEXT,               -- json: sensor-specific watermark
    last_error            TEXT,
    consecutive_failures  INTEGER NOT NULL DEFAULT 0,
    last_count            INTEGER NOT NULL DEFAULT 0
);

-- Personal context knowledge base.
CREATE TABLE IF NOT EXISTS context_facts (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,                -- role | project | goal | interest | habit | preference | person | deadline | note
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    source      TEXT,
    confidence  REAL NOT NULL DEFAULT 0.6,
    status      TEXT NOT NULL DEFAULT 'active', -- active | proposed | stale | rejected
    pinned      INTEGER NOT NULL DEFAULT 0,   -- user-confirmed facts are never overwritten by agents
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE(kind, key)
);

CREATE TABLE IF NOT EXISTS cost_ledger (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    day         TEXT NOT NULL,                -- local date YYYY-MM-DD
    run_id      TEXT,
    executor    TEXT NOT NULL,
    model       TEXT,
    cost_usd    REAL NOT NULL DEFAULT 0,
    tokens_in   INTEGER NOT NULL DEFAULT 0,
    tokens_out  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_cost_day ON cost_ledger(day, executor);

CREATE TABLE IF NOT EXISTS kv (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    updated_at  TEXT NOT NULL
);

-- Human-readable digest of tasks / approvals shown in the inbox and board: one-line headline, one-line gist and
-- 3-5 keywords. Written by triage (at creation) or by the cheap digest job; `basis` is a hash of the text it was
-- derived from so it is regenerated only when the underlying content materially changes.
CREATE TABLE IF NOT EXISTS digests (
    entity_type TEXT NOT NULL,                -- task | approval
    entity_id   TEXT NOT NULL,
    headline    TEXT NOT NULL,
    gist        TEXT,
    keywords    TEXT NOT NULL DEFAULT '[]',   -- json list
    basis       TEXT,
    source      TEXT NOT NULL DEFAULT 'llm',  -- triage | llm | user
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (entity_type, entity_id)
);

-- Topics: the things the user actually cares about (a repo, a paper, a job application, ...). Several tasks and their
-- approvals belong to one topic so the inbox can show them together with ONE recap instead of N separate threads.
CREATE TABLE IF NOT EXISTS topics (
    key              TEXT PRIMARY KEY,         -- stable slug (llm-chosen) or p:<project> / c:<category> fallback
    title            TEXT NOT NULL,
    brief            TEXT,                     -- json {state_now, recap:[{ts,text}], asks:[{id,question,why_now,stakes}]}
    brief_basis      TEXT,                     -- hash of the open items/statuses the brief was written for
    brief_updated_at TEXT,
    source           TEXT NOT NULL DEFAULT 'llm',   -- llm | fallback | user
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS topic_items (
    entity_type TEXT NOT NULL,                -- task (approvals follow their task)
    entity_id   TEXT NOT NULL,
    topic_key   TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (entity_type, entity_id)
);
CREATE INDEX IF NOT EXISTS ix_topic_items_key ON topic_items(topic_key);

-- Which project (a canonical context fact of kind 'project', identified by its key = project name) a topic belongs to.
-- source 'user' links are never changed by the AI.
CREATE TABLE IF NOT EXISTS topic_projects (
    topic_key   TEXT PRIMARY KEY,
    project_key TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT 'llm',   -- llm | rule | user
    updated_at  TEXT NOT NULL
);

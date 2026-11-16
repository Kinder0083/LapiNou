-- SPDX-License-Identifier: MIT
-- Schéma initial du serveur LapiNou.

CREATE TABLE rabbits (
    id                 TEXT PRIMARY KEY,
    name               TEXT NOT NULL DEFAULT '',
    default_lms_player TEXT NOT NULL DEFAULT '',
    last_seen          TEXT
);

CREATE TABLE conversations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    rabbit_id  TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at   TEXT
);
CREATE INDEX idx_conversations_started ON conversations(started_at);

CREATE TABLE turns (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role            TEXT NOT NULL,
    text            TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE INDEX idx_turns_conversation ON turns(conversation_id);

CREATE TABLE memories (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    text       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    source     TEXT NOT NULL DEFAULT ''
);

CREATE TABLE timers (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    rabbit_id  TEXT NOT NULL,
    due_at     TEXT NOT NULL,
    text       TEXT NOT NULL DEFAULT '',
    recurrence TEXT
);
CREATE INDEX idx_timers_due ON timers(due_at);

CREATE TABLE usage (
    month         TEXT NOT NULL,
    provider      TEXT NOT NULL,
    model         TEXT NOT NULL,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    usd           REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (month, provider, model)
);

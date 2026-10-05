"""SQLite 持久化层。

设计要点：
* 文档按 (doc_id, revision) 追加保存，重新导入生成新版本，旧版本永不修改；
* report 表在数据库层用触发器禁止 UPDATE/DELETE，保证报告不可变；
* 每次求共识时把输入（文档、词元、标签规则、标注者、标注）整体快照，
  之后改规则或重新导入都不会影响既有共识/报告。
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

DB_PATH = os.environ.get("ADJ_DB_PATH", "/data/adjudication.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS document_revisions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects(id),
    doc_id      TEXT NOT NULL,
    revision    INTEGER NOT NULL,
    content     TEXT NOT NULL,
    token_bounds TEXT NOT NULL,   -- JSON [[start,end],...]
    char_count  INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    UNIQUE(project_id, doc_id, revision)
);

CREATE TABLE IF NOT EXISTS labels (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    name       TEXT NOT NULL,
    color      TEXT NOT NULL DEFAULT '#6366f1',
    created_at TEXT NOT NULL,
    UNIQUE(project_id, name)
);

CREATE TABLE IF NOT EXISTS label_nesting (
    project_id      INTEGER NOT NULL REFERENCES projects(id),
    outer_label_id  INTEGER NOT NULL REFERENCES labels(id),
    inner_label_id  INTEGER NOT NULL REFERENCES labels(id),
    PRIMARY KEY (outer_label_id, inner_label_id)
);

CREATE TABLE IF NOT EXISTS label_mutex (
    project_id INTEGER NOT NULL REFERENCES projects(id),
    label_a_id INTEGER NOT NULL REFERENCES labels(id),
    label_b_id INTEGER NOT NULL REFERENCES labels(id),
    PRIMARY KEY (label_a_id, label_b_id)
);

CREATE TABLE IF NOT EXISTS annotators (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    name       TEXT NOT NULL,
    weight     INTEGER NOT NULL CHECK(weight BETWEEN 1 AND 10),
    created_at TEXT NOT NULL,
    UNIQUE(project_id, name)
);

CREATE TABLE IF NOT EXISTS annotations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id   INTEGER NOT NULL REFERENCES projects(id),
    doc_id       TEXT NOT NULL,
    revision     INTEGER NOT NULL,
    annotator_id INTEGER NOT NULL REFERENCES annotators(id),
    label_id     INTEGER NOT NULL REFERENCES labels(id),
    start        INTEGER NOT NULL,
    end          INTEGER NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS consensuses (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id       INTEGER NOT NULL REFERENCES projects(id),
    doc_id           TEXT NOT NULL,
    revision         INTEGER NOT NULL,
    algorithm_name   TEXT NOT NULL,
    algorithm_version TEXT NOT NULL,
    solver_status    TEXT NOT NULL,
    total_score      INTEGER NOT NULL,
    raw_total_score  REAL NOT NULL,
    params_json      TEXT NOT NULL,
    snapshot_json    TEXT NOT NULL,
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS consensus_items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    consensus_id INTEGER NOT NULL REFERENCES consensuses(id),
    candidate_idx INTEGER NOT NULL,
    start        INTEGER NOT NULL,
    end          INTEGER NOT NULL,
    label_id     INTEGER NOT NULL,
    label_name   TEXT NOT NULL,
    score        INTEGER NOT NULL,
    raw_score    REAL NOT NULL,
    adopted      INTEGER NOT NULL,
    forced       INTEGER NOT NULL DEFAULT 0,
    supporters_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS overrides (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    consensus_id INTEGER NOT NULL REFERENCES consensuses(id),
    action       TEXT NOT NULL CHECK(action IN ('select','deselect')),
    start        INTEGER NOT NULL,
    end          INTEGER NOT NULL,
    label_id     INTEGER NOT NULL,
    reason       TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reports (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id   INTEGER NOT NULL REFERENCES projects(id),
    consensus_id INTEGER NOT NULL REFERENCES consensuses(id),
    version      TEXT NOT NULL UNIQUE,
    content_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

-- 报告不可变：数据库层拒绝任何改写/删除
CREATE TRIGGER IF NOT EXISTS trg_reports_no_update
BEFORE UPDATE ON reports
BEGIN
    SELECT RAISE(ABORT, 'reports are immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_reports_no_delete
BEFORE DELETE ON reports
BEGIN
    SELECT RAISE(ABORT, 'reports are immutable');
END;

-- 文档修订同样只追加
CREATE TRIGGER IF NOT EXISTS trg_doc_rev_no_update
BEFORE UPDATE ON document_revisions
BEGIN
    SELECT RAISE(ABORT, 'document revisions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS trg_doc_rev_no_delete
BEFORE DELETE ON document_revisions
BEGIN
    SELECT RAISE(ABORT, 'document revisions are append-only');
END;
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_db_path() -> str:
    return DB_PATH


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)

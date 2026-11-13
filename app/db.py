"""SQLite 连接与建表。

表结构：
- tests           试验元信息
- samples         原始毫秒采样（rowid 即严格递增的采样序号）
- blocks          多级摘要块，level 1 为最细索引块，level 越大粒度越粗
- pending_repairs 摘要待修登记：档案员发起重建前先落库，
                  服务重启或重建失败都保留，重建成功才在同一事务里解除
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import config

_SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS tests (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL UNIQUE,
    source_filename TEXT    NOT NULL,
    sample_count    INTEGER NOT NULL,
    start_ts        INTEGER NOT NULL,
    end_ts          INTEGER NOT NULL,
    max_level       INTEGER NOT NULL,
    imported_at     TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS samples (
    test_id    INTEGER NOT NULL,
    ts         INTEGER NOT NULL,
    pressure   REAL    NOT NULL,
    FOREIGN KEY (test_id) REFERENCES tests(id)
);

CREATE INDEX IF NOT EXISTS idx_samples_test_ts
    ON samples (test_id, ts);

CREATE TABLE IF NOT EXISTS blocks (
    test_id          INTEGER NOT NULL,
    level            INTEGER NOT NULL,
    block_index      INTEGER NOT NULL,
    start_ts         INTEGER NOT NULL,
    end_ts           INTEGER NOT NULL,
    sample_count     INTEGER NOT NULL,
    first_ts         INTEGER NOT NULL,
    last_ts          INTEGER NOT NULL,
    first_pressure   REAL    NOT NULL,
    last_pressure    REAL    NOT NULL,
    max_pressure     REAL    NOT NULL,
    max_pressure_ts  INTEGER NOT NULL,
    min_pressure     REAL    NOT NULL,
    min_pressure_ts  INTEGER NOT NULL,
    first_rowid      INTEGER NOT NULL,
    last_rowid       INTEGER NOT NULL,
    PRIMARY KEY (test_id, level, block_index)
);

CREATE INDEX IF NOT EXISTS idx_blocks_test_level_ts
    ON blocks (test_id, level, start_ts, end_ts);

CREATE TABLE IF NOT EXISTS pending_repairs (
    test_id      INTEGER PRIMARY KEY,
    reason       TEXT    NOT NULL DEFAULT '',
    requested_by TEXT    NOT NULL DEFAULT '',
    attempts     INTEGER NOT NULL DEFAULT 1,
    last_error   TEXT,
    marked_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (test_id) REFERENCES tests(id)
);
"""


def connect(db_path=None) -> sqlite3.Connection:
    """打开一个带常用 PRAGMA 的连接。"""
    path = str(db_path or config.DB_PATH)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA temp_store = MEMORY")
    return conn


@contextmanager
def session() -> Iterator[sqlite3.Connection]:
    """请求级连接上下文，正常结束提交，异常回滚。"""
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path=None) -> None:
    """进程启动时调用，幂等建库建表。可指定库路径（默认取配置）。"""
    path = Path(db_path) if db_path is not None else config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()

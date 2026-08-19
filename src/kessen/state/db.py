"""SQLite状態DB初期化 (PRD §6.1 / OPS.md §6.1)。

WAL モードで読書並行性を確保。`runs` と `round_scores` の2テーブル。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id          TEXT PRIMARY KEY,
    project         TEXT NOT NULL,
    status          TEXT NOT NULL,                -- pending | running | completed | failed | cancelled | aborted | interrupted
    current_round   INTEGER NOT NULL DEFAULT 0,
    total_rounds    INTEGER NOT NULL,
    started_at      TEXT NOT NULL,                -- ISO8601 JST
    finished_at     TEXT,
    overall_winner  TEXT,
    project_dir     TEXT NOT NULL,
    error           TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_project_status ON runs(project, status);
CREATE INDEX IF NOT EXISTS idx_runs_started_at     ON runs(started_at DESC);

CREATE TABLE IF NOT EXISTS round_scores (
    run_id      TEXT NOT NULL,
    round       INTEGER NOT NULL,
    persona_id  TEXT NOT NULL,
    score       REAL,                              -- NULL のとき winner選定除外
    status      TEXT NOT NULL,                     -- ok | validation_failed | runtime_failed | cancelled
    attempts    INTEGER NOT NULL DEFAULT 1,
    note        TEXT,
    PRIMARY KEY (run_id, round, persona_id),
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_round_scores_run ON round_scores(run_id, round);
"""


def init_db(path: Path | str) -> sqlite3.Connection:
    """DBファイルを開いてスキーマを適用、WAL/foreign_keysを有効化。

    Args:
        path: 通常はファイルパス。テスト用に `:memory:` 文字列も渡せる
              (ただしWALは有効化されない。`:memory:` ではWALモードはno-op)。

    Returns:
        autocommit (`isolation_level=None`) モードの Connection。
        呼出元は明示的に `BEGIN`/`COMMIT` を発行するか、単発SQLとして使う。
    """
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


def close_db(conn: sqlite3.Connection) -> None:
    try:
        conn.close()
    except sqlite3.Error:
        pass

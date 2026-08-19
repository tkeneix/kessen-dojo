"""高レベル状態APIラッパ。

UI/API層は sqlite を直接触らず本リポジトリ経由でアクセスする。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from kessen.evaluator.base import Leaderboard
from kessen.logging_config import JST


@dataclass(frozen=True)
class RunRecord:
    """`runs` テーブル1行 (read-only DTO)。"""

    run_id: str
    project: str
    status: str
    current_round: int
    total_rounds: int
    started_at: str
    finished_at: str | None
    overall_winner: str | None
    project_dir: str
    error: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> RunRecord:
        return cls(
            run_id=row["run_id"],
            project=row["project"],
            status=row["status"],
            current_round=row["current_round"],
            total_rounds=row["total_rounds"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            overall_winner=row["overall_winner"],
            project_dir=row["project_dir"],
            error=row["error"],
        )

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "project": self.project,
            "status": self.status,
            "current_round": self.current_round,
            "total_rounds": self.total_rounds,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "overall_winner": self.overall_winner,
            "project_dir": self.project_dir,
            "error": self.error,
        }


@dataclass(frozen=True)
class RoundScoreRecord:
    """`round_scores` テーブル1行 (read-only DTO)。"""

    run_id: str
    round: int
    persona_id: str
    score: float | None
    status: str
    attempts: int
    note: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> RoundScoreRecord:
        return cls(
            run_id=row["run_id"],
            round=row["round"],
            persona_id=row["persona_id"],
            score=row["score"],
            status=row["status"],
            attempts=row["attempts"],
            note=row["note"],
        )

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "round": self.round,
            "persona_id": self.persona_id,
            "score": self.score,
            "status": self.status,
            "attempts": self.attempts,
            "note": self.note,
        }


class RunRepository:
    """runs.db への高レベルアクセサ。同期API (sqlite3はsynchronous)。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ------------------------------------------------------------------
    # write ops
    # ------------------------------------------------------------------

    def create_run(
        self,
        run_id: str,
        project: str,
        project_dir: Path,
        total_rounds: int,
        *,
        status: str = "pending",
        started_at: str | None = None,
    ) -> None:
        ts = started_at or datetime.now(tz=JST).isoformat(timespec="seconds")
        self.conn.execute(
            "INSERT INTO runs (run_id, project, project_dir, total_rounds, status, started_at, current_round) "
            "VALUES (?, ?, ?, ?, ?, ?, 0)",
            (run_id, project, str(project_dir), total_rounds, status, ts),
        )

    def update_status(
        self,
        run_id: str,
        *,
        status: str | None = None,
        current_round: int | None = None,
        finished_at: str | None = None,
        overall_winner: str | None = None,
        error: str | None = None,
    ) -> None:
        sets: list[str] = []
        params: list = []
        if status is not None:
            sets.append("status = ?")
            params.append(status)
        if current_round is not None:
            sets.append("current_round = ?")
            params.append(current_round)
        if finished_at is not None:
            sets.append("finished_at = ?")
            params.append(finished_at)
        if overall_winner is not None:
            sets.append("overall_winner = ?")
            params.append(overall_winner)
        if error is not None:
            sets.append("error = ?")
            params.append(error)
        if not sets:
            return
        params.append(run_id)
        self.conn.execute(f"UPDATE runs SET {', '.join(sets)} WHERE run_id = ?", params)

    def save_leaderboard(self, run_id: str, leaderboard: Leaderboard) -> None:
        """1ラウンドのscoreエントリを round_scores にUPSERT。"""
        for entry in leaderboard.entries:
            self.conn.execute(
                "INSERT INTO round_scores (run_id, round, persona_id, score, status, attempts, note) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(run_id, round, persona_id) DO UPDATE SET "
                "  score = excluded.score, status = excluded.status, "
                "  attempts = excluded.attempts, note = excluded.note",
                (run_id, leaderboard.round, entry.persona_id,
                 entry.score, entry.status, entry.attempts, entry.note),
            )

    def cleanup_stale_running(self) -> int:
        """サーバ起動時に呼ぶ。`running` のままだったrunを `interrupted` にマーク。

        Returns: 影響行数
        """
        cur = self.conn.execute(
            "UPDATE runs SET status = 'interrupted' WHERE status IN ('running', 'pending')"
        )
        return cur.rowcount

    def delete_run(self, run_id: str) -> None:
        """runと関連 round_scores を削除 (FOREIGN KEY CASCADE)。"""
        self.conn.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))

    # ------------------------------------------------------------------
    # read ops
    # ------------------------------------------------------------------

    def get_run(self, run_id: str) -> RunRecord | None:
        row = self.conn.execute(
            "SELECT * FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        return RunRecord.from_row(row) if row else None

    def list_runs(
        self,
        *,
        project: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[RunRecord]:
        sql = "SELECT * FROM runs"
        clauses: list[str] = []
        params: list = []
        if project:
            clauses.append("project = ?")
            params.append(project)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY started_at DESC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()
        return [RunRecord.from_row(r) for r in rows]

    def get_round_scores(
        self,
        run_id: str,
        *,
        round_n: int | None = None,
    ) -> list[RoundScoreRecord]:
        if round_n is None:
            sql = "SELECT * FROM round_scores WHERE run_id = ? ORDER BY round, persona_id"
            params: Iterable = (run_id,)
        else:
            sql = "SELECT * FROM round_scores WHERE run_id = ? AND round = ? ORDER BY persona_id"
            params = (run_id, round_n)
        rows = self.conn.execute(sql, params).fetchall()
        return [RoundScoreRecord.from_row(r) for r in rows]

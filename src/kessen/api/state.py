"""APIサーバ全体の共有状態。

- DB connection (SQLite WAL)
- RunRepository (DB CRUD)
- 実行中タスクレジストリ (run_id → asyncio.Task) — cancel用
- repo_root (ペルソナ/ナレッジの shared/ 解決用)
"""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from kessen.state import RunRepository


@dataclass
class AppState:
    """アプリケーション寿命に紐づく状態 (lifespan で生成・破棄)。"""

    conn: sqlite3.Connection
    repo: RunRepository
    repo_root: Path
    db_path: Path
    tasks: dict[str, asyncio.Task] = field(default_factory=dict)

    def register_task(self, run_id: str, task: asyncio.Task) -> None:
        self.tasks[run_id] = task

    def cancel_task(self, run_id: str) -> bool:
        """タスクをキャンセル。存在しなければ False。"""
        task = self.tasks.get(run_id)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    def cleanup_task(self, run_id: str) -> None:
        self.tasks.pop(run_id, None)

    async def shutdown(self) -> None:
        """全アクティブタスクをキャンセルしてDBクローズ。"""
        for _run_id, task in list(self.tasks.items()):
            if not task.done():
                task.cancel()
        # 終了待ち (短いgrace period)
        if self.tasks:
            await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        try:
            self.conn.close()
        except sqlite3.Error:
            pass

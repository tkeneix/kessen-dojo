"""BaseWorker ABC + WorkerResult。

PRD §4.2 を実装:
- ペルソナ配下の持ち場 (サブタスク実行単位)
- 問題/対策ナレッジを knowledge/per-persona/{persona_id}/workers/{worker_id}/lessons.jsonl に蓄積
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from kessen.knowledge.store import KnowledgeEntry, append


@dataclass
class WorkerResult:
    """Worker 実行結果。"""

    worker_id: str
    status: str  # "ok" | "error"
    summary: str = ""
    output_files: list[str] = field(default_factory=list)
    error: str | None = None


class BaseWorker(ABC):
    """ペルソナ配下の持ち場。案件側で継承してサブタスクを実装する。

    knowledge/per-persona/{persona_id}/workers/{worker_id}/lessons.jsonl に
    試行・気づき・失敗ログを蓄積し、次ラウンドのナレッジ注入に利用される。
    """

    def __init__(
        self,
        worker_id: str,
        persona_id: str,
        project_dir: Path,
        *,
        engine: str | None = None,
        role: str = "",
        engine_options: dict | None = None,
        output_filename: str | None = None,
    ):
        """Worker を初期化する。

        engine / role / engine_options は LLM-backed Worker (LLMWorker 等) のための
        共通フィールド。tool 系・subprocess 系の独自 Worker では None / 空で構わない。
        factory.build_worker から TOML 由来の値が注入される。

        output_filename:
          worker TOML の [worker].output_filename。設定されていれば LLMWorker は
          応答全体を out_dir/<output_filename> に書き、`=== FILE: ===` マーカー
          パースをスキップする。決定論的パスにすることで後続ペルソナの Glob を不要にする。
        """
        self.worker_id = worker_id
        self.persona_id = persona_id
        self.project_dir = Path(project_dir)
        self.engine = engine
        self.role = role
        self.engine_options = engine_options or {}
        self.output_filename = output_filename

    @abstractmethod
    async def execute(self, in_dir: Path, out_dir: Path) -> WorkerResult:
        """in_dir を読んで out_dir に成果物を書き出す。"""

    def append_lesson(
        self,
        kind: str,
        content: str,
        *,
        round: int | None = None,
    ) -> None:
        """Worker の lessons.jsonl に知見を追記。

        パス: knowledge/per-persona/{persona_id}/workers/{worker_id}/lessons.jsonl
        """
        path = (
            self.project_dir
            / "knowledge"
            / "per-persona"
            / self.persona_id
            / "workers"
            / self.worker_id
            / "lessons.jsonl"
        )
        append(
            path,
            KnowledgeEntry.now(
                layer="private",
                kind=kind,
                content=content,
                persona_id=self.persona_id,
                round=round,
            ),
        )

"""Evaluator基底 + Leaderboard。

PRD §4.3 / §6.1 / §6.2 を実装。

3段構成: validate (軽量チェック) → score (本評価) → evaluate (集約)
- validate: リトライ機構が呼ぶ。失敗時 issues/suggestions を返す
- score: validate OK 済み out/ をスコアリング (重い処理はここ)
- evaluate: 各 participant の score を集約して Leaderboard を返す (デフォルト実装あり)
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal


@dataclass
class ValidationResult:
    """validate() の結果。"""

    ok: bool
    issues: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)


@dataclass
class ScoreResult:
    """score() の結果。"""

    score: float
    note: str = ""
    metrics: dict = field(default_factory=dict)


# leaderboard 上の各エントリ status
LeaderboardStatus = Literal["ok", "validation_failed", "runtime_failed", "cancelled"]


@dataclass
class LeaderboardEntry:
    """ラウンド内の1ペルソナの結果。"""

    persona_id: str
    score: float | None              # 失敗時 None (winner選定から除外)
    status: LeaderboardStatus
    attempts: int = 1
    note: str = ""
    last_issues: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)


@dataclass
class Leaderboard:
    """ラウンドごとの Leaderboard。final.json/leaderboard.json の中身。"""

    round: int
    entries: list[LeaderboardEntry] = field(default_factory=list)
    winner: str | None = None
    all_failed: bool = False
    metadata: dict = field(default_factory=dict)

    # ------------------------------------------------------------------
    # builders
    # ------------------------------------------------------------------

    def add_ok(self, persona_id: str, score_result: ScoreResult, attempts: int) -> None:
        self.entries.append(
            LeaderboardEntry(
                persona_id=persona_id,
                score=score_result.score,
                status="ok",
                attempts=attempts,
                note=score_result.note,
                metrics=score_result.metrics,
            )
        )

    def add_failed(
        self,
        persona_id: str,
        status: LeaderboardStatus,
        attempts: int,
        last_issues: list[str] | None = None,
    ) -> None:
        self.entries.append(
            LeaderboardEntry(
                persona_id=persona_id,
                score=None,
                status=status,
                attempts=attempts,
                last_issues=last_issues or [],
            )
        )

    def finalize(self) -> None:
        """winner と all_failed を確定。score 降順で entries をソートする。"""
        # score=None は末尾、それ以外は score 降順
        self.entries.sort(
            key=lambda e: (-1 if e.score is None else 0, -(e.score or 0.0)),
        )
        ok_entries = [e for e in self.entries if e.status == "ok" and e.score is not None]
        if ok_entries:
            top = max(ok_entries, key=lambda e: e.score)
            self.winner = top.persona_id
            self.all_failed = False
        else:
            self.winner = None
            self.all_failed = True

    # ------------------------------------------------------------------
    # serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "round": self.round,
            "winner": self.winner,
            "all_failed": self.all_failed,
            "entries": [asdict(e) for e in self.entries],
            "metadata": self.metadata,
        }

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


class BaseEvaluator(ABC):
    """全Evaluatorの基底。案件側で継承し validate/score を実装する。

    config は project.toml の [evaluator] セクションの dict (class キー除く)。
    """

    def __init__(
        self,
        project_dir: Path,
        config: dict,
        runtime: object | None = None,
    ):
        self.project_dir = Path(project_dir)
        self.config = config
        self.runtime = runtime    # DockerRunner 等 (P8 で導入)

    # ------------------------------------------------------------------
    # required overrides
    # ------------------------------------------------------------------

    @abstractmethod
    async def validate(self, persona_out_dir: Path) -> ValidationResult:
        """成果物が評価可能か検査。軽量チェックに留める (重い処理は score へ)。"""

    @abstractmethod
    async def score(self, persona_out_dir: Path) -> ScoreResult:
        """validate OK の成果物を本評価。"""

    # ------------------------------------------------------------------
    # default impl (上書き可)
    # ------------------------------------------------------------------

    async def evaluate(
        self,
        round_dir: Path,
        validated_participants: list[str],
        round_n: int = 1,
    ) -> Leaderboard:
        """validate=OK 済みの participants だけを score して Leaderboard 化。

        validate→retry のループは Orchestrator (orchestrator/round.py) 側で完結。
        ここに来る participants は全員 validate OK 前提。
        """
        leaderboard = Leaderboard(round=round_n)
        for pid in validated_participants:
            out_dir = round_dir / pid / "out"
            score_result = await self.score(out_dir)
            # attempts は orchestrator が後付けする (Round runner が再構築)
            leaderboard.add_ok(pid, score_result, attempts=1)
        leaderboard.finalize()
        return leaderboard

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    @staticmethod
    def list_output_files(out_dir: Path, *, exclude_underscore: bool = True) -> list[Path]:
        """out_dir 配下のファイル一覧。'_' 始まりのメタファイル (例: _retry_feedback.md) を除外。"""
        if not out_dir.exists():
            return []
        files = [p for p in out_dir.rglob("*") if p.is_file()]
        if exclude_underscore:
            files = [p for p in files if not p.name.startswith("_")]
        return sorted(files)

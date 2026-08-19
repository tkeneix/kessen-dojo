"""StubEvaluator: テスト/プロトタイプ用の決定的評価器。

ファイル数とトータル文字数で score を算出。LLM呼出なし、認証不要。
"""

from __future__ import annotations

from pathlib import Path

from kessen.evaluator.base import BaseEvaluator, ScoreResult, ValidationResult


class StubEvaluator(BaseEvaluator):
    """ファイル数 + 文字数 で機械的に採点する決定的Evaluator。

    config:
      file_count_weight: float = 1.0
      char_count_weight: float = 0.001
      min_files: int = 1                   # validate 閾値
    """

    async def validate(self, persona_out_dir: Path) -> ValidationResult:
        files = self.list_output_files(persona_out_dir)
        min_files = int(self.config.get("min_files", 1))
        if len(files) < min_files:
            return ValidationResult(
                ok=False,
                issues=[f"out/ にファイルが {len(files)} 個しかありません (最低 {min_files} 個必要)"],
                suggestions=["少なくとも1ファイルを out/ に書き出してください"],
            )
        return ValidationResult(ok=True)

    async def score(self, persona_out_dir: Path) -> ScoreResult:
        files = self.list_output_files(persona_out_dir)
        total_chars = sum(p.read_text(encoding="utf-8", errors="ignore").__len__() for p in files)
        fcw = float(self.config.get("file_count_weight", 1.0))
        ccw = float(self.config.get("char_count_weight", 0.001))
        score = len(files) * fcw + total_chars * ccw
        return ScoreResult(
            score=round(score, 4),
            note=f"{len(files)} files, {total_chars} chars",
            metrics={"files": len(files), "chars": total_chars},
        )

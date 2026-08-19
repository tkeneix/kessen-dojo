"""LLMJudgeEvaluator: Claude SDK 経由で汎用LLMジャッジ。

quant 等の機械的評価が無い案件 (例: 業界課題発見系) で使う既定評価器。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, query

from kessen.config.loader import parse_engine
from kessen.evaluator.base import BaseEvaluator, ScoreResult, ValidationResult
from kessen.logging_config import setup_logger
from kessen.personas.claude_persona import to_claude_model_id

DEFAULT_RUBRIC = """\
0.0 から 1.0 のスコアを以下の観点で総合判定してください:

1. 完成度: 出力が完結しており、明確に要求に応えているか
2. 構造: 適切なフォーマット (Markdown見出し/リスト等) で整理されているか
3. 内容: 入力データに対する具体的な分析・要約があるか
4. 簡潔性: 冗長でなく、必要十分な情報量か

スコア基準:
- 1.0: 全観点で優秀
- 0.8: 概ね優秀、軽微な不足
- 0.6: 標準的
- 0.4: 主要観点で問題あり
- 0.2: 大半の観点で不足
- 0.0: 出力が要求にほぼ応えていない
"""


class LLMJudgeEvaluator(BaseEvaluator):
    """Claude (デフォルトhaiku) で out/ の成果物をジャッジする。

    config:
      judge_model: str = "claude:haiku"
      rubric: str = DEFAULT_RUBRIC                # ジャッジ用ルーブリック
      min_files: int = 1                          # validate 閾値
      max_chars_per_file: int = 8000              # 1ファイル切詰め長
    """

    async def validate(self, persona_out_dir: Path) -> ValidationResult:
        files = self.list_output_files(persona_out_dir)
        min_files = int(self.config.get("min_files", 1))
        if len(files) < min_files:
            return ValidationResult(
                ok=False,
                issues=[f"out/ のファイル数が不足 ({len(files)} < {min_files})"],
                suggestions=["少なくとも1つの成果物ファイルを書き出してください"],
            )
        return ValidationResult(ok=True)

    async def score(self, persona_out_dir: Path) -> ScoreResult:
        files = self.list_output_files(persona_out_dir)
        max_chars = int(self.config.get("max_chars_per_file", 8000))
        rubric = self.config.get("rubric", DEFAULT_RUBRIC)
        engine = self.config.get("judge_model", "claude:haiku")

        provider, model_part = parse_engine(engine)
        if provider != "claude":
            raise ValueError(
                f"LLMJudgeEvaluator currently supports only claude:* judges, got: {engine!r}"
            )
        model = to_claude_model_id(model_part)

        # 成果物を1つの prompt に詰め込む
        artifact_blocks = []
        for f in files:
            try:
                content = f.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                content = "(binary file, skipped)"
            if len(content) > max_chars:
                content = content[:max_chars] + f"\n... (truncated, original {len(content)} chars)"
            rel = f.relative_to(persona_out_dir)
            artifact_blocks.append(f"### {rel}\n```\n{content}\n```")
        artifacts_text = "\n\n".join(artifact_blocks) if artifact_blocks else "(empty)"

        system_prompt = (
            "あなたは厳密な評価者です。以下のルーブリックに従い、提示された成果物群を評価してください。\n\n"
            f"# ルーブリック\n{rubric}\n\n"
            "**回答形式 (厳守)**: 必ず最後に以下のJSONを単独行で出力してください。\n"
            '`{"score": <0.0-1.0の数値>, "note": "<50字以内の講評>"}`'
        )
        user_prompt = (
            f"以下は評価対象ペルソナの成果物 ({len(files)}ファイル) です。\n"
            "ルーブリックに従ってスコアと講評を返してください。\n\n"
            f"# 成果物\n{artifacts_text}"
        )

        logger = setup_logger("kessen")
        logger.info("LLMJudge: scoring %s files in %s with model=%s", len(files), persona_out_dir, model)

        options = ClaudeAgentOptions(
            system_prompt=system_prompt,
            allowed_tools=[],                # 推論のみ (ツール使用不要)
            model=model,
            max_turns=2,
            permission_mode="default",
            setting_sources=[],
        )

        text = ""
        async for msg in query(prompt=user_prompt, options=options):
            if hasattr(msg, "result") and msg.result:
                text = msg.result

        score, note = self._extract_score(text)
        return ScoreResult(
            score=score,
            note=note,
            metrics={"judge_model": model, "files": len(files), "raw_response": text[-300:]},
        )

    @staticmethod
    def _extract_score(text: str) -> tuple[float, str]:
        """ジャッジ回答から JSON {"score":..., "note":...} を抽出。失敗時は0.0。"""
        if not text:
            return 0.0, "judge returned empty response"
        # 最終JSONを探す: 最後の {...} ブロック
        matches = re.findall(r"\{[^{}]*\}", text)
        for m in reversed(matches):
            try:
                obj = json.loads(m)
                score = float(obj.get("score", 0))
                # clip to [0, 1]
                score = max(0.0, min(1.0, score))
                note = str(obj.get("note", ""))[:120]
                return score, note
            except (json.JSONDecodeError, ValueError, TypeError):
                continue
        return 0.0, f"failed to parse judge response: {text[:80]}"

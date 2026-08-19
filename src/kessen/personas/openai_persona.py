"""OpenAIペルソナ。

PRD §8.2 / §8.3 に準拠:
- openai SDK 直叩き (Claude Agent SDK は使わない)
- 単発 chat.completions: 入力ファイル群を user prompt に inline、応答を out/ に書く
- ツール反復は標準では行わない (PRD §8 末尾の「1往復制限」を踏襲)

multi-fileアウトプット: 応答中に '=== FILE: <name> ===' の区切りがあれば複数ファイルに展開。
区切りがなければ全体を out/response.md に書き出す。
"""

from __future__ import annotations

import re
from pathlib import Path

from kessen.auth.resolver import require_openai_key
from kessen.config.loader import parse_engine
from kessen.logging_config import setup_logger
from kessen.personas.base import BasePersona, PersonaResult

_FILE_DELIMITER = re.compile(r"^===\s*FILE:\s*(.+?)\s*===\s*$", re.MULTILINE)
_SAFE_FILENAME = re.compile(r"^[\w\-.]+$")
_DEFAULT_FALLBACK = "response.md"


class OpenAIPersona(BasePersona):
    """OpenAI Chat Completions API 経由で動くペルソナ。

    config.engine_options:
      max_completion_tokens: int = 4000
      temperature: float = 0.7
      max_chars_per_file: int = 8000
    """

    base_prompt: str = (
        "あなたは kessen-dojo の参加ペルソナです。"
        "与えられた入力ファイル群を読み、ペルソナの役割に沿った成果物を生成してください。"
    )
    provider_name: str = "openai"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        provider, model_part = parse_engine(self.engine)
        if provider != self.provider_name:
            raise ValueError(
                f"{type(self).__name__} requires engine '{self.provider_name}:*', got: {self.engine!r}"
            )
        self.model = model_part

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    async def run(
        self,
        in_dir: Path,
        out_dir: Path,
        *,
        dry_run: bool = False,
    ) -> PersonaResult:
        in_dir = Path(in_dir)
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        system_prompt = self.compose_system_prompt()
        user_prompt = self._build_input_aware_prompt(in_dir, out_dir)

        if dry_run:
            (out_dir / "_dry_run_system_prompt.md").write_text(system_prompt, encoding="utf-8")
            (out_dir / "_dry_run_user_prompt.md").write_text(user_prompt, encoding="utf-8")
            return PersonaResult(
                persona_id=self.persona_id,
                status="dry_run",
                summary=f"dry-run: prompts written to {out_dir}",
                output_files=["_dry_run_system_prompt.md", "_dry_run_user_prompt.md"],
            )

        logger = setup_logger(f"persona.{self.persona_id}")
        logger.info(
            "%s run start: persona=%s model=%s in=%s out=%s",
            type(self).__name__, self.persona_id, self.model, in_dir, out_dir,
        )

        try:
            text = await self._call_llm(system_prompt, user_prompt)
        except Exception as e:
            logger.exception("%s run failed: %s", type(self).__name__, e)
            return PersonaResult(
                persona_id=self.persona_id,
                status="error",
                summary=f"runtime error: {e}",
                error=str(e),
            )

        files_written = self._write_response(out_dir, text)
        output_files = [str(p.relative_to(out_dir)) for p in sorted(out_dir.rglob("*")) if p.is_file()]
        logger.info(
            "%s run done: persona=%s response_chars=%d files_written=%d",
            type(self).__name__, self.persona_id, len(text), len(files_written),
        )
        return PersonaResult(
            persona_id=self.persona_id,
            status="ok",
            summary=text[:500] if text else "completed (empty response)",
            output_files=output_files,
        )

    # ------------------------------------------------------------------
    # subclass hooks
    # ------------------------------------------------------------------

    def _make_client(self):
        """openai.AsyncOpenAI クライアントを生成 (subclassで base_url 等上書き可)。"""
        from openai import AsyncOpenAI
        return AsyncOpenAI(api_key=require_openai_key())

    async def _call_llm(self, system_prompt: str, user_prompt: str) -> str:
        client = self._make_client()
        opts = self.config.engine_options
        response = await client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_completion_tokens=int(opts.get("max_completion_tokens", 4000)),
            temperature=float(opts.get("temperature", 0.7)),
        )
        return response.choices[0].message.content or ""

    # ------------------------------------------------------------------
    # prompt assembly (input files → inlined user prompt)
    # ------------------------------------------------------------------

    def _build_input_aware_prompt(self, in_dir: Path, out_dir: Path) -> str:
        """入力ファイル群を user prompt に inline。

        _retry_feedback.md があれば先頭に出して目立たせる。
        """
        max_chars = int(self.config.engine_options.get("max_chars_per_file", 8000))
        files = self._read_input_files(in_dir, max_chars_per_file=max_chars)

        # _retry_feedback.md を先頭に
        feedback = [(rel, content) for rel, content in files if rel == "_retry_feedback.md"]
        others = [(rel, content) for rel, content in files if rel != "_retry_feedback.md"]
        ordered = feedback + others

        if ordered:
            files_block = "\n\n".join(f"## {rel}\n```\n{content}\n```" for rel, content in ordered)
        else:
            files_block = "(入力ファイルなし)"

        return (
            f"# 入力ファイル ({len(ordered)} 件)\n\n"
            f"{files_block}\n\n"
            f"# 出力指示\n\n"
            f"出力ディレクトリ: `{out_dir}`\n\n"
            f"上記の入力ファイル群を読み、ペルソナの役割に沿った成果物を生成してください。\n\n"
            f"**出力フォーマット**:\n"
            f"- 単一ファイルでよい場合 → そのまま markdown で書いてください (`response.md` に保存されます)\n"
            f"- 複数ファイルを書きたい場合 → 各ファイル先頭に区切り行を入れてください:\n"
            f"  ```\n"
            f"  === FILE: <ファイル名> ===\n"
            f"  <内容>\n"
            f"  === FILE: <次のファイル名> ===\n"
            f"  <内容>\n"
            f"  ```\n\n"
            f"説明文は出力に含めず、生成物そのもののみを返してください。"
        )

    @staticmethod
    def _read_input_files(in_dir: Path, *, max_chars_per_file: int = 8000) -> list[tuple[str, str]]:
        if not in_dir.exists():
            return []
        results: list[tuple[str, str]] = []
        for p in sorted(in_dir.rglob("*")):
            if not p.is_file():
                continue
            try:
                content = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                content = "(binary or unreadable, skipped)"
            if len(content) > max_chars_per_file:
                content = content[:max_chars_per_file] + f"\n... (truncated, original {len(content)} chars)"
            results.append((str(p.relative_to(in_dir)), content))
        return results

    # ------------------------------------------------------------------
    # response → file(s)
    # ------------------------------------------------------------------

    @classmethod
    def _write_response(cls, out_dir: Path, text: str) -> list[str]:
        """応答を解析して out/ にファイル書出。書き出したファイル名のリストを返す。

        '=== FILE: <name> ===' の区切りがあれば複数ファイル、無ければ response.md。
        ファイル名はサニタイズ ('..', '/' を含むもの、空、不正文字は拒否)。
        """
        if not text:
            return []

        matches = list(_FILE_DELIMITER.finditer(text))
        if not matches:
            (out_dir / _DEFAULT_FALLBACK).write_text(text.rstrip() + "\n", encoding="utf-8")
            return [_DEFAULT_FALLBACK]

        written: list[str] = []
        for i, m in enumerate(matches):
            name = m.group(1).strip()
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            body = text[start:end].strip("\n")

            if not _SAFE_FILENAME.match(name):
                # 不正ファイル名はスキップ (security)
                continue
            if ".." in name or name.startswith("."):
                continue
            (out_dir / name).write_text(body + "\n", encoding="utf-8")
            written.append(name)

        # 全部スキップされた場合は response.md にフォールバック
        if not written:
            (out_dir / _DEFAULT_FALLBACK).write_text(text.rstrip() + "\n", encoding="utf-8")
            written = [_DEFAULT_FALLBACK]
        return written

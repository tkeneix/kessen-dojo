"""LLMWorker: engine 文字列で LLM プロバイダを切替えるシングルショット Worker。

設計方針 (PRD 補遺 / 2026-05-01 設計レビュー):
- Worker は BasePersona と異なり knowledge を自動注入しない。
  in_dir に置かれたファイル群だけを LLM に渡す。
  ペルソナ orchestrator が必要な背景情報を _handoff/{worker}/in/ に staging する責任を持つ。
- 単発 chat 呼出 (ツール反復なし) で in_dir → 応答テキスト → out_dir にファイル展開。
- 出力ファイル名:
  - [worker].output_filename が設定されていれば、応答全体を out_dir/<output_filename> に書く
    (決定論的パス。後続ペルソナが Glob 不要で Read 直指定できる)
  - 未設定なら従来通り '=== FILE: <name> ===' 区切りで複数ファイル展開、区切り無しなら
    response.md にフォールバック (OpenAIPersona と同じプロトコル)
- Worker 単体リトライは持たない。失敗は例外でペルソナ orchestrator に伝播し、
  ペルソナ run 全体が再走することで対処する (Round runner の retry に委ねる)。

サポート engine:
  claude:<model>   → claude_agent_sdk.query() (Persona 層と同じ経路。サブスク認証も継承可)
  openai:<model>   → openai SDK
  xai:<model>      → openai SDK + base_url=https://api.x.ai/v1
  google:<model>   → google-genai SDK (Gemini)
"""

from __future__ import annotations

import re
from pathlib import Path

from kessen.auth.resolver import (
    require_google_key,
    require_openai_key,
    require_xai_key,
)
from kessen.config.loader import parse_engine
from kessen.logging_config import setup_logger
from kessen.personas.claude_persona import to_claude_model_id
from kessen.personas.grok_persona import XAI_BASE_URL
from kessen.workers.base import BaseWorker, WorkerResult

_FILE_DELIMITER = re.compile(r"^===\s*FILE:\s*(.+?)\s*===\s*$", re.MULTILINE)
_SAFE_FILENAME = re.compile(r"^[\w\-.]+$")
_DEFAULT_FALLBACK = "response.md"

# 異常停止と判断する finish_reason 値 (provider 横断)
# Gemini: "MAX_TOKENS", "SAFETY", "RECITATION", "OTHER", "BLOCKLIST", "PROHIBITED_CONTENT"
# OpenAI/xAI chat.completions: "length" (= MAX_TOKENS), "content_filter" (= SAFETY)
# OpenAI/xAI Responses API: status="completed" は正常 / "incomplete" + reason="max_output_tokens" 等
# 正常系は Gemini="STOP" / OpenAI chat="stop" / Responses="completed"
_NORMAL_FINISH_REASONS = {
    "STOP", "stop", "completed", "FINISH_REASON_UNSPECIFIED", None, "",
}

_BASE_PROMPT = (
    "あなたは kessen-dojo のペルソナ配下で動く Worker (専門家) です。"
    " 与えられた入力ファイル群を読み、role に従った成果物を生成してください。"
)


class WorkerLLMError(RuntimeError):
    """LLM 呼出で発生したエラー (provider別の例外を統一型にラップ)。"""


class LLMWorker(BaseWorker):
    """engine 文字列で LLM プロバイダを切り替える汎用 Worker。

    Args:
        worker_id: Worker 識別子 (lessons.jsonl のパスに使われる)
        persona_id: 親ペルソナ ID (lessons.jsonl のパスに使われる)
        project_dir: 案件ディレクトリ
        engine: "<provider>:<model>" 形式 (例: "claude:sonnet-4-6", "google:gemini-2.5-pro")
        role: Worker の役割説明 (system_prompt の # Role セクションに入る)
        engine_options: provider 別オプション (max_completion_tokens / temperature 等)
    """

    def __init__(
        self,
        worker_id: str,
        persona_id: str,
        project_dir: Path,
        *,
        engine: str,
        role: str = "",
        engine_options: dict | None = None,
        output_filename: str | None = None,
    ) -> None:
        super().__init__(
            worker_id=worker_id,
            persona_id=persona_id,
            project_dir=project_dir,
            engine=engine,
            role=role,
            engine_options=engine_options,
            output_filename=output_filename,
        )
        self.provider, self.model = parse_engine(engine)
        if self.provider not in ("claude", "openai", "xai", "google"):
            raise ValueError(
                f"LLMWorker: unsupported engine provider {self.provider!r}"
                f" (expected one of: claude, openai, xai, google)"
            )

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    async def execute(self, in_dir: Path, out_dir: Path) -> WorkerResult:
        in_dir = Path(in_dir)
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        logger = setup_logger(f"worker.{self.persona_id}.{self.worker_id}")
        logger.info(
            "LLMWorker execute start: persona=%s worker=%s engine=%s in=%s out=%s",
            self.persona_id, self.worker_id, self.engine, in_dir, out_dir,
        )

        system_prompt = self._compose_system_prompt()
        user_prompt = self._build_input_aware_prompt(in_dir, out_dir)

        try:
            text = await self._call_llm(system_prompt, user_prompt)
        except Exception as e:
            logger.exception(
                "LLMWorker LLM call failed: persona=%s worker=%s engine=%s err=%s",
                self.persona_id, self.worker_id, self.engine, e,
            )
            return WorkerResult(
                worker_id=self.worker_id,
                status="error",
                summary=f"LLM call failed: {type(e).__name__}: {e}",
                error=str(e),
            )

        files_written = self._write_response(out_dir, text, output_filename=self.output_filename)
        logger.info(
            "LLMWorker execute done: persona=%s worker=%s response_chars=%d files=%d",
            self.persona_id, self.worker_id, len(text), len(files_written),
        )
        return WorkerResult(
            worker_id=self.worker_id,
            status="ok",
            summary=text[:500] if text else "completed (empty response)",
            output_files=files_written,
        )

    # ------------------------------------------------------------------
    # prompt assembly
    # ------------------------------------------------------------------

    def _compose_system_prompt(self) -> str:
        """Worker の system_prompt は base + role のみ。knowledge 自動注入はしない。"""
        parts = [_BASE_PROMPT]
        if self.role:
            parts.append(f"# Role\n{self.role}")
        return "\n\n".join(parts)

    def _build_input_aware_prompt(self, in_dir: Path, out_dir: Path) -> str:
        """in_dir 配下の全ファイルを inline で user_prompt に詰める。

        output_filename が設定されている場合は応答全体が固定ファイルに保存されるため、
        `=== FILE: ===` マーカーの説明を出さない (literal 保存される事故を防ぐ)。
        """
        max_chars = int(self.engine_options.get("max_chars_per_file", 8000))
        files = self._read_input_files(in_dir, max_chars_per_file=max_chars)

        if files:
            files_block = "\n\n".join(
                f"## {rel}\n```\n{content}\n```" for rel, content in files
            )
        else:
            files_block = "(入力ファイルなし)"

        if self.output_filename:
            output_section = (
                f"**出力フォーマット**:\n"
                f"応答テキスト全体がそのまま `{self.output_filename}` として保存されます。\n"
                f"ファイル名・区切り行・マーカー (\"FILE:\" などの装飾) は出力に含めないでください"
                f" (literal 文字列として保存され可読性を損ないます)。\n"
                f"説明文は出力に含めず、生成物そのもののみを返してください。"
            )
        else:
            output_section = (
                f"**出力フォーマット**:\n"
                f"- 単一ファイルでよい場合 → そのまま markdown で書いてください"
                f" (`response.md` に保存されます)\n"
                f"- 複数ファイルを書きたい場合 → 各ファイル先頭に区切り行を入れてください:\n"
                f"  ```\n"
                f"  === FILE: <ファイル名> ===\n"
                f"  <内容>\n"
                f"  === FILE: <次のファイル名> ===\n"
                f"  <内容>\n"
                f"  ```\n\n"
                f"説明文は出力に含めず、生成物そのもののみを返してください。"
            )

        return (
            f"# 入力ファイル ({len(files)} 件)\n\n"
            f"{files_block}\n\n"
            f"# 出力指示\n\n"
            f"出力ディレクトリ: `{out_dir}`\n\n"
            f"上記の入力ファイル群を読み、Role に沿った成果物を生成してください。\n\n"
            f"{output_section}"
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

    @staticmethod
    def _write_response(
        out_dir: Path,
        text: str,
        *,
        output_filename: str | None = None,
    ) -> list[str]:
        """応答テキストを out_dir に書出す。

        output_filename が設定されていれば応答全体を `out_dir/<output_filename>` に書き、
        `=== FILE: ===` マーカーパースはスキップする (決定論的パス)。
        未設定なら従来通り marker パース + response.md フォールバック。
        """
        if not text:
            return []

        if output_filename:
            (out_dir / output_filename).write_text(text.rstrip() + "\n", encoding="utf-8")
            return [output_filename]

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
                continue
            if ".." in name or name.startswith("."):
                continue
            (out_dir / name).write_text(body + "\n", encoding="utf-8")
            written.append(name)

        if not written:
            (out_dir / _DEFAULT_FALLBACK).write_text(text.rstrip() + "\n", encoding="utf-8")
            written = [_DEFAULT_FALLBACK]
        return written

    # ------------------------------------------------------------------
    # provider dispatch
    # ------------------------------------------------------------------

    async def _call_llm(self, system_prompt: str, user_prompt: str) -> str:
        if self.provider == "claude":
            return await self._call_claude(system_prompt, user_prompt)
        if self.provider == "openai":
            return await self._call_openai_compatible(
                system_prompt, user_prompt, base_url=None, api_key=require_openai_key(),
            )
        if self.provider == "xai":
            return await self._call_openai_compatible(
                system_prompt, user_prompt, base_url=XAI_BASE_URL, api_key=require_xai_key(),
            )
        if self.provider == "google":
            return await self._call_gemini(system_prompt, user_prompt)
        raise WorkerLLMError(f"unsupported provider: {self.provider!r}")

    async def _call_claude(self, system_prompt: str, user_prompt: str) -> str:
        """claude_agent_sdk 経由で単発呼出。

        Persona 層 (ClaudePersona) と同じ SDK を使うことで、CLI サブスクリプション
        認証 (`claude /login` 状態) もそのまま継承できる。

        単発 chat 化のために `allowed_tools=[]` + `max_turns=1` を指定。
        ResultMessage.result からテキストを取得し、上位の FILE marker パーサに渡す。

        制約: ClaudeAgentOptions は temperature / max_tokens を直接受けないため、
        engine_options.temperature / max_tokens は Claude では無視される
        (CLI/モデル既定が使われる)。OpenAI/xAI/Gemini では引き続き有効。

        debug: claude CLI subprocess の stderr を kessen logger に流すため stderr
        コールバックを設定。`Fatal error in message reader: Command failed with
        exit code 1` の根本原因を特定するため必須 (PRD §14 既知問題の調査)。
        """
        from claude_agent_sdk import ClaudeAgentOptions, query

        logger = setup_logger(f"worker.{self.persona_id}.{self.worker_id}")

        # subprocess stderr を取り込むコレクタ
        stderr_lines: list[str] = []

        def _stderr_collector(line: str) -> None:
            line = line.rstrip("\n")
            if line:
                stderr_lines.append(line)
                logger.debug("[claude-cli stderr] %s", line)

        model_id = to_claude_model_id(self.model)
        options = ClaudeAgentOptions(
            system_prompt=system_prompt,
            allowed_tools=[],            # Worker は単発 chat — ツール反復しない
            max_turns=1,
            model=model_id,
            setting_sources=[],          # .claude/ 自動ロード抑止 (PRD §7.4)
            stderr=_stderr_collector,    # subprocess stderr を捕捉
        )

        final_text = ""
        try:
            async for message in query(prompt=user_prompt, options=options):
                # ResultMessage に最終テキストが入る (ClaudePersona と同じ取り出し方)
                if hasattr(message, "result") and message.result:
                    final_text = message.result
        except Exception as e:
            # 例外時に収集した stderr を ERROR レベルで出してから再 raise
            if stderr_lines:
                logger.error(
                    "claude CLI subprocess failed (%s). Last %d stderr lines:",
                    e, len(stderr_lines),
                )
                for line in stderr_lines[-30:]:   # 末尾30行に制限
                    logger.error("  | %s", line)
            else:
                logger.error("claude CLI subprocess failed (%s) and stderr was empty", e)
            raise
        return final_text

    async def _call_openai_compatible(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        base_url: str | None,
        api_key: str,
    ) -> str:
        """OpenAI 互換 chat.completions エンドポイント (OpenAI / xAI 両対応)。"""
        from openai import AsyncOpenAI

        client_kwargs = {"api_key": api_key}
        if base_url:
            client_kwargs["base_url"] = base_url
        client = AsyncOpenAI(**client_kwargs)

        max_tokens = int(self.engine_options.get("max_completion_tokens", 4000))
        temperature = float(self.engine_options.get("temperature", 0.7))

        response = await client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_completion_tokens=max_tokens,
            temperature=temperature,
        )
        text = response.choices[0].message.content or ""
        self._warn_if_abnormal_finish(
            getattr(response.choices[0], "finish_reason", None),
            text_len=len(text),
            max_tokens=max_tokens,
        )
        return text

    async def _call_gemini(self, system_prompt: str, user_prompt: str) -> str:
        """google-genai SDK 経由で Gemini 呼出。"""
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=require_google_key())
        max_tokens = int(self.engine_options.get("max_output_tokens", 4096))
        temperature = float(self.engine_options.get("temperature", 0.7))

        config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            max_output_tokens=max_tokens,
            temperature=temperature,
        )
        response = await client.aio.models.generate_content(
            model=self.model,
            contents=user_prompt,
            config=config,
        )
        text = response.text or ""
        self._warn_if_abnormal_finish(
            self._extract_gemini_finish_reason(response),
            text_len=len(text),
            max_tokens=max_tokens,
        )
        return text

    # ------------------------------------------------------------------
    # finish_reason 検査 (応答途中切断の検知)
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_gemini_finish_reason(response: object) -> object | None:
        """google-genai レスポンスから finish_reason を抽出 (str or enum)。

        candidates[0].finish_reason に格納される。SDK バージョンによっては enum
        (FinishReason.MAX_TOKENS) で返ることがあるため、そのまま返して呼出側で
        str 化する。
        """
        try:
            candidates = getattr(response, "candidates", None) or []
            if not candidates:
                return None
            return getattr(candidates[0], "finish_reason", None)
        except Exception:
            return None

    def _warn_if_abnormal_finish(
        self,
        finish_reason: object | None,
        *,
        text_len: int,
        max_tokens: int,
    ) -> None:
        """finish_reason が正常系 (STOP) 以外なら WARNING ログ。

        MAX_TOKENS は max_output_tokens を引き上げるべきサイン。
        SAFETY / RECITATION 等は応答が安全フィルタで止められたサイン。
        """
        # enum でも str でも .name で取れることが多い
        reason_str = getattr(finish_reason, "name", None) or str(finish_reason or "")
        if reason_str in _NORMAL_FINISH_REASONS:
            return

        logger = setup_logger(f"worker.{self.persona_id}.{self.worker_id}")
        if reason_str.upper() in {"MAX_TOKENS", "LENGTH"}:
            logger.warning(
                "LLM response truncated by token limit: persona=%s worker=%s engine=%s"
                " finish_reason=%s text_len=%d max_tokens=%d"
                " (engine_options.max_output_tokens / max_completion_tokens を引き上げてください)",
                self.persona_id, self.worker_id, self.engine,
                reason_str, text_len, max_tokens,
            )
        else:
            logger.warning(
                "LLM response abnormal finish: persona=%s worker=%s engine=%s"
                " finish_reason=%s text_len=%d",
                self.persona_id, self.worker_id, self.engine,
                reason_str, text_len,
            )

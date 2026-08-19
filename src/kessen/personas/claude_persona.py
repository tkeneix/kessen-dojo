"""Claude Agent SDK を使うペルソナ実装。

PRD §2.2 利用マップ / §8 マルチプロバイダLLM に準拠:
- query() one-shot で in/ を読み out/ に書く
- setting_sources=[] で .claude/ 自動ロードを抑止
- system_prompt は base.compose_system_prompt() で組立
- 成果物のファイル生成はペルソナ自身が `Write` ツールで行う
  (生成ファイル名・様式はペルソナ description で指定。ペルソナの種別ごとに
  異なる出力構造を取れるよう自由度を確保。2026-05-06 設計レビュー)
"""

from __future__ import annotations

from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, query

from kessen.config.loader import parse_engine
from kessen.logging_config import setup_logger
from kessen.personas.base import BasePersona, PersonaResult
from kessen.personas.path_guard import build_path_guard_matchers, find_repo_root

_KNOWN_ALIASES = {"opus", "sonnet", "haiku", "inherit"}
_DEFAULT_ALLOWED_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep", "Bash"]


def _summarize_message(message: object) -> str | None:
    """SDK 受信メッセージを 1行で要約 (進捗可視化用)。

    SDK 内部型に依存しないよう duck typing で組立。`type_name + tool/text/thinking 概略`。
    `SystemMessage` のような細かな制御メッセージは None を返してログ抑止。
    """
    type_name = type(message).__name__
    parts: list[str] = []
    content = getattr(message, "content", None)
    if isinstance(content, list):
        for block in content:
            block_type = type(block).__name__
            if block_type == "ToolUseBlock":
                tool = getattr(block, "name", "?") or "?"
                # mcp__server__tool は最後のセグメントだけ
                short = tool.rsplit("__", 1)[-1] if "__" in tool else tool
                parts.append(f"tool={short}")
            elif block_type == "TextBlock":
                txt = (getattr(block, "text", "") or "").strip()
                if txt:
                    parts.append(f"text({len(txt)}c)")
            elif block_type == "ThinkingBlock":
                parts.append("thinking")
            elif block_type == "ToolResultBlock":
                parts.append("tool_result")
    if type_name == "ResultMessage":
        parts.append("FINAL")
    elif type_name == "SystemMessage":
        return None  # 細かい制御メッセージは出さない
    return f"{type_name}: {' '.join(parts)}" if parts else type_name


def to_claude_model_id(model_part: str) -> str:
    """engine文字列の model 部分を SDK が受け付ける ID に正規化。

    'opus' 等のエイリアスはそのまま、フルIDは prefix 補完。
    """
    if model_part in _KNOWN_ALIASES:
        return model_part
    if model_part.startswith("claude-"):
        return model_part
    return f"claude-{model_part}"


class ClaudePersona(BasePersona):
    """Claude Agent SDK 経由で動くペルソナ。"""

    base_prompt: str = (
        "あなたは kessen-dojo の参加ペルソナです。"
        "与えられた入力を読み、ペルソナの役割に沿った成果物を出力ディレクトリに書き出してください。"
    )

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        provider, model_part = parse_engine(self.engine)
        if provider != "claude":
            raise ValueError(
                f"ClaudePersona requires engine 'claude:*', got: {self.engine!r}"
            )
        self.model = to_claude_model_id(model_part)

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
        user_prompt = self.compose_user_prompt(in_dir, out_dir)

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
            "ClaudePersona run start: persona=%s model=%s in=%s out=%s",
            self.persona_id, self.model, in_dir, out_dir,
        )

        # claude CLI subprocess の stderr を収集する。例外発生時に末尾を ERROR
        # に出して根本原因の手がかりにする (post-FINAL exit code 1 noisy bug の調査用)。
        # LLMWorker._call_claude と同じパターン。
        stderr_lines: list[str] = []

        def _stderr_collector(line: str) -> None:
            line = line.rstrip("\n")
            if line:
                stderr_lines.append(line)
                logger.debug("[claude-cli stderr] %s", line)

        options = self._build_options(
            system_prompt, in_dir, stderr_callback=_stderr_collector,
        )

        final_text = ""
        msg_count = 0
        try:
            async for message in query(prompt=user_prompt, options=options):
                msg_count += 1
                summary = _summarize_message(message)
                if summary is not None:
                    logger.info("[turn %d] %s", msg_count, summary)
                # ResultMessage に最終テキストが入る
                if hasattr(message, "result") and message.result:
                    final_text = message.result
        except Exception as e:
            logger.exception("ClaudePersona run failed: %s", e)
            if stderr_lines:
                logger.error(
                    "claude CLI subprocess stderr (last %d of %d lines):",
                    min(30, len(stderr_lines)), len(stderr_lines),
                )
                for line in stderr_lines[-30:]:
                    logger.error("  | %s", line)
            else:
                logger.error("claude CLI subprocess stderr was empty")
            return PersonaResult(
                persona_id=self.persona_id,
                status="error",
                summary=f"runtime error: {e}",
                error=str(e),
            )

        # ペルソナが Write ツールで out_dir に書いたファイルを集計
        out_dir.mkdir(parents=True, exist_ok=True)
        output_files = [str(p.relative_to(out_dir)) for p in sorted(out_dir.rglob("*")) if p.is_file()]
        logger.info(
            "ClaudePersona run done: persona=%s msgs=%d files=%d",
            self.persona_id, msg_count, len(output_files),
        )
        return PersonaResult(
            persona_id=self.persona_id,
            status="ok",
            summary=final_text[:500] if final_text else f"completed ({msg_count} messages)",
            output_files=output_files,
        )

    # ------------------------------------------------------------------
    # internal
    # ------------------------------------------------------------------

    def _build_options(
        self,
        system_prompt: str,
        cwd_anchor: Path,
        *,
        stderr_callback: "object | None" = None,
    ) -> ClaudeAgentOptions:
        """ClaudeAgentOptions を engine_options から組立。

        engine_options.use_kessen_mcp = true のとき、kessen MCP server
        (`call_persona`/`call_worker` を提供) を Claude セッションに注入する。
        ペルソナ orchestrator が `mcp__kessen__call_worker` で配下 Worker
        を呼び出すために必要 (PRD §4.2.1, §11.2)。

        allowed_tools への `mcp__kessen__*` 追加は呼出側の責務 (engine_options
        で明示する)。サーバを注入しただけでは Claude は呼べない。

        ツール利用可能性のゲート (2026-05-06 修正):
          SDK の `allowed_tools` は **auto-allow リスト** であり、利用可否のゲートでは
          ない (types.py L1474-1479)。利用可否ゲートは `tools=` フィールド。
          そのため engine_options.allowed_tools から組み立てた built-in ツール集合を
          `tools=` にも渡し、Claude が context として持つビルトインツールを実際に
          絞り込む。MCP ツール (mcp__*) は `mcp_servers` 経由で別途注入されるため
          `tools=` には含めない (含めると SDK が built-in 名として解釈して落ちる)。

          副次効果: 旧来の挙動 (allowed_tools のみ指定) では permission_mode=acceptEdits
          により Glob/Grep/Bash が暗黙に通っていた問題を解消する。

        path_guard:
        既定で PreToolUse hook を注入し、shared/datawarehouse / docs 配下への
        Read/Glob/Grep/Edit/Write/Bash を deny する (kessen の信頼境界)。
        engine_options.disable_path_guard = true で無効化可能 (テスト用)。

        stderr_callback:
        指定された場合 ClaudeAgentOptions.stderr に渡す。run() が claude CLI
        subprocess の stderr を収集して例外調査に使う (post-FINAL exit 1 bug など)。
        """
        opts = self.config.engine_options

        cwd = cwd_anchor.parent.resolve()

        allowed_tools_list = list(opts.get("allowed_tools", _DEFAULT_ALLOWED_TOOLS))
        # SDK の `tools=` には built-in のみを渡す (MCP ツールは mcp_servers 側で注入)
        builtin_tools = [t for t in allowed_tools_list if not t.startswith("mcp__")]

        kwargs: dict = dict(
            system_prompt=system_prompt,
            tools=builtin_tools,                       # 利用可能性ゲート
            allowed_tools=allowed_tools_list,          # auto-allow (permission prompt 抑止)
            cwd=str(cwd),
            model=opts.get("model", self.model),
            max_turns=opts.get("max_turns", 12),
            permission_mode=opts.get("permission_mode", "acceptEdits"),
            setting_sources=[],   # .claude/ 自動ロードを抑止 (PRD §7.4)
        )

        if stderr_callback is not None:
            kwargs["stderr"] = stderr_callback

        if opts.get("use_kessen_mcp", False):
            # 遅延 import で循環依存 (registry → tools → ...) を回避
            from kessen.tools.registry import build_kessen_mcp_server

            kwargs["mcp_servers"] = {"kessen": build_kessen_mcp_server()}

        if not opts.get("disable_path_guard", False):
            repo_root = find_repo_root(cwd)
            kwargs["hooks"] = build_path_guard_matchers(
                repo_root=repo_root,
                cwd_anchor=cwd,
                logger=setup_logger(f"persona.{self.persona_id}.path_guard"),
            )

        return ClaudeAgentOptions(**kwargs)

"""非Claudeペルソナを Orchestrator(Claude) から呼ぶための MCPツールラッパ。

PRD §2.3.3 の persona_tool に該当。
Orchestrator (Claude SDK) からは `mcp__kessen__call_persona` として見える。

P3 段階では Orchestrator 自体は未実装だが、ツール定義は完成させてP5以降で配線可能にする。
"""

from __future__ import annotations

from pathlib import Path

from claude_agent_sdk import tool

from kessen.logging_config import setup_logger


@tool(
    "call_persona",
    "指定された persona_id のペルソナを実行し、in_dir を読んで out_dir に成果物を書き出す。"
    " Claude/OpenAI/Grok 問わず統一インターフェイスで呼出。"
    " 戻り値はペルソナ実行結果のサマリ (PersonaResult) を文字列化したもの。",
    {
        "persona_id": str,
        "project_dir": str,
        "repo_root": str,
        "in_dir": str,
        "out_dir": str,
    },
)
async def call_persona(args: dict) -> dict:
    """Orchestrator から渡される dict 引数 → ペルソナ実行 → MCPプロトコル戻り値。"""
    # 遅延importで循環依存を回避
    from kessen.personas.factory import build_persona

    logger = setup_logger("kessen.tools")
    persona_id = args["persona_id"]
    project_dir = Path(args["project_dir"])
    repo_root = Path(args["repo_root"])
    in_dir = Path(args["in_dir"])
    out_dir = Path(args["out_dir"])

    logger.info(
        "tool call_persona: id=%s project=%s in=%s out=%s",
        persona_id, project_dir, in_dir, out_dir,
    )

    try:
        persona = build_persona(persona_id, project_dir=project_dir, repo_root=repo_root)
        result = await persona.run(in_dir, out_dir)
    except Exception as e:
        logger.exception("call_persona failed: %s", e)
        return {
            "content": [{"type": "text", "text": f"ERROR: persona {persona_id} failed: {type(e).__name__}: {e}"}],
            "is_error": True,
        }

    text = (
        f"persona={persona_id} status={result.status} "
        f"summary={result.summary[:300]!r} "
        f"output_files={result.output_files}"
    )
    return {"content": [{"type": "text", "text": text}]}

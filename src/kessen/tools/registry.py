"""kessen MCP server 構築 (インプロセス)。

PRD §2.3.4 / §2.3.6 を実装:
- create_sdk_mcp_server で kessen 名前空間を1つ作る
- ツールは kessen 配下に集約 (persona_tool, workspace_tool 等)
- Orchestrator から `mcp_servers={"kessen": build_kessen_mcp_server()}` で登録
- ツール名は `mcp__kessen__<tool_name>` で公開される
"""

from __future__ import annotations

from claude_agent_sdk import create_sdk_mcp_server

from kessen.tools.persona_tool import call_persona
from kessen.tools.worker_tool import call_worker
from kessen.version import __version__


def build_kessen_mcp_server():
    """kessen の全 MCPツールを束ねた in-process サーバを返す。

    将来的に workspace_tool / evaluator_tool / knowledge_tool / runtime_tool が
    追加された際は、ここに足すだけで Orchestrator から使えるようになる。
    """
    return create_sdk_mcp_server(
        name="kessen",
        version=__version__,
        tools=[
            call_persona,
            call_worker,
            # P5: workspace_tool (prepare_round_dirs / seed_inputs_from_previous / list_round_artifacts)
            # P5: knowledge_tool  (append_feedback / append_lesson)
            # P6: runtime_tool    (run_container)
            # P5: evaluator_tool  (run_evaluator / write_leaderboard)
        ],
    )

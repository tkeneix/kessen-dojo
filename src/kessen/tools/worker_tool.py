"""ペルソナ orchestrator 配下の Worker (専門家) を呼出すための MCPツールラッパ。

Orchestrator (Claude Agent SDK) からは `mcp__kessen__call_worker` として見える。

責務分担:
  - tools/persona_tool.py:call_persona  → Round runner / 横断オーケストレーション 用 (ペルソナ単位呼出)
  - tools/worker_tool.py:call_worker    → ペルソナ内オーケストレーション用 (ペルソナ配下の Worker 呼出)

ペルソナの Claude セッションが in_dir に staging したファイル群を Worker に渡し、
Worker は単発 LLM 呼出で out_dir に成果物を書き出す。Worker 単体リトライは持たないため、
ここで起きた例外は is_error=True で返り、ペルソナ orchestrator (もしくは Round runner) の
リトライ機構に伝播することになる。
"""

from __future__ import annotations

from pathlib import Path

from claude_agent_sdk import tool

from kessen.logging_config import setup_logger


def _resolve_project_dir(raw: str, fallback_anchor: Path | None = None) -> Path:
    """orchestrator が渡してきた project_dir を案件ディレクトリに正規化する。

    ペルソナ Claude は cwd (`runs/{run-id}/round-N/{persona}/`) を見て project_dir を
    勘違いすることがある。`project.toml` を持つ祖先ディレクトリに自動的に遡って補正する。

    raw が相対パスの場合、Python プロセスの cwd で解決されるため Claudeセッションの
    cwd とずれる。fallback_anchor (in_dir) があれば、raw で見つからない場合に
    in_dir からも遡って project.toml を探す。

    探索順:
      1. raw 自体が project.toml を持っていればそのまま使う
      2. なければ raw の親方向に project.toml を探す (12 階層まで)
      3. fallback_anchor が指定されていれば、そこから親方向に project.toml を探す (12 階層まで)
      4. 見つからなければ raw をそのまま返す (load_worker_config 側で
         FileNotFoundError として扱われる)
    """

    def _walk_up(start: Path, levels: int = 12) -> Path | None:
        candidate = start
        for _ in range(levels):
            if (candidate / "project.toml").exists():
                return candidate
            if candidate.parent == candidate:
                break
            candidate = candidate.parent
        return None

    p = Path(raw).resolve()
    found = _walk_up(p)
    if found:
        return found

    if fallback_anchor is not None:
        found = _walk_up(fallback_anchor.resolve())
        if found:
            return found

    return p


@tool(
    "call_worker",
    "親ペルソナ (persona_id) 配下の Worker (worker_id) を呼出し、in_dir を読み out_dir に成果物を書き出す。"
    " engine は worker TOML の既定 + persona TOML の [workers.<id>] override で解決される。"
    " 戻り値は WorkerResult のサマリ (status / summary / output_files) を文字列化したもの。",
    {
        "worker_id": str,
        "persona_id": str,
        "project_dir": str,
        "in_dir": str,
        "out_dir": str,
    },
)
async def call_worker(args: dict) -> dict:
    """Orchestrator から渡される dict 引数 → Worker 実行 → MCPプロトコル戻り値。"""
    # 遅延importで循環依存を回避 (workers.factory → config.loader → ...)
    from kessen.workers.factory import build_worker

    logger = setup_logger("kessen.tools")
    worker_id = args["worker_id"]
    persona_id = args["persona_id"]
    raw_project_dir = args["project_dir"]
    in_dir = Path(args["in_dir"])
    project_dir = _resolve_project_dir(raw_project_dir, fallback_anchor=in_dir)
    out_dir = Path(args["out_dir"])

    if str(project_dir) != str(Path(raw_project_dir).resolve()):
        logger.info(
            "tool call_worker: project_dir auto-corrected from %s to %s",
            raw_project_dir,
            project_dir,
        )

    logger.info(
        "tool call_worker: worker=%s persona=%s project=%s in=%s out=%s",
        worker_id,
        persona_id,
        project_dir,
        in_dir,
        out_dir,
    )

    try:
        worker = build_worker(worker_id, persona_id=persona_id, project_dir=project_dir)
        result = await worker.execute(in_dir, out_dir)
    except Exception as e:
        logger.exception("call_worker failed: %s", e)
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"ERROR: worker {worker_id} (persona={persona_id}) failed: {type(e).__name__}: {e}",
                }
            ],
            "is_error": True,
        }

    is_error = result.status != "ok"
    text = (
        f"worker={worker_id} persona={persona_id} status={result.status} "
        f"summary={result.summary[:300]!r} "
        f"output_files={result.output_files}"
    )
    response: dict = {"content": [{"type": "text", "text": text}]}
    if is_error:
        response["is_error"] = True
    return response

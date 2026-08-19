"""ラウンド実行・状態取得・キャンセル・resume の REST エンドポイント。

PRD §9 REST API 準拠。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from kessen.api.state import AppState
from kessen.evaluator.base import Leaderboard
from kessen.logging_config import JST, setup_logger
from kessen.orchestrator.runner import _gen_run_id, run_competition

router = APIRouter(prefix="/projects/{project_name}/rounds", tags=["runs"])


def _get_state(request: Request) -> AppState:
    return request.app.state.kessen


StateDep = Annotated[AppState, Depends(_get_state)]


class StartRoundRequest(BaseModel):
    rounds: int | None = Field(default=None, description="project.toml の rounds を上書き")
    run_id: str | None = Field(default=None, description="run_id 明示指定 (省略時は自動発行)")


class StartRoundResponse(BaseModel):
    run_id: str
    project: str
    status: str
    project_dir: str
    total_rounds: int


def _resolve_project_dir(state: AppState, project_name: str) -> Path:
    """`projects/{name}/project.toml` の存在を確認してパスを返す。"""
    cand = state.repo_root / "projects" / project_name
    if not (cand / "project.toml").is_file():
        raise HTTPException(status_code=404, detail=f"project '{project_name}' not found at {cand}")
    return cand


# ---------------------------------------------------------------------------
# orchestration: background task wrapper
# ---------------------------------------------------------------------------


async def _run_with_state(
    state: AppState,
    run_id: str,
    project_dir: Path,
    rounds: int | None,
    from_round: int = 1,
) -> None:
    """run_competition を実行しつつDB状態を更新する非同期ラッパ。

    asyncio.create_task で起動される。終了時に tasks レジストリから自分を除去。
    """
    logger = setup_logger("kessen.api")
    logger.info("background run start: run_id=%s from_round=%d", run_id, from_round)
    state.repo.update_status(run_id, status="running", current_round=max(0, from_round - 1))

    async def _on_round_started(round_n: int) -> None:
        state.repo.update_status(run_id, current_round=round_n)

    async def _on_round_completed(round_n: int, leaderboard: Leaderboard) -> None:
        state.repo.save_leaderboard(run_id, leaderboard)

    try:
        result = await run_competition(
            project_dir,
            state.repo_root,
            rounds_override=rounds,
            run_id=run_id,
            from_round=from_round,
            on_round_started=_on_round_started,
            on_round_completed=_on_round_completed,
        )
        state.repo.update_status(
            run_id,
            status=result.status,
            finished_at=datetime.now(tz=JST).isoformat(timespec="seconds"),
            overall_winner=result.overall_winner,
        )
        logger.info("background run done: run_id=%s status=%s", run_id, result.status)
    except asyncio.CancelledError:
        logger.warning("background run cancelled: run_id=%s", run_id)
        state.repo.update_status(
            run_id,
            status="cancelled",
            finished_at=datetime.now(tz=JST).isoformat(timespec="seconds"),
        )
        raise
    except Exception as e:
        logger.exception("background run failed: run_id=%s", run_id)
        state.repo.update_status(
            run_id,
            status="failed",
            error=str(e)[:500],
            finished_at=datetime.now(tz=JST).isoformat(timespec="seconds"),
        )
    finally:
        state.cleanup_task(run_id)


# ---------------------------------------------------------------------------
# endpoints
# ---------------------------------------------------------------------------


@router.post("", response_model=StartRoundResponse, status_code=202)
async def start_round(
    project_name: str,
    body: StartRoundRequest,
    state: StateDep,
) -> StartRoundResponse:
    """新しい競技ランを起動。実行はバックグラウンドタスクで進む。"""
    project_dir = _resolve_project_dir(state, project_name)

    # rounds値: body指定 → なければ project.toml読込
    from kessen.config.loader import load_project_config
    project_cfg = load_project_config(project_dir)
    total = body.rounds if body.rounds is not None else project_cfg.project.rounds

    run_id = body.run_id or _gen_run_id()
    if state.repo.get_run(run_id) is not None:
        raise HTTPException(status_code=409, detail=f"run_id '{run_id}' already exists")

    state.repo.create_run(
        run_id=run_id,
        project=project_name,
        project_dir=project_dir,
        total_rounds=total,
        status="pending",
    )

    task = asyncio.create_task(_run_with_state(state, run_id, project_dir, body.rounds))
    state.register_task(run_id, task)

    return StartRoundResponse(
        run_id=run_id,
        project=project_name,
        status="pending",
        project_dir=str(project_dir),
        total_rounds=total,
    )


@router.get("")
async def list_runs(
    project_name: str,
    state: StateDep,
    status: str | None = None,
    limit: int = 50,
) -> dict:
    """案件のラン一覧 (started_at降順)。"""
    runs = state.repo.list_runs(project=project_name, status=status, limit=limit)
    return {"project": project_name, "runs": [r.to_dict() for r in runs]}


@router.get("/{run_id}")
async def get_run(
    project_name: str,
    run_id: str,
    state: StateDep,
) -> dict:
    """run の状態 + 各ラウンドのスコア。final.json があれば併せて返す。"""
    record = state.repo.get_run(run_id)
    if record is None or record.project != project_name:
        raise HTTPException(status_code=404, detail=f"run '{run_id}' not found")

    scores = [s.to_dict() for s in state.repo.get_round_scores(run_id)]

    # final.json があれば
    final_path = Path(record.project_dir) / "runs" / run_id / "final.json"
    final_payload = None
    if final_path.exists():
        try:
            final_payload = json.loads(final_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            final_payload = None

    return {
        "run": record.to_dict(),
        "round_scores": scores,
        "final": final_payload,
        "is_active": run_id in state.tasks and not state.tasks[run_id].done(),
    }


@router.get("/{run_id}/leaderboard")
async def get_run_leaderboard(
    project_name: str,
    run_id: str,
    state: StateDep,
    round: int | None = None,
) -> dict:
    """指定 run の全ラウンド (or round指定) の leaderboard.json をファイルから返す。"""
    record = state.repo.get_run(run_id)
    if record is None or record.project != project_name:
        raise HTTPException(status_code=404, detail=f"run '{run_id}' not found")

    run_dir = Path(record.project_dir) / "runs" / run_id

    def _load_one(round_n: int) -> dict | None:
        path = run_dir / f"round-{round_n}" / "leaderboard.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    if round is not None:
        payload = _load_one(round)
        if payload is None:
            raise HTTPException(status_code=404, detail=f"round {round} leaderboard not found")
        return {"run_id": run_id, "round": round, "leaderboard": payload}

    # 全ラウンド
    boards = []
    for r in range(1, record.total_rounds + 1):
        payload = _load_one(r)
        if payload is not None:
            boards.append(payload)
    return {"run_id": run_id, "leaderboards": boards}


class CancelResponse(BaseModel):
    run_id: str
    cancelled: bool
    message: str


@router.post("/{run_id}/cancel", response_model=CancelResponse)
async def cancel_run(
    project_name: str,
    run_id: str,
    state: StateDep,
) -> CancelResponse:
    record = state.repo.get_run(run_id)
    if record is None or record.project != project_name:
        raise HTTPException(status_code=404, detail=f"run '{run_id}' not found")
    if record.status not in ("pending", "running"):
        return CancelResponse(run_id=run_id, cancelled=False,
                              message=f"run is already {record.status}")
    cancelled = state.cancel_task(run_id)
    if not cancelled:
        # タスクがレジストリに無いケース: stale or 既に完了。状態だけ更新。
        state.repo.update_status(
            run_id, status="cancelled",
            finished_at=datetime.now(tz=JST).isoformat(timespec="seconds"),
        )
    return CancelResponse(
        run_id=run_id,
        cancelled=True,
        message="cancellation requested" if cancelled else "marked cancelled (no active task)",
    )


class ResumeResponse(BaseModel):
    run_id: str
    resumed_from_round: int
    status: str


@router.post("/{run_id}/resume", response_model=ResumeResponse)
async def resume_run(
    project_name: str,
    run_id: str,
    state: StateDep,
) -> ResumeResponse:
    """interrupted/cancelled/failed の run を current_round から再開。"""
    record = state.repo.get_run(run_id)
    if record is None or record.project != project_name:
        raise HTTPException(status_code=404, detail=f"run '{run_id}' not found")
    if record.status in ("running", "pending"):
        raise HTTPException(status_code=409, detail=f"run is currently {record.status}, cancel first")
    if record.status == "completed":
        raise HTTPException(status_code=409, detail="run already completed")

    # current_round が完了済 (leaderboard.json存在) なら次ラウンドから、未完なら同ラウンドから
    project_dir = Path(record.project_dir)
    next_round = max(1, record.current_round)
    leaderboard_path = project_dir / "runs" / run_id / f"round-{next_round}" / "leaderboard.json"
    if leaderboard_path.exists():
        next_round += 1
    if next_round > record.total_rounds:
        raise HTTPException(status_code=409, detail="all rounds already completed")

    task = asyncio.create_task(
        _run_with_state(state, run_id, project_dir, rounds=record.total_rounds, from_round=next_round)
    )
    state.register_task(run_id, task)
    return ResumeResponse(run_id=run_id, resumed_from_round=next_round, status="resumed")

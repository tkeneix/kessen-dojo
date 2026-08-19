"""Competition runner (multi-round)。

PRD §6 ラウンド実行フローの上位:
1. run-id 発行
2. project.toml ロード → Evaluator instantiate
3. ラウンドループ (run_round 呼出 + 勝者seed)
4. abort_run_on_all_failed 判定
5. final.json 出力
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from kessen.config.loader import (
    EvaluatorConfig,
    ProjectConfig,
    load_class,
    load_project_config,
)
from kessen.evaluator.base import BaseEvaluator, Leaderboard, LeaderboardEntry
from kessen.evaluator.scorers.stub import StubEvaluator
from kessen.knowledge.store import KnowledgeEntry, append as knowledge_append, read_all as knowledge_read_all
from kessen.logging_config import JST, setup_logger
from kessen.orchestrator.round import run_round

# 状態通知コールバック型 (P4でAPI状態DBと連携するために追加)
RoundStartedCallback = Callable[[int], Awaitable[None]]
RoundCompletedCallback = Callable[[int, Leaderboard], Awaitable[None]]


@dataclass
class CompetitionResult:
    """全ラウンド完了後の集計。"""

    run_id: str
    project: str
    status: str                                  # "completed" | "aborted"
    rounds_completed: int
    total_rounds: int
    aborted_at_round: int | None = None
    rounds: list[dict] = field(default_factory=list)   # 各 round の leaderboard 簡易版
    overall_winner: str | None = None
    overall_winner_round: int | None = None      # 勝者を出したラウンド番号
    overall_winner_score: float | None = None    # 勝者のスコア
    winner_artifacts: list[str] = field(default_factory=list)  # 勝者 out/ の絶対パス群
    ranking: list[dict] = field(default_factory=list)  # 全エントリ score降順 (ペルソナ × ラウンド)
    metadata: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# evaluator instantiation
# ---------------------------------------------------------------------------


def _instantiate_evaluator(
    eval_cfg: EvaluatorConfig | None,
    project_dir: Path,
) -> BaseEvaluator:
    """[evaluator] セクションから Evaluator を構築。未指定なら StubEvaluator。"""
    if eval_cfg is None:
        return StubEvaluator(project_dir=project_dir, config={})

    cls = load_class(eval_cfg.cls, project_dir=project_dir)
    if not issubclass(cls, BaseEvaluator):
        raise TypeError(f"{eval_cfg.cls!r} is not a BaseEvaluator subclass")

    extra = (eval_cfg.__pydantic_extra__ or {}).copy()
    return cls(project_dir=project_dir, config=extra)


# ---------------------------------------------------------------------------
# run-id / state
# ---------------------------------------------------------------------------


def _gen_run_id() -> str:
    """JST 現在時刻ベースで run_id を発行。例: '2026-05-01T09-15-30'。"""
    return datetime.now(tz=JST).strftime("%Y-%m-%dT%H-%M-%S")


def _write_state(run_dir: Path, **fields) -> None:
    state_path = run_dir / "state.json"
    if state_path.exists():
        try:
            current = json.loads(state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            current = {}
    else:
        current = {}
    current.update(fields)
    state_path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# main entrypoint
# ---------------------------------------------------------------------------


async def run_competition(
    project_dir: Path,
    repo_root: Path,
    *,
    rounds_override: int | None = None,
    run_id: str | None = None,
    from_round: int = 1,
    on_round_started: RoundStartedCallback | None = None,
    on_round_completed: RoundCompletedCallback | None = None,
) -> CompetitionResult:
    """1案件を最終結果まで走らせる。

    Args:
        project_dir: 案件ルート (project.toml のある場所)
        repo_root: kessen リポルート
        rounds_override: project.toml の rounds を上書き
        run_id: 指定があればそれを使う、無ければ自動発行
        from_round: 開始ラウンド (1-origin)。resume用。1未満ラウンドはskip。
        on_round_started: ラウンド開始時に呼ぶ非同期コールバック (round_n)。
        on_round_completed: ラウンド終了時に呼ぶ非同期コールバック (round_n, leaderboard)。

    Resume挙動:
        from_round以降のループ内で、`runs/{run-id}/round-N/leaderboard.json` が
        既に存在するラウンドはスキップ (既存leaderboardをロードして on_round_completed のみ呼ぶ)。
    """
    logger = setup_logger("kessen")
    project_dir = project_dir.resolve()
    repo_root = repo_root.resolve()
    project_cfg: ProjectConfig = load_project_config(project_dir)

    rounds = rounds_override if rounds_override is not None else project_cfg.project.rounds
    run_id = run_id or _gen_run_id()
    run_dir = project_dir / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    _write_state(
        run_dir,
        run_id=run_id,
        project=project_cfg.project.name,
        status="running",
        started_at=datetime.now(tz=JST).isoformat(timespec="seconds"),
        total_rounds=rounds,
        current_round=max(0, from_round - 1),
    )

    evaluator = _instantiate_evaluator(project_cfg.evaluator, project_dir)
    participants = list(project_cfg.participants.ids)
    max_parallel = project_cfg.project.max_parallel
    project_retry = project_cfg.retry

    logger.info(
        "run_competition: run_id=%s project=%s rounds=%d from_round=%d participants=%s parallel=%d",
        run_id, project_cfg.project.name, rounds, from_round, participants, max_parallel,
    )

    rounds_summary: list[dict] = []
    aborted_at: int | None = None
    cancelled = False
    # resumeで途中から始める場合、過去ラウンドのleaderboardもまずロード
    for r in range(1, from_round):
        existing = run_dir / f"round-{r}" / "leaderboard.json"
        if existing.exists():
            lb = _load_leaderboard(existing, round_n=r)
            rounds_summary.append(_lb_to_summary(lb))

    # 直前完了ラウンドの勝者を seed として再構築
    seed_dir: Path | None
    if from_round == 1:
        seed_dir = _initial_seed_dir(project_dir)
    elif rounds_summary:
        # from_round > 1 で過去結果がある場合: 直前ラウンドの勝者から seed 再構築
        seed_dir = _build_next_seed(
            run_dir, from_round - 1,
            _load_leaderboard(run_dir / f"round-{from_round - 1}" / "leaderboard.json", round_n=from_round - 1),
        )
    else:
        seed_dir = _initial_seed_dir(project_dir)

    for r in range(from_round, rounds + 1):
        _write_state(run_dir, current_round=r)
        if on_round_started is not None:
            try:
                await on_round_started(r)
            except Exception:
                logger.exception("on_round_started callback failed (run continues)")

        # resume: 既に完了しているラウンドはスキップ
        existing = run_dir / f"round-{r}" / "leaderboard.json"
        if existing.exists():
            logger.info("round %d: skipped (leaderboard already exists, resume)", r)
            leaderboard = _load_leaderboard(existing, round_n=r)
        else:
            try:
                leaderboard = await run_round(
                    round_n=r,
                    participants=participants,
                    project_dir=project_dir,
                    repo_root=repo_root,
                    run_dir=run_dir,
                    evaluator=evaluator,
                    project_retry=project_retry,
                    max_parallel=max_parallel,
                    seed_dir=seed_dir,
                )
            except asyncio.CancelledError:
                logger.warning("run_competition cancelled at round %d", r)
                cancelled = True
                # P1: cancel 時に partial round leaderboard が round.py 側で
                # フラッシュされていれば rounds_summary に積み、final.json/summary.md
                # を書き出してから raise する (数十分の作業を捨てない)。
                _flush_cancelled_run(
                    run_dir=run_dir,
                    project_name=project_cfg.project.name,
                    run_id=run_id,
                    rounds_total=rounds,
                    rounds_summary=rounds_summary,
                    cancelled_round=r,
                    logger=logger,
                )
                raise
            except Exception as e:
                logger.exception("round %d failed with unhandled exception", r)
                _write_state(run_dir, status="failed", error=str(e),
                             finished_at=datetime.now(tz=JST).isoformat(timespec="seconds"))
                raise

        rounds_summary.append(_lb_to_summary(leaderboard))

        if on_round_completed is not None:
            try:
                await on_round_completed(r, leaderboard)
            except Exception:
                logger.exception("on_round_completed callback failed (run continues)")

        # abort 判定
        if leaderboard.all_failed and project_retry.abort_run_on_all_failed:
            aborted_at = r
            logger.warning("run aborted at round %d (all participants failed)", r)
            break

        # 次ラウンドの seed
        seed_dir = _build_next_seed(run_dir, r, leaderboard)

    if cancelled:
        status = "cancelled"
    elif aborted_at:
        status = "aborted"
    else:
        status = "completed"
    winner_detail = _pick_overall_winner_detail(rounds_summary)
    if winner_detail is not None:
        overall_winner, overall_winner_round, overall_winner_score = winner_detail
        winner_artifacts = _collect_winner_artifacts(run_dir, overall_winner_round, overall_winner)
    else:
        overall_winner = None
        overall_winner_round = None
        overall_winner_score = None
        winner_artifacts = []
    ranking = _build_ranking(rounds_summary)

    result = CompetitionResult(
        run_id=run_id,
        project=project_cfg.project.name,
        status=status,
        rounds_completed=len(rounds_summary),
        total_rounds=rounds,
        aborted_at_round=aborted_at,
        rounds=rounds_summary,
        overall_winner=overall_winner,
        overall_winner_round=overall_winner_round,
        overall_winner_score=overall_winner_score,
        winner_artifacts=winner_artifacts,
        ranking=ranking,
    )

    (run_dir / "final.json").write_text(
        json.dumps(asdict(result), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _write_summary_md(run_dir, result)
    _write_run_history(project_dir, run_dir, result)
    _write_state(
        run_dir,
        status=status,
        finished_at=datetime.now(tz=JST).isoformat(timespec="seconds"),
        overall_winner=overall_winner,
    )
    logger.info("run_competition done: run_id=%s status=%s winner=%s", run_id, status, overall_winner)
    return result


# ---------------------------------------------------------------------------
# cancel flush (P1)
# ---------------------------------------------------------------------------


def _flush_cancelled_run(
    *,
    run_dir: Path,
    project_name: str,
    run_id: str,
    rounds_total: int,
    rounds_summary: list[dict],
    cancelled_round: int,
    logger,
) -> None:
    """cancel 時に partial 結果を final.json / summary.md / state.json にフラッシュ。

    - run_round が partial leaderboard.json を書いている場合は rounds_summary に積む
      (`metadata.cancelled` が立っているのでフル完走分とは識別できる)。
    - final.json は `status="cancelled"`, `aborted_at_round=cancelled_round` で書く。
    - _write_run_history は cancel 時には呼ばない (knowledge/common 汚染回避)。
    """
    fully_completed = len(rounds_summary)
    partial_lb_path = run_dir / f"round-{cancelled_round}" / "leaderboard.json"
    if partial_lb_path.exists():
        try:
            partial_lb = _load_leaderboard(partial_lb_path, round_n=cancelled_round)
            rounds_summary.append(_lb_to_summary(partial_lb))
        except Exception:
            logger.exception(
                "cancel flush: failed to load partial leaderboard %s (continuing)",
                partial_lb_path,
            )

    winner_detail = _pick_overall_winner_detail(rounds_summary)
    if winner_detail is not None:
        overall_winner, overall_winner_round, overall_winner_score = winner_detail
        winner_artifacts = _collect_winner_artifacts(
            run_dir, overall_winner_round, overall_winner,
        )
    else:
        overall_winner = None
        overall_winner_round = None
        overall_winner_score = None
        winner_artifacts = []
    ranking = _build_ranking(rounds_summary)

    cancel_result = CompetitionResult(
        run_id=run_id,
        project=project_name,
        status="cancelled",
        rounds_completed=fully_completed,
        total_rounds=rounds_total,
        aborted_at_round=cancelled_round,
        rounds=rounds_summary,
        overall_winner=overall_winner,
        overall_winner_round=overall_winner_round,
        overall_winner_score=overall_winner_score,
        winner_artifacts=winner_artifacts,
        ranking=ranking,
        metadata={"cancelled": True},
    )

    try:
        (run_dir / "final.json").write_text(
            json.dumps(asdict(cancel_result), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        _write_summary_md(run_dir, cancel_result)
    except Exception:
        logger.exception(
            "cancel flush: failed to write final.json/summary.md for run_id=%s",
            run_id,
        )

    _write_state(
        run_dir,
        status="cancelled",
        finished_at=datetime.now(tz=JST).isoformat(timespec="seconds"),
    )


# ---------------------------------------------------------------------------
# seeding
# ---------------------------------------------------------------------------


def _initial_seed_dir(project_dir: Path) -> Path | None:
    """Round1 の in/ にコピーする初期seedディレクトリ。

    `projects/{name}/task/` があればそれを使う (規約)。無ければ None。
    """
    cand = project_dir / "task"
    return cand if cand.exists() and cand.is_dir() else None


def _load_leaderboard(path: Path, round_n: int) -> Leaderboard:
    """既存 leaderboard.json をロード (resume用)。"""
    payload = json.loads(path.read_text(encoding="utf-8"))
    lb = Leaderboard(round=round_n)
    lb.winner = payload.get("winner")
    lb.all_failed = payload.get("all_failed", False)
    lb.metadata = payload.get("metadata", {})
    for e in payload.get("entries", []):
        lb.entries.append(LeaderboardEntry(
            persona_id=e["persona_id"],
            score=e.get("score"),
            status=e["status"],
            attempts=e.get("attempts", 1),
            note=e.get("note", ""),
            last_issues=e.get("last_issues", []),
            metrics=e.get("metrics", {}),
        ))
    return lb


def _lb_to_summary(lb: Leaderboard) -> dict:
    summary = {
        "round": lb.round,
        "winner": lb.winner,
        "all_failed": lb.all_failed,
        "entries": [
            {"persona_id": e.persona_id, "score": e.score, "status": e.status, "attempts": e.attempts}
            for e in lb.entries
        ],
    }
    # P1: cancel フラグを上位 (final.json / summary.md) に伝える
    if lb.metadata.get("cancelled"):
        summary["cancelled"] = True
    return summary


def _build_next_seed(run_dir: Path, completed_round: int, leaderboard: Leaderboard) -> Path | None:
    """次ラウンドの seed 用ディレクトリを作って返す。

    構成: runs/{run-id}/_seeds/round-{N+1}/
      - previous_winner/             (勝者の out/ を丸ごとコピー、勝者がいなければ無し)
      - previous_leaderboard.json    (直前ラウンドの leaderboard 生データ)
      - _leaderboard_summary.md      (run 内の全ラウンド推移 + note を md で要約。全 persona 共通)
    """
    next_seed = run_dir / "_seeds" / f"round-{completed_round + 1}"
    if next_seed.exists():
        import shutil
        shutil.rmtree(next_seed)
    next_seed.mkdir(parents=True, exist_ok=True)

    # leaderboard をコピー
    src_lb = run_dir / f"round-{completed_round}" / "leaderboard.json"
    if src_lb.exists():
        (next_seed / "previous_leaderboard.json").write_text(
            src_lb.read_text(encoding="utf-8"),
            encoding="utf-8",
        )

    # ラウンド推移の md 要約 (全 persona 共通視点)
    try:
        summary_md = _render_seed_leaderboard_summary(run_dir, completed_round)
        if summary_md:
            (next_seed / "_leaderboard_summary.md").write_text(summary_md, encoding="utf-8")
    except Exception:
        setup_logger("kessen").exception(
            "_build_next_seed: failed to render leaderboard_summary.md (continuing)"
        )

    # 勝者の out/ をコピー
    if leaderboard.winner:
        import shutil
        winner_out = run_dir / f"round-{completed_round}" / leaderboard.winner / "out"
        if winner_out.exists():
            shutil.copytree(winner_out, next_seed / "previous_winner")
    return next_seed


def _render_seed_leaderboard_summary(run_dir: Path, completed_round: int) -> str:
    """run 内 round-1 .. round-N の leaderboard.json を集約して md にする。

    全 persona に同じファイルが配られるので、視点は中立 (誰目線でもない)。
    各ラウンドの順位表 + note + ラウンド勝者を載せる。次ラウンド開始時、
    各 persona の in/_leaderboard_summary.md として読める。
    """
    lines: list[str] = []
    lines.append(f"# Leaderboard summary (rounds 1..{completed_round})")
    lines.append("")
    lines.append(
        "このファイルは、これまでのラウンドで全 persona がどう評価されたかの"
        "中立な要約。次ラウンドの方針検討の参考に。"
    )
    lines.append("")

    # ラウンド勝者の縦串
    lines.append("## Round winners")
    lines.append("")
    lines.append("| Round | Winner | Score |")
    lines.append("|---|---|---|")
    rounds_data: list[tuple[int, dict]] = []
    for r in range(1, completed_round + 1):
        lb_path = run_dir / f"round-{r}" / "leaderboard.json"
        if not lb_path.exists():
            continue
        try:
            data = json.loads(lb_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        rounds_data.append((r, data))
        winner = data.get("winner") or "—"
        winner_entry = next(
            (e for e in data.get("entries", []) if e.get("persona_id") == winner),
            None,
        )
        winner_score = winner_entry.get("score") if winner_entry else None
        score_str = f"{winner_score:.4f}" if winner_score is not None else "—"
        lines.append(f"| {r} | {winner} | {score_str} |")
    lines.append("")

    # 各ラウンドの順位表 + note
    for r, data in rounds_data:
        lines.append(f"## Round {r}")
        lines.append("")
        ranked = sorted(
            data.get("entries", []),
            key=lambda e: (-1 if e.get("score") is None else 0, -(e.get("score") or 0.0)),
        )
        if not ranked:
            lines.append("(no entries)")
            lines.append("")
            continue
        lines.append("| # | Persona | Score | Status | Attempts |")
        lines.append("|---|---|---|---|---|")
        for rank, e in enumerate(ranked, 1):
            score = e.get("score")
            score_str = f"{score:.4f}" if score is not None else "—"
            lines.append(
                f"| {rank} | {e.get('persona_id')} | {score_str} |"
                f" {e.get('status', '?')} | {e.get('attempts', 1)} |"
            )
        lines.append("")
        # 上位の note (winner のみ詳細、他は短縮)
        notes_block: list[str] = []
        for rank, e in enumerate(ranked, 1):
            note = (e.get("note") or "").strip()
            if not note:
                continue
            notes_block.append(f"- **{e.get('persona_id')}** (#{rank}): {note}")
        if notes_block:
            lines.append("### Notes")
            lines.append("")
            lines.extend(notes_block)
            lines.append("")

    return "\n".join(lines) + "\n"


def _pick_overall_winner_detail(
    rounds_summary: list[dict],
) -> tuple[str, int, float] | None:
    """全ラウンド全エントリで最もスコアが高いものを (persona_id, round, score) で返す。"""
    best: tuple[float, str, int] | None = None
    for rs in rounds_summary:
        for entry in rs["entries"]:
            score = entry.get("score")
            if score is None:
                continue
            if best is None or score > best[0]:
                best = (float(score), entry["persona_id"], rs["round"])
    if best is None:
        return None
    return (best[1], best[2], best[0])


def _pick_overall_winner(rounds_summary: list[dict]) -> str | None:
    """後方互換: persona_id だけ返す。詳細は `_pick_overall_winner_detail`。"""
    detail = _pick_overall_winner_detail(rounds_summary)
    return detail[0] if detail else None


def _collect_winner_artifacts(run_dir: Path, winner_round: int, winner_id: str) -> list[str]:
    """勝者ペルソナの out/ 配下のファイル絶対パスを収集 (主成果物優先で並べ替え)。"""
    out_dir = run_dir / f"round-{winner_round}" / winner_id / "out"
    if not out_dir.exists():
        return []
    files: list[str] = []
    for p in sorted(out_dir.rglob("*")):
        if not p.is_file():
            continue
        if p.name.startswith("_"):
            continue                     # _retry_feedback.md / _dry_run_*.md など
        if "__pycache__" in p.parts:
            continue
        files.append(str(p))

    # strategy.py / notes.md を優先表示
    def _key(path_str: str) -> tuple[int, str]:
        name = Path(path_str).name
        priority = {"strategy.py": 0, "notes.md": 1, "README.md": 2}.get(name, 3)
        return (priority, name)

    files.sort(key=_key)
    return files


def _build_ranking(rounds_summary: list[dict]) -> list[dict]:
    """全ラウンド全エントリを score 降順 (None は末尾) でフラット化。"""
    flat: list[dict] = []
    for rs in rounds_summary:
        for entry in rs["entries"]:
            flat.append(
                {
                    "persona_id": entry["persona_id"],
                    "round": rs["round"],
                    "score": entry.get("score"),
                    "status": entry.get("status", "ok"),
                }
            )
    flat.sort(key=lambda e: (-1 if e["score"] is None else 0, -(e["score"] or 0.0)))
    return flat


def _write_summary_md(run_dir: Path, result: CompetitionResult) -> None:
    """human-readable サマリを `summary.md` として書き出す。"""
    lines: list[str] = []
    lines.append(f"# Run Summary: {result.run_id}")
    lines.append("")
    lines.append(f"- Project: `{result.project}`")
    lines.append(f"- Status: **{result.status}**")
    lines.append(f"- Rounds: {result.rounds_completed} / {result.total_rounds}")
    if result.aborted_at_round:
        lines.append(f"- Aborted at round: {result.aborted_at_round}")
    if result.overall_winner:
        score_str = (
            f"{result.overall_winner_score:.4f}"
            if result.overall_winner_score is not None else "—"
        )
        lines.append(
            f"- Overall winner: **{result.overall_winner}** "
            f"(round {result.overall_winner_round}, score={score_str})"
        )
    lines.append("")

    lines.append("## Round winners")
    lines.append("")
    lines.append("| Round | Winner | Score |")
    lines.append("|---|---|---|")
    for rs in result.rounds:
        rn = rs["round"]
        winner = rs.get("winner") or "—"
        winner_score = next(
            (e.get("score") for e in rs["entries"] if e["persona_id"] == winner),
            None,
        )
        score_str = f"{winner_score:.4f}" if winner_score is not None else "—"
        if rs.get("cancelled"):
            marker = " (cancelled)"
        elif winner == result.overall_winner and rn == result.overall_winner_round:
            marker = " ← overall winner"
        else:
            marker = ""
        lines.append(f"| {rn} | {winner}{marker} | {score_str} |")
    lines.append("")

    if result.ranking:
        lines.append("## Overall ranking (all rounds × personas)")
        lines.append("")
        lines.append("| # | Persona | Round | Score | Status |")
        lines.append("|---|---|---|---|---|")
        for i, e in enumerate(result.ranking, 1):
            score_str = f"{e['score']:.4f}" if e["score"] is not None else "—"
            mark = " ← winner" if (i == 1 and e["status"] == "ok") else ""
            lines.append(
                f"| {i} | {e['persona_id']}{mark} | {e['round']} | {score_str} | {e['status']} |"
            )
        lines.append("")

    if result.winner_artifacts:
        lines.append("## Winner artifacts")
        lines.append("")
        for path in result.winner_artifacts:
            lines.append(f"- `{path}`")
        lines.append("")

    (run_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# run history (project-level knowledge)
# ---------------------------------------------------------------------------


def _write_run_history(
    project_dir: Path,
    run_dir: Path,
    result: CompetitionResult,
) -> None:
    """run 完了時に knowledge/common/leaderboard_history.jsonl に 1 エントリ追記。

    各 persona の最終ラウンド時点での score / note / 主成果物パス (project_dir 相対)
    を含む。次 run 開始時に retriever (common 層) 経由で全 persona の system_prompt
    に注入され、過去 run の総括が伝わる。

    冪等性: 同 run_id の entry が既存なら skip (resume / 再実行で重複させない)。
    """
    logger = setup_logger("kessen")
    history_path = project_dir / "knowledge" / "common" / "leaderboard_history.jsonl"

    # 同 run_id が既に記録済みなら skip
    if history_path.exists():
        try:
            for existing in knowledge_read_all(history_path):
                if existing.extra.get("run_id") == result.run_id:
                    logger.info(
                        "run_history: skip (already recorded for run_id=%s)",
                        result.run_id,
                    )
                    return
        except Exception:
            logger.exception("run_history: read_all failed; proceeding to append")

    # 最終ラウンド = rounds_summary 末尾
    if not result.rounds:
        logger.info("run_history: no rounds_summary; skip run_id=%s", result.run_id)
        return
    final_round = result.rounds[-1]
    final_round_n = int(final_round["round"])

    # 最終ラウンドの leaderboard.json から各 persona の note/metrics を取り、
    # rank 付きで整形 + out/ 配下の主成果物パスを relative で収集
    final_lb_path = run_dir / f"round-{final_round_n}" / "leaderboard.json"
    detailed_entries: list[dict] = []
    if final_lb_path.exists():
        try:
            lb_data = json.loads(final_lb_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.exception("run_history: failed to parse %s", final_lb_path)
            lb_data = {"entries": []}
        ranked = sorted(
            lb_data.get("entries", []),
            key=lambda e: (-1 if e.get("score") is None else 0, -(e.get("score") or 0.0)),
        )
        for rank, e in enumerate(ranked, 1):
            abs_paths = _collect_winner_artifacts(run_dir, final_round_n, e["persona_id"])
            rel_paths: list[str] = []
            for ap in abs_paths:
                try:
                    rel_paths.append(str(Path(ap).relative_to(project_dir)))
                except ValueError:
                    rel_paths.append(ap)
            # 戦略の目的・改善方針を metrics.strategy_summary から拾い、history へ伝搬
            # (Evaluator が strategy.py の strategy_summary() を呼んで格納している)。
            # 次 run のペルソナが retriever 経由で参照する。
            summary = (e.get("metrics") or {}).get("strategy_summary")
            detailed_entries.append({
                "rank": rank,
                "persona_id": e["persona_id"],
                "score": e.get("score"),
                "status": e.get("status"),
                "note": e.get("note", ""),
                "attempts": e.get("attempts", 1),
                "artifacts": rel_paths,
                "strategy_summary": summary,
            })

    # 人間が読む短いサマリ (retriever が "Recent Logs" として表示する内容)
    if result.overall_winner and result.overall_winner_score is not None:
        winner_part = (
            f"winner={result.overall_winner}"
            f" score={result.overall_winner_score:.3f}"
            f" round={result.overall_winner_round}"
        )
    else:
        winner_part = "winner=—"
    content_lines = [
        f"run_id={result.run_id} status={result.status}"
        f" rounds={result.rounds_completed}/{result.total_rounds} {winner_part}"
    ]
    for e in detailed_entries:
        score_str = f"{e['score']:.3f}" if e["score"] is not None else "—"
        line = f"  #{e['rank']} {e['persona_id']} score={score_str} status={e['status']}"
        if e["note"]:
            line += f" — {e['note']}"
        content_lines.append(line)
        # 戦略の目的・改善方針 (strategy_summary) があれば挿入。retriever が
        # 次 run のペルソナ system_prompt に注入し、改善検討の素材になる。
        if e.get("strategy_summary"):
            for s_line in str(e["strategy_summary"]).splitlines():
                content_lines.append(f"      | {s_line}")
    content = "\n".join(content_lines)

    extra = {
        "run_id": result.run_id,
        "project": result.project,
        "status": result.status,
        "rounds_completed": result.rounds_completed,
        "total_rounds": result.total_rounds,
        "overall_winner": result.overall_winner,
        "overall_winner_round": result.overall_winner_round,
        "overall_winner_score": result.overall_winner_score,
        "final_round": final_round_n,
        "entries": detailed_entries,
    }

    try:
        knowledge_append(
            history_path,
            KnowledgeEntry.now(
                layer="common",
                kind="run_summary",
                content=content,
                round=final_round_n,
                extra=extra,
            ),
        )
        logger.info(
            "run_history: wrote %s (run_id=%s)", history_path, result.run_id,
        )
    except Exception:
        logger.exception("run_history: failed to write %s", history_path)


# ---------------------------------------------------------------------------
# sync wrapper
# ---------------------------------------------------------------------------


def run_competition_sync(
    project_dir: Path,
    repo_root: Path,
    *,
    rounds_override: int | None = None,
    run_id: str | None = None,
) -> CompetitionResult:
    """同期ラッパ (CLIから呼ぶ用)。"""
    return asyncio.run(
        run_competition(
            project_dir,
            repo_root,
            rounds_override=rounds_override,
            run_id=run_id,
        )
    )

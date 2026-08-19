"""ラウンド実行 (orchestrator)。

PRD §6 / §6.1 / §6.2 を実装:
- 並列ペルソナ起動 (max_parallel)
- per-persona リトライループ (validate→retry→snapshot)
- attempts/ 階層保存 + retry_summary.json
- leaderboard.json 書出

Cancel 時の挙動 (P1, 2026-05-05 追加):
- run_persona_with_retry / run_round は asyncio.CancelledError を捕捉して
  書きかけの成果物 (attempts/attempt-N/out, retry_summary.json, partial leaderboard.json)
  をフラッシュしてから raise する。run_competition 側で final.json / summary.md
  を補完できるようにする。
"""

from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path

from kessen.config.loader import RetryConfig, load_persona_config, merge_retry
from kessen.evaluator.base import (
    BaseEvaluator,
    Leaderboard,
    LeaderboardEntry,
    ValidationResult,
)
from kessen.knowledge.store import KnowledgeEntry, append
from kessen.logging_config import setup_logger
from kessen.personas.base import BasePersona
from kessen.personas.factory import build_persona


@dataclass
class RetryResult:
    """1ペルソナの retry ループ結果。"""

    persona_id: str
    status: str                           # "ok" | "validation_failed" | "runtime_failed"
    attempts: int
    last_issues: list[str] = field(default_factory=list)
    last_validation: dict | None = None
    had_runtime_warning: bool = False     # PersonaResult.status="error" が 1回以上あった (P5)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _format_retry_feedback(attempt: int, max_attempts: int, history: list[list[str]]) -> str:
    """`_retry_feedback.md` の中身を組立。"""
    lines = [f"# Retry Feedback (attempt {attempt}/{max_attempts + 1})", ""]
    for i, issues in enumerate(history, start=1):
        lines.append(f"## attempt {i} で指摘された問題")
        for issue in issues:
            lines.append(f"- {issue}")
        lines.append("")
    lines.append("## 修正方針")
    lines.append("上記の問題をすべて解消してから今回の出力を行ってください。")
    return "\n".join(lines)


def _snapshot_attempt(
    attempt_dir: Path,
    validation: ValidationResult | None,
    error: str | None = None,
) -> None:
    """validation.json を attempt_dir に書き出す。"""
    payload = {
        "ok": validation.ok if validation else False,
        "issues": validation.issues if validation else ([error] if error else []),
        "suggestions": validation.suggestions if validation else [],
        "error": error,
    }
    (attempt_dir / "validation.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _promote_attempt_to_out(attempt_out: Path, persona_out: Path) -> None:
    """attempts/attempt-N/out/ の中身を persona_dir/out/ に複製 (上書き)。"""
    if persona_out.exists():
        shutil.rmtree(persona_out)
    persona_out.mkdir(parents=True, exist_ok=True)
    if attempt_out.exists():
        for src in attempt_out.iterdir():
            dst = persona_out / src.name
            if src.is_dir():
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)


# ---------------------------------------------------------------------------
# per-persona retry loop
# ---------------------------------------------------------------------------


async def run_persona_with_retry(
    persona: BasePersona,
    evaluator: BaseEvaluator,
    persona_dir: Path,
    retry_cfg: RetryConfig,
) -> RetryResult:
    """1ペルソナを retry 込みで実行。

    persona_dir 直下の構成:
      in/                          (呼出元が初期seedを置いていてもOK、_retry_feedback.md は実行中に追加)
      out/                         (最終attemptの成果物コピー)
      attempts/attempt-N/out/      (各試行の生成物)
      attempts/attempt-N/validation.json
      retry_summary.json
    """
    logger = setup_logger("kessen")
    in_dir = persona_dir / "in"
    out_dir = persona_dir / "out"
    attempts_dir = persona_dir / "attempts"
    in_dir.mkdir(parents=True, exist_ok=True)
    attempts_dir.mkdir(parents=True, exist_ok=True)

    accumulated_issues: list[list[str]] = []
    attempt = 0
    last_error: str | None = None
    any_had_warning = False  # P5: PersonaResult.status="error" が1回以上あった
    # cancel ハンドラ用に「現在進行中の attempt」を追跡
    current_attempt_dir: Path | None = None
    current_attempt_out: Path | None = None
    current_attempt_no: int = 0

    try:
        while True:
            attempt_no = attempt + 1
            attempt_dir = attempts_dir / f"attempt-{attempt_no}"
            attempt_out = attempt_dir / "out"
            attempt_out.mkdir(parents=True, exist_ok=True)
            current_attempt_dir = attempt_dir
            current_attempt_out = attempt_out
            current_attempt_no = attempt_no

            # retry の場合、_retry_feedback.md を in/ に書く (history付き)
            if attempt > 0 and retry_cfg.include_history_in_feedback:
                (in_dir / "_retry_feedback.md").write_text(
                    _format_retry_feedback(attempt_no, retry_cfg.max_attempts, accumulated_issues),
                    encoding="utf-8",
                )

            # ペルソナ実行
            try:
                result = await persona.run(in_dir, attempt_out)
                last_error = None
                # P5: ClaudePersona は SDK内部エラーを握りつぶして status="error" を返すことがある。
                # 成果物が書かれていれば validate に委ね、警告だけ記録する。
                if result is not None and getattr(result, "status", None) == "error":
                    any_had_warning = True
                    logger.warning(
                        "persona %s attempt %d returned status=error: %s (continuing to validate)",
                        persona.persona_id, attempt_no, getattr(result, "error", ""),
                    )
            except asyncio.CancelledError:
                raise  # 下の except で flush
            except Exception as e:
                logger.exception("persona %s attempt %d runtime error", persona.persona_id, attempt_no)
                last_error = f"{type(e).__name__}: {e}"
                _snapshot_attempt(attempt_dir, validation=None, error=last_error)
                if attempt >= retry_cfg.max_attempts:
                    _promote_attempt_to_out(attempt_out, out_dir)
                    _write_retry_summary(persona_dir, attempt_no, "runtime_failed")
                    return RetryResult(
                        persona_id=persona.persona_id,
                        status="runtime_failed",
                        attempts=attempt_no,
                        last_issues=[last_error],
                        had_runtime_warning=any_had_warning,
                    )
                accumulated_issues.append([f"runtime error: {last_error}"])
                attempt += 1
                continue

            # validate
            validation = await evaluator.validate(attempt_out)
            _snapshot_attempt(attempt_dir, validation=validation)

            if validation.ok:
                _promote_attempt_to_out(attempt_out, out_dir)
                _write_retry_summary(persona_dir, attempt_no, "ok")
                return RetryResult(
                    persona_id=persona.persona_id,
                    status="ok",
                    attempts=attempt_no,
                    last_validation=asdict(validation),
                    had_runtime_warning=any_had_warning,
                )

            accumulated_issues.append(validation.issues)
            if attempt >= retry_cfg.max_attempts:
                _promote_attempt_to_out(attempt_out, out_dir)
                _write_retry_summary(persona_dir, attempt_no, "validation_failed")
                return RetryResult(
                    persona_id=persona.persona_id,
                    status="validation_failed",
                    attempts=attempt_no,
                    last_issues=validation.issues,
                    last_validation=asdict(validation),
                    had_runtime_warning=any_had_warning,
                )
            attempt += 1
    except asyncio.CancelledError:
        # P1: cancel 時に書きかけ成果物をフラッシュしてから re-raise する。
        # 数十分かけた中間 attempt を捨てないために out/ への promote と
        # retry_summary.json (final_status="cancelled") を残す。
        attempt_no_for_flush = current_attempt_no or 1
        try:
            if current_attempt_dir is not None:
                _snapshot_attempt(
                    current_attempt_dir,
                    validation=None,
                    error="cancelled: run was cancelled mid-flight",
                )
            if current_attempt_out is not None:
                _promote_attempt_to_out(current_attempt_out, out_dir)
            _write_retry_summary(persona_dir, attempt_no_for_flush, "cancelled")
            logger.warning(
                "persona %s cancelled mid-flight at attempt %d (partial state flushed)",
                persona.persona_id, attempt_no_for_flush,
            )
        except Exception:
            logger.exception(
                "persona %s: failed to flush partial state during cancel",
                persona.persona_id,
            )
        raise


def _write_retry_summary(persona_dir: Path, total_attempts: int, final_status: str) -> None:
    (persona_dir / "retry_summary.json").write_text(
        json.dumps(
            {"total_attempts": total_attempts, "final_status": final_status},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# ナレッジ書き込み (P5 ラウンド間循環)
# ---------------------------------------------------------------------------


def _write_round_knowledge(project_dir: Path, round_n: int, leaderboard: Leaderboard) -> None:
    """ラウンド完了後に per-persona の lessons をナレッジストアに書き込む (PRD §7)。

    - 各ペルソナの成功/失敗ログ → knowledge/per-persona/{id}/lessons.jsonl (layer=private)

    次ラウンド開始時に KnowledgeRetriever が system_prompt に注入するため、
    ペルソナは前ラウンドの結果を踏まえて出力を改善できる。

    **2026-05-06 廃止**: 勝者の round-level feedback を knowledge/common/feedback.jsonl
    に書く経路は削除。run 完了時に書かれる knowledge/common/leaderboard_history.jsonl
    (`runner._write_run_history`) が全エントリ + strategy_summary を含み上位互換になった
    ため。中間ラウンドの推移は `_seeds/round-N/_leaderboard_summary.md` で staging される。
    """
    logger = setup_logger("kessen")

    # 各ペルソナの結果 → per-persona/{id}/lessons.jsonl
    for entry in leaderboard.entries:
        lessons_path = (
            project_dir / "knowledge" / "per-persona" / entry.persona_id / "lessons.jsonl"
        )
        if entry.status == "ok" and entry.score is not None:
            content = (
                f"round={round_n} score={entry.score:.3f} attempts={entry.attempts}"
            )
            if entry.note:
                content += f"\n{entry.note}"
            kind = "lesson"
        else:
            content = (
                f"round={round_n} status={entry.status} attempts={entry.attempts}"
            )
            if entry.last_issues:
                content += f"\nlast_issues: {', '.join(entry.last_issues)}"
            kind = entry.status  # "validation_failed" | "runtime_failed"

        try:
            append(
                lessons_path,
                KnowledgeEntry.now(
                    layer="private",
                    kind=kind,
                    content=content,
                    persona_id=entry.persona_id,
                    round=round_n,
                ),
            )
        except Exception:
            logger.exception(
                "failed to write lessons for persona %s round %d",
                entry.persona_id, round_n,
            )


# ---------------------------------------------------------------------------
# round runner (parallel)
# ---------------------------------------------------------------------------


async def run_round(
    *,
    round_n: int,
    participants: list[str],
    project_dir: Path,
    repo_root: Path,
    run_dir: Path,
    evaluator: BaseEvaluator,
    project_retry: RetryConfig,
    max_parallel: int,
    seed_dir: Path | None = None,
) -> Leaderboard:
    """1ラウンド実行 → leaderboard.json 書出して Leaderboard 返却。

    Args:
        round_n: ラウンド番号 (1-origin)
        participants: 参加ペルソナID一覧
        project_dir: 案件ルート
        repo_root: kessen リポルート (ナレッジ shared/ 解決用)
        run_dir: runs/{run-id}/ 絶対パス
        evaluator: ラウンド評価器
        project_retry: 案件側 [retry] 設定 (persona側で上書き可)
        max_parallel: 同時並列数
        seed_dir: 各 persona の in/ にseedとしてコピーする中身ディレクトリ (None なら何もしない)
    """
    logger = setup_logger("kessen")
    round_dir = run_dir / f"round-{round_n}"
    round_dir.mkdir(parents=True, exist_ok=True)

    # 各 persona ディレクトリと in/ を準備 (seed反映)
    for pid in participants:
        persona_in = round_dir / pid / "in"
        persona_in.mkdir(parents=True, exist_ok=True)
        if seed_dir is not None and seed_dir.exists():
            for src in seed_dir.iterdir():
                dst = persona_in / src.name
                if src.is_dir():
                    if dst.exists():
                        shutil.rmtree(dst)
                    shutil.copytree(src, dst)
                else:
                    shutil.copy2(src, dst)

    sem = asyncio.Semaphore(max_parallel)

    async def _one(pid: str) -> RetryResult:
        async with sem:
            persona_cfg = load_persona_config(project_dir, pid)
            retry_cfg = merge_retry(project_retry, persona_cfg.retry)
            persona = build_persona(pid, project_dir=project_dir, repo_root=repo_root)
            persona_dir = round_dir / pid
            logger.info("round %d: starting persona %s (engine=%s)", round_n, pid, persona.engine)
            return await run_persona_with_retry(persona, evaluator, persona_dir, retry_cfg)

    # P1: cancel 時に partial leaderboard を書けるよう、タスクを explicit に作って
    # asyncio.gather() に渡す。cancel が来た場合、各タスクの完了/cancel を区別して
    # 書き出し可能なものから partial leaderboard.json を構築する。
    tasks: dict[str, asyncio.Task[RetryResult]] = {
        pid: asyncio.create_task(_one(pid)) for pid in participants
    }
    try:
        retry_results: list[RetryResult] = await asyncio.gather(*tasks.values())
    except asyncio.CancelledError:
        # 子タスク (各ペルソナ) は CancelledError を受けて自前で out/, retry_summary.json
        # をフラッシュしてから raise してくる。すべての子の cancel-cleanup が落ち着くまで待つ。
        await asyncio.gather(*tasks.values(), return_exceptions=True)
        partial = _build_partial_leaderboard_on_cancel(round_n, participants, tasks, round_dir)
        try:
            partial.write(round_dir / "leaderboard.json")
            logger.warning(
                "round %d cancelled: partial leaderboard written (entries=%d)",
                round_n, len(partial.entries),
            )
        except Exception:
            logger.exception(
                "round %d: failed to write partial leaderboard during cancel", round_n,
            )
        raise

    # ok の personas だけ score
    ok_pids = [r.persona_id for r in retry_results if r.status == "ok"]
    leaderboard = Leaderboard(round=round_n)
    if ok_pids:
        scored = await evaluator.evaluate(round_dir, ok_pids, round_n=round_n)
        # attempts と had_runtime_warning を retry結果から差し替え/注入
        attempts_map = {r.persona_id: r.attempts for r in retry_results}
        warning_map = {r.persona_id: r.had_runtime_warning for r in retry_results}
        for entry in scored.entries:
            entry.attempts = attempts_map.get(entry.persona_id, entry.attempts)
            if warning_map.get(entry.persona_id):
                entry.metrics["had_runtime_warning"] = True  # P5
        leaderboard.entries.extend(scored.entries)

    # 失敗 personas を追加
    for r in retry_results:
        if r.status != "ok":
            leaderboard.add_failed(
                r.persona_id,
                status=r.status,         # type: ignore[arg-type]
                attempts=r.attempts,
                last_issues=r.last_issues,
            )

    leaderboard.finalize()
    # P5: ラウンド完了後にナレッジを書き込む (次ラウンドの system_prompt に自動注入される)
    _write_round_knowledge(project_dir, round_n, leaderboard)
    leaderboard.write(round_dir / "leaderboard.json")
    logger.info(
        "round %d done: winner=%s all_failed=%s entries=%d",
        round_n, leaderboard.winner, leaderboard.all_failed, len(leaderboard.entries),
    )
    return leaderboard


# ---------------------------------------------------------------------------
# cancel helpers (P1)
# ---------------------------------------------------------------------------


def _count_attempt_dirs(attempts_dir: Path) -> int:
    """attempts/attempt-* の数を数える (cancel 時の attempts 推定用)。"""
    if not attempts_dir.exists():
        return 0
    return sum(
        1 for p in attempts_dir.iterdir()
        if p.is_dir() and p.name.startswith("attempt-")
    )


def _build_partial_leaderboard_on_cancel(
    round_n: int,
    participants: list[str],
    tasks: dict[str, asyncio.Task[RetryResult]],
    round_dir: Path,
) -> Leaderboard:
    """cancel 時に提出する partial Leaderboard を構築する。

    - 完了済みタスク: その RetryResult を反映。`status="ok"` の persona は score
      を取らずに `score=None` のまま記録する (cancel 中は重い score() を走らせない)。
    - 未完了/cancel タスク: ディスクの attempts/ から attempts 数だけ推定して
      `status="cancelled"` で記録。
    """
    leaderboard = Leaderboard(round=round_n, metadata={"cancelled": True})
    for pid in participants:
        task = tasks.get(pid)
        result: RetryResult | None = None
        if task is not None and task.done() and not task.cancelled():
            try:
                if task.exception() is None:
                    result = task.result()
            except asyncio.CancelledError:
                result = None
            except Exception:
                result = None

        if result is None:
            attempts_count = _count_attempt_dirs(round_dir / pid / "attempts")
            leaderboard.entries.append(
                LeaderboardEntry(
                    persona_id=pid,
                    score=None,
                    status="cancelled",
                    attempts=max(1, attempts_count),
                    note="cancelled mid-flight",
                    last_issues=["run cancelled mid-flight"],
                )
            )
            continue

        if result.status == "ok":
            # validate=ok まで到達したが、cancel 中なので score() は走らせない。
            # entries には score=None で残し、note でその旨を明記。
            leaderboard.entries.append(
                LeaderboardEntry(
                    persona_id=result.persona_id,
                    score=None,
                    status="ok",
                    attempts=result.attempts,
                    note="validated; not scored due to cancellation",
                    metrics=(
                        {"had_runtime_warning": True}
                        if result.had_runtime_warning else {}
                    ),
                )
            )
        else:
            leaderboard.add_failed(
                result.persona_id,
                status=result.status,  # type: ignore[arg-type]
                attempts=result.attempts,
                last_issues=result.last_issues,
            )

    leaderboard.finalize()
    # cancel は all_failed の概念とは別物 (作業途中で打ち切られただけ)。
    leaderboard.all_failed = False
    leaderboard.winner = None
    leaderboard.metadata["cancelled"] = True
    return leaderboard

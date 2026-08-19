"""kessen CLI エントリポイント。

P1 段階で `persona-run` を追加。P2以降で run-once / evaluator-run / knowledge-show 等を追加。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from pathlib import Path

import click

from kessen.auth.resolver import (
    ClaudeAuthMode,
    resolve_claude_auth,
    resolve_openai_key,
    resolve_xai_key,
)
from kessen.knowledge.retriever import KnowledgeRetriever
from kessen.logging_config import setup_logger
from kessen.orchestrator.runner import run_competition_sync
from kessen.personas.factory import build_persona
from kessen.version import __version__


def _repo_root() -> Path:
    """リポジトリのルート（kessen インストール元）を推定。

    1. 環境変数 KESSEN_REPO_ROOT
    2. CWD が pyproject.toml を持つならそこ
    3. CWD のまま
    """
    import os
    env = os.environ.get("KESSEN_REPO_ROOT")
    if env:
        return Path(env).resolve()
    cwd = Path.cwd()
    if (cwd / "pyproject.toml").exists():
        return cwd.resolve()
    return cwd.resolve()


@click.group()
@click.version_option(version=__version__, prog_name="kessen")
def cli() -> None:
    """kessen-dojo: 複数ペルソナのAIエージェントが評価指標で競い合うコンペ実行基盤。"""


@cli.command()
def info() -> None:
    """インストール状態と認証モードを表示。"""
    logger = setup_logger("kessen")
    logger.info("kessen info invoked")

    claude = resolve_claude_auth()
    openai_key = resolve_openai_key()
    xai_key = resolve_xai_key()

    click.echo(f"kessen-dojo v{__version__}")
    click.echo("")
    click.echo("[Claude]")
    click.echo(f"  mode: {claude.mode.value}")
    if claude.mode is ClaudeAuthMode.API_KEY:
        click.echo(f"  api_key: {claude.masked_api_key()}")
    elif claude.mode is ClaudeAuthMode.SUBSCRIPTION:
        click.echo(f"  cli_path: {claude.cli_path}")
    else:
        click.echo("  (未認証: ANTHROPIC_API_KEY もしくは claude CLI ログインが必要)")

    click.echo("")
    click.echo("[OpenAI]")
    click.echo(f"  api_key: {'設定済み' if openai_key else '未設定'}")

    click.echo("")
    click.echo("[xAI (Grok)]")
    click.echo(f"  api_key: {'設定済み' if xai_key else '未設定'}")


@cli.command("persona-run")
@click.option("--project", "project_path", required=True, type=click.Path(exists=True, file_okay=False, path_type=Path),
              help="案件ディレクトリ (project.toml のある場所)")
@click.option("--persona", "persona_id", required=True, type=str,
              help="起動するペルソナID")
@click.option("--in-dir", "in_dir", required=True, type=click.Path(path_type=Path),
              help="入力ディレクトリ")
@click.option("--out-dir", "out_dir", required=True, type=click.Path(path_type=Path),
              help="出力ディレクトリ")
@click.option("--dry-run", is_flag=True, default=False,
              help="LLMを呼ばずにプロンプトだけ out_dir に書き出す")
@click.option("--repo-root", "repo_root", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None, help="リポジトリルート (省略時は環境変数/CWDから推定)")
def persona_run(
    project_path: Path,
    persona_id: str,
    in_dir: Path,
    out_dir: Path,
    dry_run: bool,
    repo_root: Path | None,
) -> None:
    """指定ペルソナを単発実行 (P1)。

    例:
      kessen persona-run --project projects/_sample --persona echo-claude \\
        --in-dir /tmp/in --out-dir /tmp/out --dry-run
    """
    logger = setup_logger("kessen")
    project_dir = project_path.resolve()
    root = (repo_root or _repo_root()).resolve()

    in_dir = in_dir.resolve()
    out_dir = out_dir.resolve()
    in_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    logger.info(
        "persona-run: project=%s persona=%s in=%s out=%s dry_run=%s",
        project_dir, persona_id, in_dir, out_dir, dry_run,
    )

    try:
        persona = build_persona(persona_id, project_dir=project_dir, repo_root=root)
    except Exception as e:
        logger.exception("ペルソナ構築失敗: %s", e)
        click.echo(f"ERROR: {e}", err=True)
        raise SystemExit(1) from e

    try:
        result = asyncio.run(persona.run(in_dir, out_dir, dry_run=dry_run))
    except Exception as e:
        logger.exception("ペルソナ実行失敗: %s", e)
        click.echo(f"ERROR: {e}", err=True)
        raise SystemExit(2) from e

    click.echo(json.dumps(asdict(result), ensure_ascii=False, indent=2))
    if result.status == "error":
        raise SystemExit(3)


@cli.command("run-once")
@click.option("--project", "project_path", required=True, type=click.Path(exists=True, file_okay=False, path_type=Path),
              help="案件ディレクトリ (project.toml のある場所)")
@click.option("--rounds", "rounds", type=int, default=None,
              help="ラウンド数を上書き (省略時は project.toml の rounds)")
@click.option("--run-id", "run_id", type=str, default=None,
              help="run-id を明示指定 (省略時はJST時刻ベースで自動発行)")
@click.option("--repo-root", "repo_root", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None, help="リポジトリルート")
def run_once(
    project_path: Path,
    rounds: int | None,
    run_id: str | None,
    repo_root: Path | None,
) -> None:
    """1案件のコンペを最終結果まで走らせる (P2)。

    例:
      kessen run-once --project projects/_sample
      kessen run-once --project projects/_sample --rounds 1
    """
    logger = setup_logger("kessen")
    project_dir = project_path.resolve()
    root = (repo_root or _repo_root()).resolve()
    logger.info("run-once: project=%s rounds=%s run_id=%s", project_dir, rounds, run_id)

    try:
        result = run_competition_sync(
            project_dir, root, rounds_override=rounds, run_id=run_id,
        )
    except Exception as e:
        logger.exception("run-once failed: %s", e)
        click.echo(f"ERROR: {e}", err=True)
        raise SystemExit(2) from e

    click.echo(_format_run_summary(result))
    if result.status == "aborted":
        raise SystemExit(3)


def _format_run_summary(result) -> str:
    """run-once 末尾用の人間可読サマリ。final.json/summary.md と同じ情報をターミナル整形。"""
    bar = "=" * 64
    out: list[str] = []
    out.append(bar)
    out.append(
        f"Run: {result.run_id}  status={result.status}  "
        f"({result.rounds_completed}/{result.total_rounds} rounds)"
    )
    if result.overall_winner:
        score_str = (
            f"{result.overall_winner_score:+.4f}"
            if result.overall_winner_score is not None else "—"
        )
        out.append(
            f"Overall winner: {result.overall_winner}  "
            f"(round {result.overall_winner_round}, score={score_str})"
        )
    out.append(bar)
    out.append("")

    # Round winners
    out.append("Round winners:")
    for rs in result.rounds:
        rn = rs["round"]
        winner = rs.get("winner") or "—"
        winner_score = next(
            (e.get("score") for e in rs["entries"] if e["persona_id"] == winner),
            None,
        )
        score_str = f"{winner_score:+.4f}" if winner_score is not None else "  —    "
        marker = " ← overall winner" if (
            winner == result.overall_winner and rn == result.overall_winner_round
        ) else ""
        out.append(f"  round {rn}: {winner:18s} (score={score_str}){marker}")
    out.append("")

    # Overall ranking
    if result.ranking:
        out.append("Overall ranking (all rounds × personas, sorted by score desc):")
        for i, e in enumerate(result.ranking, 1):
            score_str = f"{e['score']:+.4f}" if e["score"] is not None else "   —   "
            mark = " ← winner" if (i == 1 and e["status"] == "ok") else ""
            status_str = "" if e["status"] == "ok" else f"  [{e['status']}]"
            out.append(
                f"  {i:2d}. {e['persona_id']:18s} r{e['round']}  "
                f"score={score_str}{status_str}{mark}"
            )
        out.append("")

    # Winner artifacts
    if result.winner_artifacts:
        out.append("Winner artifacts:")
        for path in result.winner_artifacts:
            out.append(f"  {path}")
        out.append("")

    out.append(f"Detail: runs/{result.run_id}/final.json (machine) / summary.md (human)")
    return "\n".join(out)


@cli.command("serve")
@click.option("--host", default="127.0.0.1", show_default=True, help="bind host")
@click.option("--port", default=8788, show_default=True, type=int, help="bind port")
@click.option("--db", "db_path", type=click.Path(path_type=Path), default=None,
              help="SQLite状態DBファイル (省略時は ./runs.db、KESSEN_DB_PATH 環境変数でも指定可)")
@click.option("--repo-root", "repo_root", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None, help="リポジトリルート (project検索の起点、省略時は CWD/環境変数)")
@click.option("--reload", is_flag=True, default=False, help="auto-reload (開発用)")
def serve(host: str, port: int, db_path: Path | None, repo_root: Path | None, reload: bool) -> None:
    """REST API サーバを常駐起動 (P4)。

    例:
      kessen serve
      kessen serve --port 9000 --db /tmp/runs.db
      curl -X POST http://localhost:8788/projects/_sample/rounds
    """
    import os

    import uvicorn

    if db_path is not None:
        os.environ["KESSEN_DB_PATH"] = str(db_path.resolve())
    if repo_root is not None:
        os.environ["KESSEN_REPO_ROOT"] = str(repo_root.resolve())

    logger = setup_logger("kessen")
    logger.info("kessen serve: host=%s port=%d db=%s repo_root=%s",
                host, port, db_path or os.environ.get("KESSEN_DB_PATH", "./runs.db"),
                repo_root or os.environ.get("KESSEN_REPO_ROOT", str(Path.cwd())))

    uvicorn.run(
        "kessen.api:app_factory",
        factory=True,
        host=host,
        port=port,
        reload=reload,
        log_level="info",
    )


@cli.command("knowledge-show")
@click.option("--project", "project_path", required=True, type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--persona", "persona_id", required=True, type=str)
@click.option("--repo-root", "repo_root", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
def knowledge_show(project_path: Path, persona_id: str, repo_root: Path | None) -> None:
    """指定ペルソナに注入される階層マージ済みナレッジを表示 (debug用)。"""
    project_dir = project_path.resolve()
    root = (repo_root or _repo_root()).resolve()
    retriever = KnowledgeRetriever(root)
    text = retriever.compose(persona_id, project_dir)
    if not text:
        click.echo("(no knowledge to inject — all layers empty)")
    else:
        click.echo(text)


if __name__ == "__main__":
    cli()

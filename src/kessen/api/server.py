"""FastAPI アプリ factory + lifespan。

CLI `kessen serve` から uvicorn 経由で起動される。
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from kessen.api.routes import health, runs
from kessen.api.state import AppState
from kessen.logging_config import setup_logger
from kessen.state import RunRepository, init_db
from kessen.version import __version__

DEFAULT_DB_PATH = "runs.db"
ENV_DB_PATH = "KESSEN_DB_PATH"
ENV_REPO_ROOT = "KESSEN_REPO_ROOT"


def _resolve_db_path() -> Path:
    return Path(os.environ.get(ENV_DB_PATH, DEFAULT_DB_PATH)).resolve()


def _resolve_repo_root() -> Path:
    val = os.environ.get(ENV_REPO_ROOT)
    if val:
        return Path(val).resolve()
    cwd = Path.cwd()
    if (cwd / "pyproject.toml").exists():
        return cwd.resolve()
    return cwd.resolve()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger = setup_logger("kessen.api")
    db_path = _resolve_db_path()
    repo_root = _resolve_repo_root()
    logger.info("kessen api starting: db=%s repo_root=%s", db_path, repo_root)

    conn = init_db(db_path)
    repo = RunRepository(conn)
    stale = repo.cleanup_stale_running()
    if stale:
        logger.warning("marked %d stale 'running'/'pending' runs as 'interrupted'", stale)

    state = AppState(conn=conn, repo=repo, repo_root=repo_root, db_path=db_path)
    app.state.kessen = state

    try:
        yield
    finally:
        logger.info("kessen api shutting down")
        await state.shutdown()


def create_app(*, repo_root: Path | None = None, db_path: Path | None = None) -> FastAPI:
    """FastAPI app factory。

    Args:
        repo_root: 指定があれば環境変数より優先 (テスト用)
        db_path: 同上
    """
    if repo_root is not None:
        os.environ[ENV_REPO_ROOT] = str(repo_root)
    if db_path is not None:
        os.environ[ENV_DB_PATH] = str(db_path)

    app = FastAPI(
        title="kessen-dojo",
        version=__version__,
        description="マルチペルソナAIエージェント競技基盤 REST API",
        lifespan=lifespan,
    )
    app.include_router(health.router)
    app.include_router(runs.router)
    return app


# uvicorn の `--factory` で参照する場合
def app_factory() -> FastAPI:
    return create_app()

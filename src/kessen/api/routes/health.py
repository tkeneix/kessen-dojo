"""ヘルスチェック / メタ情報。"""

from __future__ import annotations

from fastapi import APIRouter

from kessen.version import __version__

router = APIRouter(tags=["meta"])


@router.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": __version__}

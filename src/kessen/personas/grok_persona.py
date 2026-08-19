"""Grok (xAI) ペルソナ。

xAI API は OpenAI互換のため `openai` SDK を流用、base_url のみ差し替え。
"""

from __future__ import annotations

from kessen.auth.resolver import require_xai_key
from kessen.personas.openai_persona import OpenAIPersona

XAI_BASE_URL = "https://api.x.ai/v1"


class GrokPersona(OpenAIPersona):
    """xAI (Grok) を openai 互換APIで叩くペルソナ。"""

    base_prompt: str = (
        "あなたは kessen-dojo の参加ペルソナ (Grok) です。"
        "与えられた入力ファイル群を読み、ペルソナの役割に沿った成果物を生成してください。"
    )
    provider_name: str = "xai"

    def _make_client(self):
        from openai import AsyncOpenAI
        return AsyncOpenAI(api_key=require_xai_key(), base_url=XAI_BASE_URL)

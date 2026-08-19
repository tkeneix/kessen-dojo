"""認証情報リゾルバ。

優先順位 (Claude):
  1. ANTHROPIC_API_KEY が設定済み → APIキー経路
  2. claude CLI が PATH にある → サブスクリプション認証を継承（CLIログイン状態の検証は SDK 任せ）
  3. どちらも無い → NONE（呼び出し元が必要に応じて MissingCredentialError を投げる）

詳細は docs/OPS.md §2 参照。
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from enum import StrEnum


class ClaudeAuthMode(StrEnum):
    """Claude 認証モード。"""

    API_KEY = "api_key"
    SUBSCRIPTION = "subscription"
    NONE = "none"


@dataclass(frozen=True)
class ClaudeAuth:
    """Claude 認証情報。"""

    mode: ClaudeAuthMode
    api_key: str | None = None
    cli_path: str | None = None

    @property
    def is_authenticated(self) -> bool:
        return self.mode is not ClaudeAuthMode.NONE

    def masked_api_key(self) -> str | None:
        """APIキーをマスクして表示用に返す。"""
        if not self.api_key:
            return None
        if len(self.api_key) <= 12:
            return "***"
        return f"{self.api_key[:8]}...{self.api_key[-4:]}"


class MissingCredentialError(RuntimeError):
    """認証情報が必要なのに解決できなかった場合に投げる。"""


def resolve_claude_auth() -> ClaudeAuth:
    """Claude 認証を解決する。

    Returns:
        ClaudeAuth: 解決されたモードと付随情報。NONEの場合も例外は投げない（呼出元判断）。
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        return ClaudeAuth(mode=ClaudeAuthMode.API_KEY, api_key=api_key)

    cli_path = shutil.which("claude")
    if cli_path:
        return ClaudeAuth(mode=ClaudeAuthMode.SUBSCRIPTION, cli_path=cli_path)

    return ClaudeAuth(mode=ClaudeAuthMode.NONE)


def require_claude_auth() -> ClaudeAuth:
    """Claude 認証が解決できなければ MissingCredentialError を投げる。"""
    auth = resolve_claude_auth()
    if not auth.is_authenticated:
        raise MissingCredentialError(
            "Claude 認証が解決できません。"
            "ANTHROPIC_API_KEY を設定するか、`claude /login` でCLIサブスク認証を行ってください。"
        )
    return auth


def resolve_openai_key() -> str | None:
    """OpenAI APIキーを取得（未設定なら None）。"""
    return os.environ.get("OPENAI_API_KEY")


def resolve_xai_key() -> str | None:
    """xAI (Grok) APIキーを取得（未設定なら None）。"""
    return os.environ.get("XAI_API_KEY")


def resolve_google_key() -> str | None:
    """Google (Gemini) APIキーを取得（未設定なら None）。

    GOOGLE_API_KEY を主、GEMINI_API_KEY をエイリアスとして許容。
    """
    return os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")


def require_openai_key() -> str:
    key = resolve_openai_key()
    if not key:
        raise MissingCredentialError("OPENAI_API_KEY が設定されていません。")
    return key


def require_xai_key() -> str:
    key = resolve_xai_key()
    if not key:
        raise MissingCredentialError("XAI_API_KEY が設定されていません。")
    return key


def require_google_key() -> str:
    key = resolve_google_key()
    if not key:
        raise MissingCredentialError(
            "GOOGLE_API_KEY (または GEMINI_API_KEY) が設定されていません。"
        )
    return key

"""ペルソナの直接ファイルアクセスを制限する PreToolUse hook。

kessen-dojo の設計上、以下のディレクトリはペルソナから直接読まれてはいけない:
  - shared/datawarehouse/  : データは Evaluator 経由でのみ generate_signal に配給される
  - docs/                  : kessen 開発者向けの仕様書・参考実装。ペルソナは案件配下 (task/, knowledge/) のみ参照する

本モジュールは Claude Agent SDK の PreToolUse hook として動作し、
Read / Glob / Grep / Edit / Write / Bash の呼出引数を検査して、上記ディレクトリ
配下を指す場合に `permissionDecision: "deny"` を返す。

Bash については `command` 文字列のサブストリング検査 (絶対パスと相対パスの双方)
で 95% カバーする。完全保証ではないが、kessen の信頼境界としては十分。

設計上の注意:
  - shared/personas/, shared/knowledge/, shared/workers/ は読んでよい (ペルソナ
    定義・ナレッジ・Worker 定義は案件横断で共有される)。
  - 案件配下 (projects/{name}/) は当然読める。
  - 自分のラウンド作業ディレクトリ (runs/{run-id}/round-N/{persona}/) も読める。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from claude_agent_sdk import HookMatcher
from claude_agent_sdk.types import (
    HookContext,
    HookJSONOutput,
    PreToolUseHookInput,
)

# repo_root 配下で deny するディレクトリ (相対パス)
_DEFAULT_DENIED_DIRS: tuple[str, ...] = (
    "shared/datawarehouse",
    "docs",
)

# 読み書き系ツールの引数キー (tool_input から path を取り出す)
_PATH_TOOL_KEYS: dict[str, tuple[str, ...]] = {
    "Read":      ("file_path",),
    "Edit":      ("file_path",),
    "Write":     ("file_path",),
    "Glob":      ("path", "pattern"),
    "Grep":      ("path", "glob"),
    "NotebookEdit": ("notebook_path",),
}


def find_repo_root(start: Path) -> Path:
    """start から祖先方向に pyproject.toml を探して repo_root を返す。

    見つからなければ start.resolve() を repo_root とみなす (フォールバック)。
    """
    p = start.resolve()
    for ancestor in [p, *p.parents]:
        if (ancestor / "pyproject.toml").exists():
            return ancestor
    return start.resolve()


def _normalize(path_str: str, anchor: Path) -> Path | None:
    """tool_input の path 文字列を anchor 起点で絶対パス化。

    - 空文字列 / glob 専用パターン (`**/*.py` 等) はそのまま (== anchor 配下解釈)
    - resolve() で symlink も解決
    """
    if not path_str:
        return None
    p = Path(path_str)
    if not p.is_absolute():
        p = (anchor / p).resolve()
    else:
        try:
            p = p.resolve()
        except OSError:
            pass
    return p


def _is_under(target: Path, root: Path) -> bool:
    """target が root と同一またはその子孫か。"""
    try:
        target.relative_to(root)
        return True
    except ValueError:
        return False


def _build_bash_pattern(repo_root: Path, denied_dirs: tuple[str, ...]) -> re.Pattern[str]:
    """Bash command 文字列スキャン用の正規表現。

    マッチ条件:
      - "shared/datawarehouse" / "docs" などの相対セグメント (前後が境界)
      - "{repo_root}/shared/datawarehouse" / "{repo_root}/docs" などの絶対パス
    """
    parts: list[str] = []
    for rel in denied_dirs:
        # 相対形 (前後に / または ' or " or 空白 or 行頭/末)
        parts.append(re.escape(rel))
        # 絶対形
        parts.append(re.escape(str(repo_root / rel)))
    pattern = "|".join(parts)
    # 単語境界の代わりに「英数字・/ 以外で囲まれている」を境界とする
    # これで /shared/datawarehouse/foo や 'shared/datawarehouse/bar.parquet' などを拾う
    return re.compile(rf"(?:^|[^A-Za-z0-9_./-])(?:{pattern})(?:[^A-Za-z0-9_./-]|/|$)")


def make_path_guard_hook(
    repo_root: Path,
    cwd_anchor: Path,
    denied_dirs: tuple[str, ...] = _DEFAULT_DENIED_DIRS,
    logger: logging.Logger | None = None,
):
    """PreToolUse hook を生成して返す (HookCallback シグネチャに合致)。

    repo_root: kessen-dojo リポジトリ root (pyproject.toml がある場所)
    cwd_anchor: 相対パス解決の基点 (persona の cwd)
    denied_dirs: repo_root 相対の deny ディレクトリ
    """
    denied_abs = tuple((repo_root / d).resolve() for d in denied_dirs)
    bash_pattern = _build_bash_pattern(repo_root, denied_dirs)
    log = logger or logging.getLogger("kessen.path_guard")

    def _build_deny(reason: str) -> HookJSONOutput:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            },
        }

    def _check_path(value: Any) -> Path | None:
        """tool_input から取り出した値が deny 対象なら、その絶対パスを返す。"""
        if not isinstance(value, str) or not value:
            return None
        # glob だけ (例: "**/*.py") は anchor 配下と解釈 → deny にならない
        # 一方 "shared/datawarehouse/**/*.parquet" のように deny ディレクトリを
        # 含む glob は denied としたいので _normalize して relative_to で判定
        target = _normalize(value, cwd_anchor)
        if target is None:
            return None
        for root in denied_abs:
            if _is_under(target, root):
                return target
        return None

    async def _hook(
        input: PreToolUseHookInput,
        tool_use_id: str | None,
        context: HookContext,
    ) -> HookJSONOutput:
        del tool_use_id, context
        tool_name = input["tool_name"] if isinstance(input, dict) else input.tool_name
        tool_input = input["tool_input"] if isinstance(input, dict) else input.tool_input

        # 読み書き系: 既知のキーから path を取り出して判定
        keys = _PATH_TOOL_KEYS.get(tool_name)
        if keys:
            for k in keys:
                blocked = _check_path(tool_input.get(k))
                if blocked is not None:
                    msg = (
                        f"Access denied by kessen path guard: '{tool_name}' tried to access "
                        f"{blocked} which is inside a restricted directory "
                        f"({', '.join(denied_dirs)}). "
                        f"datawarehouse data is provided through the Evaluator's "
                        f"generate_signal arguments only; docs/ is for kessen developers, "
                        f"not personas. Use task/, knowledge/, and your own runs/ directory."
                    )
                    log.warning(msg)
                    return _build_deny(msg)

        # Bash: command 文字列をパターンマッチ
        if tool_name == "Bash":
            cmd = tool_input.get("command", "")
            if isinstance(cmd, str) and bash_pattern.search(cmd):
                msg = (
                    f"Access denied by kessen path guard: 'Bash' command appears to "
                    f"reference a restricted directory ({', '.join(denied_dirs)}). "
                    f"datawarehouse data is provided through the Evaluator only; "
                    f"docs/ is for kessen developers. Inspect data shape via the "
                    f"DataFrames passed into your generate_signal/baseline functions, "
                    f"not by reading parquet files directly. Command was: {cmd[:200]}"
                )
                log.warning(msg)
                return _build_deny(msg)

        # それ以外は許可
        return {}

    return _hook


def build_path_guard_matchers(
    repo_root: Path,
    cwd_anchor: Path,
    denied_dirs: tuple[str, ...] = _DEFAULT_DENIED_DIRS,
    logger: logging.Logger | None = None,
) -> dict[str, list[HookMatcher]]:
    """ClaudeAgentOptions.hooks に渡す形式 (PreToolUse → HookMatcher のリスト) を返す。"""
    hook = make_path_guard_hook(repo_root, cwd_anchor, denied_dirs, logger)
    matched_tools = "|".join([*_PATH_TOOL_KEYS.keys(), "Bash"])
    return {
        "PreToolUse": [
            HookMatcher(matcher=matched_tools, hooks=[hook]),
        ],
    }

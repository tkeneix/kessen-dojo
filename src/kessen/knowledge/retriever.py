"""階層マージ ナレッジ retriever。

PRD §7.1 / §7.2 / §7.4 を実装:
  - shared (弱) < common (中) < private (強) の優先度
  - .md は同名ファイルを下位が上書き
  - .jsonl は全層時系列マージして新着 N 件
  - 構造データ (.json/.toml) はキー単位で下位がディープマージ
  - 空階層でもエラーにならず空Markdown断片を返す

system_prompt 末尾への注入が前提。
"""

from __future__ import annotations

import json
import tomllib
from collections.abc import Iterable
from pathlib import Path
from typing import Literal

from kessen.knowledge.store import KnowledgeEntry, read_all

KnowledgeLayer = Literal["shared", "common", "private"]
DEFAULT_LAYERS: tuple[KnowledgeLayer, ...] = ("shared", "common", "private")
DEFAULT_MAX_JSONL = 20


class KnowledgeRetriever:
    """ナレッジ階層をマージして system_prompt 用 Markdown断片を生成する。"""

    def __init__(self, repo_root: Path):
        self.repo_root = repo_root.resolve()

    # ------------------------------------------------------------------
    # public
    # ------------------------------------------------------------------

    def compose(
        self,
        persona_id: str,
        project_dir: Path,
        *,
        layers: Iterable[KnowledgeLayer] = DEFAULT_LAYERS,
        max_jsonl: int = DEFAULT_MAX_JSONL,
    ) -> str:
        """指定ペルソナ向けに階層マージしたMarkdownを返す。空でも空文字を返すだけ（例外なし）。"""
        layer_dirs = self._resolve_layer_dirs(project_dir.resolve(), persona_id, layers)

        md_files = self._merge_md(layer_dirs)
        jsonl_entries = self._merge_jsonl(layer_dirs, max_n=max_jsonl)
        struct_data = self._merge_struct(layer_dirs)

        return self._render(md_files, jsonl_entries, struct_data)

    # ------------------------------------------------------------------
    # layer resolution
    # ------------------------------------------------------------------

    def _resolve_layer_dirs(
        self,
        project_dir: Path,
        persona_id: str,
        layers: Iterable[KnowledgeLayer],
    ) -> list[tuple[KnowledgeLayer, Path]]:
        """各 layer のディレクトリパスを (layer, path) のリストで返す。
        弱い順 → 強い順。存在しない層はスキップ。
        """
        result: list[tuple[KnowledgeLayer, Path]] = []
        for layer in layers:
            if layer == "shared":
                p = self.repo_root / "shared" / "knowledge"
            elif layer == "common":
                p = project_dir / "knowledge" / "common"
            elif layer == "private":
                p = project_dir / "knowledge" / "per-persona" / persona_id
            else:
                continue
            if p.exists() and p.is_dir():
                result.append((layer, p))
        return result

    # ------------------------------------------------------------------
    # mergers
    # ------------------------------------------------------------------

    @staticmethod
    def _merge_md(layer_dirs: list[tuple[KnowledgeLayer, Path]]) -> dict[str, tuple[KnowledgeLayer, str]]:
        """同名 .md は下位 layer (より強い) が上書き。

        Returns:
            { 相対パス文字列: (採用layer, 内容) }
        """
        result: dict[str, tuple[KnowledgeLayer, str]] = {}
        for layer, base in layer_dirs:
            for md_path in sorted(base.rglob("*.md")):
                rel = str(md_path.relative_to(base))
                result[rel] = (layer, md_path.read_text(encoding="utf-8"))
        return result

    @staticmethod
    def _merge_jsonl(
        layer_dirs: list[tuple[KnowledgeLayer, Path]],
        *,
        max_n: int,
    ) -> list[KnowledgeEntry]:
        """全層の .jsonl エントリを集めて ts 降順で先頭 N 件。"""
        entries: list[KnowledgeEntry] = []
        for _, base in layer_dirs:
            for jsonl_path in sorted(base.rglob("*.jsonl")):
                entries.extend(read_all(jsonl_path))
        # ts 降順
        entries.sort(key=lambda e: e.ts, reverse=True)
        return entries[:max_n]

    @staticmethod
    def _deep_merge(dst: dict, src: dict) -> dict:
        """src を dst にディープマージ。同キー dict は再帰、それ以外は src で上書き。"""
        for k, v in src.items():
            if k in dst and isinstance(dst[k], dict) and isinstance(v, dict):
                KnowledgeRetriever._deep_merge(dst[k], v)
            else:
                dst[k] = v
        return dst

    @classmethod
    def _merge_struct(cls, layer_dirs: list[tuple[KnowledgeLayer, Path]]) -> dict[str, dict]:
        """同名 .json/.toml をキー単位でディープマージ（下位が上書き）。

        Returns:
            { 相対パス文字列: マージ済みdict }
        """
        result: dict[str, dict] = {}
        for _, base in layer_dirs:
            for path in sorted(list(base.rglob("*.json")) + list(base.rglob("*.toml"))):
                rel = str(path.relative_to(base))
                if path.suffix == ".json":
                    data = json.loads(path.read_text(encoding="utf-8"))
                else:
                    with path.open("rb") as f:
                        data = tomllib.load(f)
                if not isinstance(data, dict):
                    continue
                if rel in result:
                    cls._deep_merge(result[rel], data)
                else:
                    result[rel] = data
        return result

    # ------------------------------------------------------------------
    # render
    # ------------------------------------------------------------------

    @staticmethod
    def _render(
        md_files: dict[str, tuple[KnowledgeLayer, str]],
        jsonl_entries: list[KnowledgeEntry],
        struct_data: dict[str, dict],
    ) -> str:
        """マージ結果をMarkdown断片に整形。空なら空文字。"""
        if not md_files and not jsonl_entries and not struct_data:
            return ""

        sections: list[str] = ["# Knowledge (auto-injected)"]

        if md_files:
            sections.append("\n## Documents")
            for rel, (layer, content) in sorted(md_files.items()):
                sections.append(f"\n### {rel} (layer={layer})\n\n{content.rstrip()}")

        if struct_data:
            sections.append("\n## Structured Data")
            for rel, data in sorted(struct_data.items()):
                sections.append(f"\n### {rel}\n\n```json\n{json.dumps(data, ensure_ascii=False, indent=2)}\n```")

        if jsonl_entries:
            sections.append(f"\n## Recent Logs (latest {len(jsonl_entries)})")
            for e in jsonl_entries:
                pid = f" persona={e.persona_id}" if e.persona_id else ""
                rnd = f" round={e.round}" if e.round is not None else ""
                sections.append(f"- `{e.ts}` [{e.layer}] {e.kind}{pid}{rnd}: {e.content}")

        return "\n".join(sections) + "\n"

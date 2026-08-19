"""ナレッジJSONLストア。

PRD §7.3 JSONLスキーマ最小定義に準拠:
  {ts, layer, persona_id?, round?, kind, content}
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

from kessen.logging_config import JST

KnowledgeLayer = Literal["shared", "common", "private"]
KnowledgeKind = Literal[
    "feedback",
    "lesson",
    "hypothesis",
    "error",
    "validation_failed",
    "runtime_failed",
]


@dataclass
class KnowledgeEntry:
    """ナレッジJSONL 1エントリ。"""

    ts: str                                 # ISO 8601 JST
    layer: KnowledgeLayer
    kind: str                               # KnowledgeKind 想定だが拡張可
    content: str
    persona_id: str | None = None
    round: int | None = None
    extra: dict = field(default_factory=dict)

    @classmethod
    def now(
        cls,
        layer: KnowledgeLayer,
        kind: str,
        content: str,
        *,
        persona_id: str | None = None,
        round: int | None = None,
        extra: dict | None = None,
    ) -> KnowledgeEntry:
        return cls(
            ts=datetime.now(tz=JST).isoformat(timespec="seconds"),
            layer=layer,
            kind=kind,
            content=content,
            persona_id=persona_id,
            round=round,
            extra=extra or {},
        )

    def to_dict(self) -> dict:
        d = asdict(self)
        # None フィールドは省略してJSONを軽量化
        if d.get("persona_id") is None:
            d.pop("persona_id", None)
        if d.get("round") is None:
            d.pop("round", None)
        if not d.get("extra"):
            d.pop("extra", None)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> KnowledgeEntry:
        return cls(
            ts=d["ts"],
            layer=d["layer"],
            kind=d["kind"],
            content=d["content"],
            persona_id=d.get("persona_id"),
            round=d.get("round"),
            extra=d.get("extra", {}),
        )


def append(jsonl_path: Path, entry: KnowledgeEntry) -> None:
    """JSONLファイルに1行追記。親ディレクトリが無ければ作る。"""
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    with jsonl_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry.to_dict(), ensure_ascii=False))
        f.write("\n")


def read_all(jsonl_path: Path) -> Iterator[KnowledgeEntry]:
    """JSONLを全行読み出してエントリを yield。存在しないファイルは空イテレータ。"""
    if not jsonl_path.exists():
        return
    with jsonl_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                # 破損行はスキップ（呼出元でログ）
                continue
            yield KnowledgeEntry.from_dict(d)

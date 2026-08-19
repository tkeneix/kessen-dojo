"""ペルソナID → BasePersona具象インスタンス への解決。"""

from __future__ import annotations

from pathlib import Path

from kessen.config.loader import (
    load_class,
    load_persona_config,
    resolve_persona_class_path,
)
from kessen.personas.base import BasePersona


def build_persona(
    persona_id: str,
    project_dir: Path,
    repo_root: Path,
) -> BasePersona:
    """persona_id を解決して BasePersona の具象インスタンスを返す。

    手順:
      1. persona TOML を読み込む (案件配下 or shared/ から探索)
      2. class フィールド or engine からデフォルトクラスを引く
      3. project_dir を sys.path に追加して動的import
      4. 具象クラスをインスタンス化
    """
    persona_cfg = load_persona_config(project_dir, persona_id)
    class_path = resolve_persona_class_path(persona_cfg)
    cls = load_class(class_path, project_dir=project_dir)

    if not issubclass(cls, BasePersona):
        raise TypeError(
            f"{class_path!r} resolved to {cls.__name__} which is not a BasePersona subclass"
        )

    return cls(
        persona_id=persona_cfg.persona.id,
        engine=persona_cfg.persona.engine,
        project_dir=project_dir,
        repo_root=repo_root,
        config=persona_cfg,
    )

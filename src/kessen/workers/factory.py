"""worker_id → BaseWorker 具象インスタンス への解決。

build_worker は以下の順で動く:
  1. workers/{worker_id}.toml を読む (案件配下 → shared フォールバック)
  2. class フィールド or デフォルト (LLMWorker) を動的import
  3. ペルソナ側 [workers.<id>] override (engine / prompt_addon / engine_options) を適用
  4. 解決済みパラメータで Worker をインスタンス化

ペルソナの orchestrator が build_worker → worker.execute(in_dir, out_dir) を呼ぶ
基本フローを想定。MCP ツール (tools/worker_tool.py) からも同じ factory を使う。
"""

from __future__ import annotations

from pathlib import Path

from kessen.config.loader import (
    WorkerOverride,
    load_class,
    load_persona_config,
    load_worker_config,
    resolve_worker_class_path,
)
from kessen.workers.base import BaseWorker


def build_worker(
    worker_id: str,
    persona_id: str,
    project_dir: Path,
    *,
    apply_persona_override: bool = True,
) -> BaseWorker:
    """worker_id を解決して BaseWorker の具象インスタンスを返す。

    Args:
        worker_id: workers/{worker_id}.toml に対応する Worker 識別子
        persona_id: 親ペルソナ ID。lessons.jsonl パスと override 解決に使う
        project_dir: 案件ディレクトリ
        apply_persona_override: True なら persona TOML の [workers.<id>] を適用。
            persona TOML が見つからない/該当 override が無い場合は default のみ。
            False なら Worker TOML の値だけで構築 (テスト・直接呼出向け)。

    Raises:
        FileNotFoundError: workers/{worker_id}.toml が見つからない
        ValueError: TOML id 不一致 / engine 不正
        TypeError: class が BaseWorker のサブクラスでない
    """
    worker_cfg = load_worker_config(project_dir, worker_id)
    class_path = resolve_worker_class_path(worker_cfg)
    cls = load_class(class_path, project_dir=project_dir)

    if not issubclass(cls, BaseWorker):
        raise TypeError(
            f"{class_path!r} resolved to {cls.__name__} which is not a BaseWorker subclass"
        )

    override: WorkerOverride | None = None
    if apply_persona_override:
        override = _resolve_persona_override(project_dir, persona_id, worker_id)

    engine = (override.engine if override and override.engine else worker_cfg.worker.engine)
    role = worker_cfg.worker.role
    if override and override.prompt_addon:
        role = f"{role}\n\n{override.prompt_addon}" if role else override.prompt_addon

    engine_options: dict = dict(worker_cfg.engine_options)
    if override and override.engine_options:
        engine_options.update(override.engine_options)

    return cls(
        worker_id=worker_id,
        persona_id=persona_id,
        project_dir=project_dir,
        engine=engine,
        role=role,
        engine_options=engine_options,
        output_filename=worker_cfg.worker.output_filename,
    )


def _resolve_persona_override(
    project_dir: Path,
    persona_id: str,
    worker_id: str,
) -> WorkerOverride | None:
    """persona TOML から [workers.<worker_id>] override を取得。

    persona TOML が無い場合 (テスト等) は黙って None。
    """
    try:
        persona_cfg = load_persona_config(project_dir, persona_id)
    except FileNotFoundError:
        return None
    return persona_cfg.workers.get_override(worker_id)

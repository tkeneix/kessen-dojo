"""TOML設定ローダ + 動的Pythonクラス解決。

PRD §5 設定ファイル仕様 / §5.4 動的import規約 を実装。
"""

from __future__ import annotations

import importlib
import sys
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# ----------------------------------------------------------------------
# pydantic models
# ----------------------------------------------------------------------


class RetryConfig(BaseModel):
    """[retry] セクション。project/persona 共通スキーマ。"""

    model_config = ConfigDict(extra="forbid")

    max_attempts: int = 0
    on_exhaustion: Literal["exclude", "zero_score"] = "exclude"
    include_history_in_feedback: bool = True
    abort_run_on_all_failed: bool = False


class EvaluatorConfig(BaseModel):
    """[evaluator] セクション。case固有のキーは extra=allow で受ける。"""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    cls: str = Field(alias="class")


class ParticipantsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ids: list[str]


class ProjectMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    rounds: int = 1
    max_parallel: int = 1


class ProjectConfig(BaseModel):
    """project.toml 全体。"""

    model_config = ConfigDict(extra="forbid")

    project: ProjectMeta
    evaluator: EvaluatorConfig | None = None
    participants: ParticipantsConfig
    retry: RetryConfig = Field(default_factory=RetryConfig)


class KnowledgeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    include: list[Literal["shared", "common", "private"]] = Field(
        default_factory=lambda: ["shared", "common", "private"]
    )


class WorkerOverride(BaseModel):
    """Persona TOML 内 [workers.<id>] セクションでの上書き指定。

    各ペルソナがその worker をどう使うか (どの engine で、どんな追加指示で) を表す。
    None フィールドは「上書きせず WorkerConfig 既定値を使う」意味。
    """

    model_config = ConfigDict(extra="forbid")
    engine: str | None = None
    prompt_addon: str | None = None
    engine_options: dict = Field(default_factory=dict)


class WorkersConfig(BaseModel):
    """Persona TOML 内 [workers] セクション。

    ids に列挙された worker_id ごとに、[workers.<id>] サブテーブルで
    engine / prompt_addon / engine_options を上書きできる (extra="allow" で受ける)。
    """

    model_config = ConfigDict(extra="allow")
    ids: list[str] = Field(default_factory=list)

    def get_override(self, worker_id: str) -> WorkerOverride | None:
        """[workers.<worker_id>] が指定されていれば WorkerOverride を返す。"""
        extras = self.__pydantic_extra__ or {}
        raw = extras.get(worker_id)
        if not isinstance(raw, dict):
            return None
        return WorkerOverride.model_validate(raw)


class PersonaMeta(BaseModel):
    """[persona] セクション。`class` は Python予約語のため alias 経由。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: str
    cls: str | None = Field(default=None, alias="class")
    engine: str
    description: str = ""


class PersonaConfig(BaseModel):
    """persona TOML 全体。"""

    model_config = ConfigDict(extra="forbid")

    persona: PersonaMeta
    knowledge: KnowledgeConfig = Field(default_factory=KnowledgeConfig)
    workers: WorkersConfig = Field(default_factory=WorkersConfig)
    engine_options: dict = Field(default_factory=dict)
    retry: RetryConfig | None = None  # None なら project の retry を継承


class WorkerMeta(BaseModel):
    """Worker TOML の [worker] セクション。`class` は alias 経由。

    output_filename:
      設定すると LLMWorker は応答全体を `out_dir/<output_filename>` に書出し、
      `=== FILE: <name> ===` マーカーパースをスキップする。後続ペルソナにとって
      パスが決定論的になり、Glob 不要になる (2026-05-06 設計レビュー)。
      未指定なら従来通り marker パース + response.md フォールバック。
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: str
    cls: str | None = Field(default=None, alias="class")
    engine: str
    role: str = ""
    output_filename: str | None = None


class WorkerConfig(BaseModel):
    """worker TOML 全体 (workers/{id}.toml)。"""

    model_config = ConfigDict(extra="forbid")

    worker: WorkerMeta
    engine_options: dict = Field(default_factory=dict)


# ----------------------------------------------------------------------
# loaders
# ----------------------------------------------------------------------


def load_toml(path: Path) -> dict:
    """TOMLファイルをロード。存在しなければ FileNotFoundError。"""
    with path.open("rb") as f:
        return tomllib.load(f)


def load_project_config(project_dir: Path) -> ProjectConfig:
    """`project.toml` を読み ProjectConfig を返す。"""
    path = project_dir / "project.toml"
    data = load_toml(path)
    return ProjectConfig.model_validate(data)


def load_persona_config(project_dir: Path, persona_id: str) -> PersonaConfig:
    """`personas/{id}.toml` を読み PersonaConfig を返す。

    案件配下に無ければ `shared/personas/{id}.toml` を探索。
    """
    candidates = [
        project_dir / "personas" / f"{persona_id}.toml",
        project_dir.parent.parent / "shared" / "personas" / f"{persona_id}.toml",
    ]
    for candidate in candidates:
        if candidate.exists():
            data = load_toml(candidate)
            cfg = PersonaConfig.model_validate(data)
            if cfg.persona.id != persona_id:
                raise ValueError(
                    f"persona TOML id mismatch: file says {cfg.persona.id!r}, expected {persona_id!r} ({candidate})"
                )
            return cfg
    raise FileNotFoundError(
        f"persona '{persona_id}' not found in {project_dir / 'personas'} or shared/personas"
    )


def load_worker_config(project_dir: Path, worker_id: str) -> WorkerConfig:
    """`workers/{id}.toml` を読み WorkerConfig を返す。

    案件配下に無ければ `shared/workers/{id}.toml` を探索 (personas と同パターン)。
    """
    candidates = [
        project_dir / "workers" / f"{worker_id}.toml",
        project_dir.parent.parent / "shared" / "workers" / f"{worker_id}.toml",
    ]
    for candidate in candidates:
        if candidate.exists():
            data = load_toml(candidate)
            cfg = WorkerConfig.model_validate(data)
            if cfg.worker.id != worker_id:
                raise ValueError(
                    f"worker TOML id mismatch: file says {cfg.worker.id!r}, expected {worker_id!r} ({candidate})"
                )
            return cfg
    raise FileNotFoundError(
        f"worker '{worker_id}' not found in {project_dir / 'workers'} or shared/workers"
    )


def merge_retry(project_retry: RetryConfig, persona_retry: RetryConfig | None) -> RetryConfig:
    """ペルソナ側の retry が指定されていればそれを採用、なければ project の値を継承。"""
    return persona_retry if persona_retry is not None else project_retry


# ----------------------------------------------------------------------
# dynamic class import
# ----------------------------------------------------------------------


_DEFAULT_PERSONA_CLASSES = {
    "claude": "kessen.personas.claude_persona:ClaudePersona",
    "openai": "kessen.personas.openai_persona:OpenAIPersona",
    "xai": "kessen.personas.grok_persona:GrokPersona",
}


def parse_engine(engine: str) -> tuple[str, str]:
    """'claude:opus-4-7' → ('claude', 'opus-4-7') の形に分解。"""
    if ":" not in engine:
        raise ValueError(f"engine must be 'provider:model' format, got: {engine!r}")
    provider, model = engine.split(":", 1)
    if not provider or not model:
        raise ValueError(f"invalid engine string: {engine!r}")
    return provider, model


def resolve_persona_class_path(cfg: PersonaConfig) -> str:
    """PersonaConfig.persona.class が空なら engine からデフォルトクラスを引く。"""
    if cfg.persona.cls:
        return cfg.persona.cls
    provider, _ = parse_engine(cfg.persona.engine)
    if provider not in _DEFAULT_PERSONA_CLASSES:
        raise ValueError(
            f"engine provider '{provider}' has no default persona class; specify [persona] class explicitly"
        )
    return _DEFAULT_PERSONA_CLASSES[provider]


_DEFAULT_WORKER_CLASS = "kessen.workers.llm_worker:LLMWorker"


def resolve_worker_class_path(cfg: WorkerConfig) -> str:
    """WorkerConfig.worker.class が空ならデフォルトの LLMWorker を返す。"""
    return cfg.worker.cls or _DEFAULT_WORKER_CLASS


def load_class(class_path: str, project_dir: Path | None = None) -> type:
    """'module.path:ClassName' 文字列からクラスをロード。

    project_dir が与えられた場合、その絶対パスを sys.path 先頭に追加して
    案件配下の persona_classes / workers / evaluators / tools を解決可能にする。
    """
    if ":" not in class_path:
        raise ValueError(f"class_path must be 'module.path:ClassName' format, got: {class_path!r}")
    module_path, class_name = class_path.split(":", 1)

    if project_dir is not None:
        project_str = str(project_dir.resolve())
        if project_str not in sys.path:
            sys.path.insert(0, project_str)

    module = importlib.import_module(module_path)
    cls = getattr(module, class_name, None)
    if cls is None:
        raise AttributeError(f"module '{module_path}' has no attribute {class_name!r}")
    if not isinstance(cls, type):
        raise TypeError(f"{class_path!r} resolved to {type(cls).__name__}, not a class")
    return cls

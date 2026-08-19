"""BasePersona ABC + 共通プロンプト合成ロジック。

PRD §4.1 / §4.1.1 (リトライfeedback読込規約) を実装。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from kessen.config.loader import PersonaConfig
from kessen.knowledge.retriever import KnowledgeRetriever

# §4.1.1 で定めた既定の retry feedback 読込規約
RETRY_FEEDBACK_RULE = (
    "**重要**: in/ ディレクトリに `_retry_feedback.md` がある場合、必ず最初に読むこと。"
    "これは前回試行で評価モデルが指摘した問題と修正提案である。"
    "今回の出力ではこれらをすべて解消する必要がある。"
)


@dataclass
class PersonaResult:
    """ペルソナ実行結果。"""

    persona_id: str
    status: str                          # "ok" | "dry_run" | "error"
    summary: str = ""
    output_files: list[str] = field(default_factory=list)
    error: str | None = None
    had_runtime_warning: bool = False    # SDK内部エラーが記録されたが成果物は valid の可能性 (P5)


class BasePersona(ABC):
    """全ペルソナの基底。具象は engine 別 (Claude / OpenAI / Grok) または案件側継承で実装する。"""

    # サブクラスで上書きするデフォルト指示文
    base_prompt: str = "あなたは kessen-dojo の参加ペルソナです。"

    def __init__(
        self,
        persona_id: str,
        engine: str,
        project_dir: Path,
        repo_root: Path,
        config: PersonaConfig,
    ):
        self.persona_id = persona_id
        self.engine = engine
        self.project_dir = project_dir
        self.repo_root = repo_root
        self.config = config
        self.retriever = KnowledgeRetriever(repo_root)

    # ------------------------------------------------------------------
    # public abstract
    # ------------------------------------------------------------------

    @abstractmethod
    async def run(
        self,
        in_dir: Path,
        out_dir: Path,
        *,
        dry_run: bool = False,
    ) -> PersonaResult:
        """in_dir を読み out_dir に成果物を書き出す。dry_run=True なら推論せずプロンプトのみ書出。"""

    # ------------------------------------------------------------------
    # prompt composition (testable)
    # ------------------------------------------------------------------

    def compose_system_prompt(self, extra: str = "") -> str:
        """system_prompt を [base_prompt → 案件指示 → retry規約 → ナレッジ] の順に連結。"""
        knowledge = self.retriever.compose(
            self.persona_id,
            self.project_dir,
            layers=tuple(self.config.knowledge.include),
        )
        parts = [self.base_prompt]
        if self.config.persona.description:
            parts.append(f"# Role\n{self.config.persona.description}")
        if extra:
            parts.append(extra)
        parts.append(RETRY_FEEDBACK_RULE)
        if knowledge:
            parts.append(knowledge)
        return "\n\n".join(parts)

    @staticmethod
    def compose_user_prompt(in_dir: Path, out_dir: Path) -> str:
        """ペルソナへの user_prompt を組立。

        in_dir 配下の全ファイルを列挙して prompt に含める (2026-05-06):
          ペルソナ allowed_tools から Glob を外したことで、エージェントが
          ディレクトリ列挙できなくなった。代わりに staging 済みファイル名を
          orchestrator 側が prompt に明示する。

        成果物の生成方式 (2026-05-06 改訂):
          ペルソナ自身が `Write` ツールで `out_dir` 配下にファイルを生成する。
          生成すべきファイル名・様式は **ペルソナの description で個別に指定** する
          (ペルソナの種別ごとに異なる出力構造を取れるよう自由度を確保)。
          基盤側ではプログラム的なファイル生成は行わない。
        """
        files = sorted(
            (str(p.relative_to(in_dir)) for p in in_dir.rglob("*") if p.is_file()),
        ) if in_dir.exists() else []

        if files:
            files_block = "\n".join(f"- `{f}`" for f in files)
        else:
            files_block = "(入力ファイルなし)"

        return (
            f"# 入力ディレクトリ\n"
            f"パス: `{in_dir}`\n\n"
            f"以下のファイルが配置済みです (`Read` ツールで読み込めます):\n"
            f"{files_block}\n\n"
            f"# 出力ディレクトリ\n"
            f"パス: `{out_dir}`\n\n"
            f"# 成果物の生成\n"
            f"ペルソナの description (Role) に **生成すべきファイル名と様式が指定**"
            f" されています。description の指示に従って `Write` ツールで `{out_dir}`"
            f" 配下に必要なファイルを作成してください。\n\n"
            f"- description で指定された全ファイルを必ず Write すること (Write 忘れは"
            f" ペルソナ要件未達とみなされる)。\n"
            f"- description で指定された様式 (関数シグネチャ・必須セクション等) を厳守すること。\n"
            f"- description に記載が無いファイルは原則として書かない。\n\n"
            f"ペルソナの description に従って役割を解釈し、必要な成果物を生成してください。"
        )

# kessen-dojo PRD

決戦道場 — 複数ペルソナのAIエージェントが評価指標で競い合い、ラウンド勝ち抜き方式で最良案を選び取る常駐型エージェント基盤の仕様書。

- 対象バージョン: `0.1.0`
- 運用手順は [OPS.md](OPS.md)、新規案件の作り方は [NEW_PROJECT.md](NEW_PROJECT.md) を参照
- 本書の章番号はソース内コメントの `PRD §x.y` 参照と対応している

---

## 1. プロジェクト概要

### 1.1 目的

同じ課題に対して**思想の異なる複数のAIペルソナ**を同時に走らせ、機械的な評価指標でランク付けし、勝者の成果物を次ラウンドの起点として配り直す。これを繰り返すことで、単一エージェントの一発出力より良い成果物へ収束させる。

基盤が提供するのは以下の4点であり、課題そのものの知識は持たない。

1. ペルソナの並列実行と、成果物の隔離されたディレクトリ管理
2. 評価器による validate → retry → score の反復
3. ラウンド間の勝者シード配布とナレッジ蓄積
4. 進行状態の永続化 (ファイル + SQLite) と REST/CLI からの操作

### 1.2 想定ユースケース

| ユースケース | 内容 |
|---|---|
| 定量戦略コンペ | 複数のトレード思想のペルソナが戦略コードを生成、バックテスト指標で競う (収録案件 `nk225-yorihike`) |
| 文書生成コンペ | 同じ入力に対し構成の異なるレポートを生成、LLMジャッジで採点 (`LLMJudgeEvaluator`) |
| 案出しの発散と収束 | 発散フェーズ (多ペルソナ並列) → 収束フェーズ (勝者シード + ラウンド継続) |

### 1.3 設計原則

1. **基盤と案件の分離** — `src/kessen/` に案件固有のコードを書かない。案件は `projects/{name}/` 配下で完結させ、基底クラスの継承と TOML で表現する。
2. **成果物はファイルが正** — ペルソナ間・ラウンド間の受け渡しはすべてディレクトリとファイル。プロセス内メモリの共有状態を作らない。
3. **失敗しても走り切る** — ペルソナ1体の失敗はラウンドを止めない。失敗はスコア `None` として leaderboard に残し、勝者選定から除外する。
4. **中断に強い** — Ctrl+C / API cancel の際も、書きかけの成果物と leaderboard をフラッシュしてから終了する。数十分の実行結果を捨てない。
5. **ペルソナに与える権限は最小限** — ツール利用可能性を明示的にゲートし、リポジトリ内の非公開領域へは PreToolUse hook でアクセスを拒否する (§10)。

---

## 2. アーキテクチャ

```
                    ┌──────────────┐          ┌──────────────┐
   CLI (kessen) ───▶│              │          │  runs.db     │
                    │ Orchestrator │◀────────▶│ (SQLite/WAL) │
   REST API    ───▶ │              │          └──────────────┘
   (FastAPI)        └──────┬───────┘
                           │ round loop
                           ▼
        ┌──────────────────────────────────────────┐
        │  run_round: 並列 N ペルソナ + retry       │
        └───┬───────────────┬───────────────┬──────┘
            ▼               ▼               ▼
      ┌──────────┐    ┌──────────┐    ┌──────────┐
      │ Persona  │    │ Persona  │    │ Persona  │   ← Claude / OpenAI / xAI
      │  (+Worker)│   │          │    │          │
      └────┬─────┘    └────┬─────┘    └────┬─────┘
           │ out/          │ out/          │ out/
           └───────────────┴───────────────┘
                           ▼
                    ┌──────────────┐
                    │  Evaluator   │  validate() → score() → Leaderboard
                    └──────┬───────┘
                           ▼
              leaderboard.json → 勝者 out/ を次ラウンドの seed へ
                           +
              knowledge/*.jsonl  → 次ラウンドの system_prompt へ注入
```

### 2.1 主要コンポーネント

| 層 | モジュール | 責務 |
|---|---|---|
| CLI | `kessen.cli` | `info` / `persona-run` / `run-once` / `serve` / `knowledge-show` |
| Orchestrator | `kessen.orchestrator.runner` | run-id 発行、ラウンドループ、勝者シード生成、`final.json` / `summary.md` / run履歴の出力 |
| Orchestrator | `kessen.orchestrator.round` | 1ラウンドの並列実行、per-persona リトライループ、leaderboard 生成 |
| Persona | `kessen.personas.*` | `BasePersona` と provider 別具象 (`ClaudePersona` / `OpenAIPersona` / `GrokPersona`)、`build_persona` |
| Worker | `kessen.workers.*` | `BaseWorker` / `LLMWorker` / `build_worker`。ペルソナ配下の単発専門家 |
| Evaluator | `kessen.evaluator.*` | `BaseEvaluator`、`Leaderboard`、標準 scorer (`StubEvaluator` / `LLMJudgeEvaluator`) |
| Knowledge | `kessen.knowledge.*` | 階層マージ retriever と JSONL ストア |
| Config | `kessen.config.loader` | TOML → pydantic モデル、`module:Class` の動的 import |
| Auth | `kessen.auth.resolver` | Claude / OpenAI / xAI / Google の認証解決 |
| State | `kessen.state.*` | SQLite (`runs` / `round_scores`) の初期化とリポジトリ |
| API | `kessen.api.*` | FastAPI アプリ、ラン起動/取得/キャンセル/再開 |
| Tools | `kessen.tools.*` | in-process MCP サーバと `call_persona` / `call_worker` |
| Logging | `kessen.logging_config` | JST タイムスタンプ + 7世代ローテーション |

### 2.2 Claude Agent SDK 利用マップ

| 用途 | 実装箇所 | 主なオプション |
|---|---|---|
| ペルソナ本体 | `personas/claude_persona.py` | `tools=` (利用可能性ゲート) / `allowed_tools=` (auto-allow) / `cwd` / `model` / `max_turns=12` / `permission_mode="acceptEdits"` / `setting_sources=[]` / `hooks` (path guard) / `mcp_servers` (任意) |
| Worker (`claude:` engine) | `workers/llm_worker.py::_call_claude` | `allowed_tools=[]` / `max_turns=1` / `setting_sources=[]` / `stderr` コールバック |
| LLM ジャッジ | `evaluator/scorers/llm_judge.py` | `allowed_tools=[]` / `max_turns=2` / `permission_mode="default"` / `setting_sources=[]` |
| MCPツール提供 | `tools/registry.py` | `create_sdk_mcp_server(name="kessen", ...)` |
| アクセス制御 | `personas/path_guard.py` | `PreToolUse` hook で `permissionDecision: "deny"` |

**重要な実装上の性質** (2026-05-06 の修正で確定):

- SDK の `allowed_tools` は「権限プロンプトを出さずに自動許可するリスト」であって、**利用可否のゲートではない**。ゲートは `tools=` フィールド。そのため `ClaudePersona._build_options` は `engine_options.allowed_tools` から MCP 以外のビルトインツールを抜き出して `tools=` にも渡している。
- `mcp__*` のツール名を `tools=` に混ぜると SDK がビルトイン名として解釈して失敗する。MCP ツールは `mcp_servers` 経由でのみ注入する。
- 全呼び出しで `setting_sources=[]` を指定し、`.claude/` 配下の設定自動ロードを抑止する (§7.4)。

### 2.3 MCPツール統合層

#### 2.3.1 目的と方針

ペルソナ自身を「配下の Worker を指揮する小さなオーケストレータ」として動かすための経路。基盤の Python 関数を in-process MCP サーバとして Claude セッションに露出する。外部プロセスの MCP サーバは立てない。

#### 2.3.2 ツール一覧

| ツール | 実装 | 用途 |
|---|---|---|
| `call_persona` | `tools/persona_tool.py` | ペルソナ単位の呼び出し。provider を問わず統一 IF で実行する |
| `call_worker` | `tools/worker_tool.py` | ペルソナ配下の Worker を呼び出す |

将来の追加候補 (未実装): `workspace_tool` / `knowledge_tool` / `evaluator_tool` / `runtime_tool`。

#### 2.3.3 ツール定義: `call_persona` / `call_worker`

いずれも引数は文字列パスの dict で受け、戻り値は MCP のコンテンツ形式 (`{"content": [{"type": "text", "text": ...}]}`) を返す。失敗時は `is_error: True` を付けて返し、例外はペルソナ側 / Round runner 側のリトライ機構に委ねる。

`call_worker` は `project_dir` の誤りに対して自己修復する: 渡されたパスから最大12階層まで祖先を遡って `project.toml` を探し、見つからなければ `in_dir` を起点に再探索する。ペルソナの Claude セッションが cwd (`runs/{run-id}/round-N/{persona}/`) を基準に相対パスを渡してくるケースを吸収するため。

#### 2.3.4 in-process MCP サーバの構築

`tools/registry.py::build_kessen_mcp_server()` が `create_sdk_mcp_server(name="kessen", version=__version__, tools=[...])` で単一の名前空間を作る。ツールを増やす場合はこのリストに足すだけでよい。

#### 2.3.5 ペルソナへの登録

ペルソナ TOML の `[engine_options]` で `use_kessen_mcp = true` を指定すると、`ClaudeAgentOptions.mcp_servers` に `{"kessen": ...}` が注入される。**サーバを注入しただけでは呼べない**ため、`allowed_tools` に `mcp__kessen__call_worker` 等を明示的に加える必要がある。

#### 2.3.6 ツール名前空間の規約

公開されるツール名は `mcp__kessen__<tool_name>` 形式。ログの進捗表示 (`_summarize_message`) では末尾セグメントのみを表示する。

---

## 3. ディレクトリ構成

```
kessen-dojo/
├── src/kessen/               基盤コード (案件非依存)
│   ├── cli.py                CLI エントリポイント
│   ├── logging_config.py     JST + 7世代ローテのロガー
│   ├── version.py
│   ├── auth/resolver.py      認証解決
│   ├── config/loader.py      TOML スキーマ + 動的 import
│   ├── orchestrator/         runner.py (run全体) / round.py (1ラウンド)
│   ├── personas/             base / claude / openai / grok / factory / path_guard
│   ├── workers/              base / llm_worker / factory
│   ├── evaluator/            base + scorers/{stub,llm_judge}
│   ├── knowledge/            retriever (階層マージ) / store (JSONL)
│   ├── state/                db (SQLite スキーマ) / repository
│   ├── api/                  server / state / routes/{health,runs}
│   └── tools/                registry / persona_tool / worker_tool
├── shared/                   案件横断の共有資産
│   └── knowledge/            全案件に注入される shared 層ナレッジ
│       └── principles.md
├── projects/{name}/          案件単位の独立空間
│   ├── project.toml          ラウンド数 / 参加者 / Evaluator / retry
│   ├── personas/{id}.toml    ペルソナ定義
│   ├── task/                 round-1 の初期 seed (in/ にコピーされる)
│   ├── knowledge/
│   │   ├── common/           案件共通ナレッジ (自動追記される run 履歴を含む)
│   │   └── per-persona/{id}/ ペルソナ個別ナレッジ (lessons.jsonl が自動追記される)
│   ├── evaluators/           案件固有 Evaluator (拡張ポイント)
│   ├── workers/{id}.toml     案件固有 Worker 定義 (拡張ポイント / 任意)
│   ├── persona_classes/      案件固有 Persona クラス (拡張ポイント / 任意)
│   └── runs/{run-id}/        実行成果物 (git 管理外)
├── tools/data/               サンプル案件が使うデータ
├── docs/                     本書 / OPS / NEW_PROJECT
├── scripts/                  補助スクリプト置き場
└── pyproject.toml
```

`shared/` の探索は `project_dir.parent.parent / "shared"` で行うため、**案件ディレクトリはリポジトリ直下の `projects/{name}/` に置くこと**が前提になる。別の場所に置くと `shared/personas` `shared/workers` のフォールバック解決が効かない (ナレッジの shared 層は `repo_root` 起点なので別系統)。

`runs/` と `logs/` は `.gitignore` 済み。

---

## 4. 拡張点（基底クラス）

案件側は原則としてこの3つの基底クラスの継承と TOML だけで表現する。

### 4.1 BasePersona

`src/kessen/personas/base.py`

```python
class BasePersona(ABC):
    base_prompt: str                      # サブクラスで上書きする既定指示文

    def __init__(self, persona_id, engine, project_dir, repo_root, config): ...

    @abstractmethod
    async def run(self, in_dir: Path, out_dir: Path, *, dry_run: bool = False) -> PersonaResult: ...

    def compose_system_prompt(self, extra: str = "") -> str: ...
    @staticmethod
    def compose_user_prompt(in_dir: Path, out_dir: Path) -> str: ...
```

`PersonaResult` のフィールド: `persona_id` / `status` (`"ok"` | `"dry_run"` | `"error"`) / `summary` / `output_files` / `error` / `had_runtime_warning`。

**system_prompt の合成順** (`compose_system_prompt`):

1. `base_prompt`
2. `# Role` — ペルソナ TOML の `description`
3. `extra` (呼び出し側が渡した追加指示、任意)
4. リトライfeedback読込規約 (§4.1.1)
5. 階層マージ済みナレッジ (§7)

**user_prompt の合成** (`compose_user_prompt`):

- `in_dir` 配下の全ファイルを再帰列挙して相対パスのリストとして提示する。ペルソナの `allowed_tools` から `Glob` を外した構成でもディレクトリ内容を把握できるようにするため、列挙は基盤側の責務。
- 出力は**ペルソナ自身が `Write` ツールで `out_dir` に書く**。生成すべきファイル名と様式は**ペルソナの `description` で個別に指定する**規約 (2026-05-06 設計レビュー)。基盤側はプログラム的なファイル生成を行わない。

#### 4.1.1 リトライfeedback読込規約

リトライ時、Round runner はペルソナの `in/` に `_retry_feedback.md` を書き込む。全ペルソナの system_prompt には次の規約が常に含まれる。

> `in/` ディレクトリに `_retry_feedback.md` がある場合、必ず最初に読むこと。これは前回試行で評価モデルが指摘した問題と修正提案である。今回の出力ではこれらをすべて解消する必要がある。

`OpenAIPersona` 系は入力ファイルを user_prompt に inline する方式のため、`_retry_feedback.md` を先頭に並べ替えて目立たせる。

### 4.2 BaseWorker

`src/kessen/workers/base.py`

```python
class BaseWorker(ABC):
    def __init__(self, worker_id, persona_id, project_dir, *,
                 engine=None, role="", engine_options=None, output_filename=None): ...

    @abstractmethod
    async def execute(self, in_dir: Path, out_dir: Path) -> WorkerResult: ...

    def append_lesson(self, kind: str, content: str, *, round: int | None = None) -> None: ...
```

Worker はペルソナ配下の「持ち場」であり、ペルソナとは次の点で異なる。

| | Persona | Worker |
|---|---|---|
| ナレッジ自動注入 | あり (§7) | **なし** (`in_dir` のファイルのみが入力) |
| ツール反復 | あり (エージェント動作) | なし (単発 chat) |
| 自前のリトライ | Round runner が付与 | **持たない**。失敗はペルソナ実行全体の再走で対処 |
| 知見の蓄積先 | `knowledge/per-persona/{id}/lessons.jsonl` | `knowledge/per-persona/{persona_id}/workers/{worker_id}/lessons.jsonl` |

ナレッジを注入しないのは意図的で、Worker に渡す背景情報の取捨選択はペルソナ側の責務 (`_handoff/{worker}/in/` などに staging する)。

標準実装の `LLMWorker` は `engine` 文字列で provider を切り替える (§8.1)。出力の扱いは次の2通り。

- Worker TOML に `output_filename` が指定されている場合 → 応答全体を `out_dir/<output_filename>` に書く。後続のペルソナがパスを決め打ちで `Read` できる (決定論的パス、2026-05-06 追加)。
- 未指定の場合 → 応答中の `=== FILE: <name> ===` 区切りで複数ファイルに展開する。区切りが無ければ `response.md` にフォールバック。ファイル名は `^[\w\-.]+$` に一致し、`..` を含まず `.` で始まらないものだけ採用する。

#### 4.2.1 ペルソナ内オーケストレーション

ペルソナが Worker を指揮する場合の配線:

1. ペルソナ TOML の `[engine_options]` に `use_kessen_mcp = true` を指定
2. 同じく `allowed_tools` に `"mcp__kessen__call_worker"` を追加
3. ペルソナは自セッションで入力を `_handoff/{worker}/in/` に staging し、`call_worker` を呼ぶ
4. Worker の成果物が `out_dir` に出るので、ペルソナがそれを読んで最終成果物にまとめる

Worker の engine / 追加指示 / engine_options は、ペルソナ TOML の `[workers.<id>]` セクションで Worker TOML の既定値を上書きできる (§5.5)。

### 4.3 BaseEvaluator

`src/kessen/evaluator/base.py`

```python
class BaseEvaluator(ABC):
    def __init__(self, project_dir: Path, config: dict, runtime: object | None = None): ...

    @abstractmethod
    async def validate(self, persona_out_dir: Path) -> ValidationResult: ...
    @abstractmethod
    async def score(self, persona_out_dir: Path) -> ScoreResult: ...

    async def evaluate(self, round_dir, validated_participants, round_n=1) -> Leaderboard: ...
    @staticmethod
    def list_output_files(out_dir, *, exclude_underscore=True) -> list[Path]: ...
```

**3段構成の役割分担**:

| メソッド | 呼ばれる場所 | 期待される処理 |
|---|---|---|
| `validate` | リトライループ内 (試行のたび) | 成果物が評価可能かの軽量チェック。失敗時は `issues` / `suggestions` を返す (それが `_retry_feedback.md` になる) |
| `score` | validate 通過後に1回 | 本評価。重い処理はここに置く |
| `evaluate` | ラウンド末尾 | 通過者を `score` して `Leaderboard` に集約。既定実装で足りることが多い |

- `config` は `project.toml` の `[evaluator]` から `class` キーを除いた dict がそのまま渡る。
- `list_output_files` は既定で `_` 始まりのファイル (`_retry_feedback.md` / `_dry_run_*.md` など基盤のメタファイル) を除外する。
- `runtime` はコンテナ実行系 (P6) 用の予約引数。現状は常に `None`。
- `ScoreResult.metrics` に `strategy_summary` キーを入れると、run 終了時に `knowledge/common/leaderboard_history.jsonl` へ転記され、次 run のペルソナに伝わる (§7.3.1)。

標準 scorer:

| クラス | 用途 | 特徴 |
|---|---|---|
| `StubEvaluator` | テスト / プロトタイプ | ファイル数 × 重み + 文字数 × 重み。LLM 呼出なし・認証不要・決定的 |
| `LLMJudgeEvaluator` | 機械評価が定義できない案件 | Claude (既定 `claude:haiku`) にルーブリックで採点させ、応答末尾の JSON から `score` / `note` を抽出。パース失敗時は 0.0 |

---

## 5. 設定ファイル仕様

すべて TOML。pydantic でバリデーションする。**トップレベルのモデルは `extra="forbid"`** なので、綴り間違いのキーはロード時に例外になる (`[engine_options]` などの自由記述セクションを除く)。

### 5.1 案件メタ `projects/{name}/project.toml`

```toml
[project]
name = "nk225-yorihike"      # 表示名。ディレクトリ名と一致させる必要はない
rounds = 3                   # ラウンド数 (CLI --rounds / API body で上書き可)
max_parallel = 1             # 1ラウンド内で同時実行するペルソナ数

[evaluator]                  # 省略時は StubEvaluator
class = "evaluators.nk225_evaluator:Nk225YorihikeEvaluator"
data_path = "../../tools/data/..."   # class 以外のキーは Evaluator の config に渡る
warmup_days = 30

[participants]
ids = ["rsi-contrarian", "momentum-turtles", "short-only"]

[retry]
max_attempts = 2             # 追加試行の回数。総試行数は max_attempts + 1
on_exhaustion = "exclude"    # "exclude" | "zero_score" (※現状 未実装、下記注記)
include_history_in_feedback = true
abort_run_on_all_failed = false
```

| セクション | キー | 既定値 | 意味 |
|---|---|---|---|
| `[project]` | `name` | 必須 | 案件表示名 |
| | `rounds` | `1` | ラウンド数 |
| | `max_parallel` | `1` | ペルソナの同時実行数 (`asyncio.Semaphore`) |
| `[evaluator]` | `class` | 省略可 | `module:ClassName`。省略時は `StubEvaluator` |
| | (その他) | — | Evaluator の `config` dict にそのまま渡る |
| `[participants]` | `ids` | 必須 | 参加ペルソナIDのリスト |
| `[retry]` | `max_attempts` | `0` | 追加試行数。`0` ならリトライなし (1回で確定) |
| | `on_exhaustion` | `"exclude"` | 試行尽きた後の扱い |
| | `include_history_in_feedback` | `true` | `_retry_feedback.md` に過去試行の指摘履歴も載せる |
| | `abort_run_on_all_failed` | `false` | 全員失敗したラウンドで run 全体を打ち切る |

> **注記**: `on_exhaustion` はスキーマとして受理されるが、現状のコードは値を参照していない。試行を使い切ったペルソナは常に `score=None` で leaderboard に残り、勝者選定から除外される (= `"exclude"` 相当の動作)。`"zero_score"` を指定しても挙動は変わらない (§11.2)。

### 5.2 ペルソナ定義 `projects/{name}/personas/{id}.toml`

```toml
[persona]
id = "rsi-contrarian"        # ファイル名と一致必須 (不一致は ValueError)
engine = "claude:sonnet-4-6" # "<provider>:<model>"
class = "..."                # 省略時は engine の provider から既定クラスを解決
description = """
このペルソナの役割・思想・生成すべきファイル名と様式をここに書く。
system_prompt の "# Role" セクションとしてそのまま注入される。
"""

[knowledge]
include = ["shared", "common", "private"]   # 注入するナレッジ層 (§7.1)

[engine_options]             # 自由記述。provider 別に解釈される
max_turns = 12
permission_mode = "acceptEdits"
# allowed_tools = ["Read", "Write", "Edit", "Grep"]
# use_kessen_mcp = true
# disable_path_guard = false

[workers]                    # 任意。Worker を使う場合のみ
ids = ["research"]
[workers.research]           # Worker TOML の既定値を、このペルソナ用に上書き
engine = "google:gemini-2.5-pro"
prompt_addon = "出力は日本語で、根拠となるデータを必ず併記すること。"
engine_options = { temperature = 0.3 }

[retry]                      # 省略時は project.toml の [retry] を継承
max_attempts = 3
```

`[engine_options]` の主なキー (ClaudePersona):

| キー | 既定値 | 意味 |
|---|---|---|
| `model` | `[persona].engine` の model 部 | モデル ID の上書き |
| `max_turns` | `12` | エージェントの最大ターン数 |
| `permission_mode` | `"acceptEdits"` | SDK の権限モード |
| `allowed_tools` | `["Read", "Write", "Edit", "Glob", "Grep", "Bash"]` | 利用可能ツール。ビルトイン分は `tools=` にも渡され、実際に利用可否のゲートになる (§2.2) |
| `use_kessen_mcp` | `false` | kessen MCP サーバの注入 (§2.3.5) |
| `disable_path_guard` | `false` | PreToolUse hook の無効化 (テスト用) |

OpenAI / xAI 系ペルソナの `[engine_options]`: `max_completion_tokens` (既定 4000) / `temperature` (既定 0.7) / `max_chars_per_file` (既定 8000)。

`[workers.<id>]` の各キーは省略可 (未指定なら Worker TOML の既定値を使う)。`prompt_addon` は Worker の `role` に追記される。

### 5.3 横断ペルソナ・Worker `shared/{personas,workers}/{id}.toml`

ペルソナ / Worker の TOML は次の順で探索され、最初に見つかったものを使う。

1. `projects/{name}/personas/{id}.toml` (または `workers/{id}.toml`)
2. `shared/personas/{id}.toml` (または `shared/workers/{id}.toml`)

案件横断で同じペルソナを再利用したい場合は `shared/` に置く。どちらにも無ければ `FileNotFoundError`。

### 5.4 動的import規約

- クラス指定は必ず `"module.path:ClassName"` 形式。`:` が無い文字列は `ValueError`。
- `load_class(class_path, project_dir=...)` は `project_dir` の絶対パスを `sys.path` 先頭に挿入してから import する。これにより案件配下の `evaluators/` `workers/` `persona_classes/` がトップレベルモジュールとして解決される (例: `"evaluators.nk225_evaluator:Nk225YorihikeEvaluator"`)。
- 解決されたオブジェクトがクラスでない場合は `TypeError`、期待する基底クラスのサブクラスでない場合も `TypeError`。
- provider から引かれる既定クラス:

| provider | 既定 Persona クラス |
|---|---|
| `claude` | `kessen.personas.claude_persona:ClaudePersona` |
| `openai` | `kessen.personas.openai_persona:OpenAIPersona` |
| `xai` | `kessen.personas.grok_persona:GrokPersona` |

Worker の既定クラスは `kessen.workers.llm_worker:LLMWorker`。

### 5.5 Worker定義 `projects/{name}/workers/{id}.toml`

```toml
[worker]
id = "research"                    # ファイル名と一致必須
engine = "google:gemini-2.5-pro"
class = "..."                      # 省略時 LLMWorker
role = "与えられた資料から論点を抽出し、根拠付きで整理する専門家。"
output_filename = "research.md"    # 省略時は FILE マーカー方式 (§4.2)

[engine_options]
max_output_tokens = 8192
temperature = 0.3
max_chars_per_file = 8000
```

> **注記**: `[workers].ids` は宣言用のリストで、基盤側はこれを列挙して自動実行しない。Worker は `call_worker` (MCP) か案件コードからの `build_worker()` 呼び出しで起動する (§11.2)。

---

## 6. ラウンド実行フロー

`run_competition()` (`orchestrator/runner.py`) が run 全体を、`run_round()` (`orchestrator/round.py`) が1ラウンドを担当する。

```
run_competition(project_dir, repo_root, rounds_override, run_id, from_round)
 ├─ run_id 発行 (JST: "%Y-%m-%dT%H-%M-%S")
 ├─ project.toml ロード → Evaluator を instantiate
 ├─ state.json 初期化 (status="running")
 ├─ seed_dir = projects/{name}/task/   (round-1 の初期 seed)
 └─ for r in from_round..rounds:
      ├─ leaderboard.json が既にある → スキップ (resume)
      ├─ run_round(...)
      │    ├─ 各ペルソナの in/ に seed_dir の中身をコピー
      │    ├─ Semaphore(max_parallel) で並列に run_persona_with_retry()
      │    ├─ status=="ok" のペルソナだけ evaluator.evaluate() で採点
      │    ├─ 失敗ペルソナを score=None で追加 → finalize() で勝者確定
      │    ├─ knowledge/per-persona/{id}/lessons.jsonl に結果を追記
      │    └─ round-N/leaderboard.json を書く
      ├─ all_failed かつ abort_run_on_all_failed → 打ち切り
      └─ 勝者の out/ から次ラウンドの seed を構築
 ├─ 全ラウンド × 全ペルソナで最高スコアの1件を overall winner に選ぶ
 ├─ final.json / summary.md を書く
 ├─ knowledge/common/leaderboard_history.jsonl に run 総括を1行追記
 └─ state.json を status=completed/aborted で更新
```

**成果物ディレクトリ構成**:

```
projects/{name}/runs/{run-id}/
├── state.json                     進捗 (run_id / status / current_round / started_at / finished_at)
├── final.json                     機械可読の最終結果 (CompetitionResult)
├── summary.md                     人間可読サマリ
├── round-{N}/
│   ├── leaderboard.json           そのラウンドの順位表
│   └── {persona-id}/
│       ├── in/                    seed + _retry_feedback.md
│       ├── out/                   最終試行の成果物 (attempts から昇格)
│       ├── attempts/attempt-{K}/
│       │   ├── out/               その試行の生成物
│       │   └── validation.json    ok / issues / suggestions / error
│       └── retry_summary.json     total_attempts / final_status
└── _seeds/round-{N+1}/            次ラウンドへの引き継ぎ
    ├── previous_winner/           勝者の out/ 一式 (勝者不在なら無し)
    ├── previous_leaderboard.json
    └── _leaderboard_summary.md    全ラウンドの推移を中立視点でまとめた md
```

**勝者選定のルール**:

- ラウンド勝者 = そのラウンドで `status=="ok"` かつ `score` が最大のペルソナ (`Leaderboard.finalize()`)。
- 総合勝者 = **全ラウンド × 全ペルソナのエントリのうち最高スコア1件**。最終ラウンドの勝者とは限らない (`_pick_overall_winner_detail`)。
- `final.json.winner_artifacts` には総合勝者の `out/` 配下のファイルパスが入る (`_` 始まりと `__pycache__` は除外、`strategy.py` → `notes.md` → `README.md` の順で優先表示)。

### 6.1 状態管理

状態は3系統に分かれる。互いに独立して読める。

| 系統 | 実体 | 書き手 | 用途 |
|---|---|---|---|
| ファイル | `runs/{run-id}/state.json` | `runner._write_state` | CLI/API どちらでも常に書かれる進捗 |
| ファイル | `round-N/leaderboard.json`、`final.json`、`summary.md` | Round runner / Competition runner | 結果の正 (resume 判定にも使う) |
| DB | `runs.db` の `runs` / `round_scores` | API 層 (`RunRepository`) | 一覧・横断クエリ・キャンセル対象の特定 |

`run-once` (CLI) は DB を使わない。DB に記録が残るのは `kessen serve` 経由で起動したランのみ。

`runs.status` が取りうる値: `pending` / `running` / `completed` / `failed` / `cancelled` / `aborted` / `interrupted`。`interrupted` はサーバ起動時のクリーンアップ (`cleanup_stale_running`) が、前回プロセス終了時に `running`/`pending` のまま残っていたランに付ける。

### 6.2 リトライ機構

`run_persona_with_retry()` の1ペルソナあたりのループ:

```
attempt = 0
loop:
  attempt_no = attempt + 1
  attempts/attempt-{attempt_no}/out/ を作る
  attempt > 0 かつ include_history_in_feedback → in/_retry_feedback.md を書く
  persona.run(in_dir, attempt_out)
    ├─ 例外 → validation.json に error を記録
    │         試行が尽きていれば status="runtime_failed" で終了、そうでなければ次試行
    └─ PersonaResult.status=="error" → 警告ログのみ (成果物があるので validate に委ねる)
  evaluator.validate(attempt_out) → validation.json
    ├─ ok  → out/ に昇格して status="ok" で終了
    └─ ng  → issues を蓄積。試行が尽きていれば status="validation_failed" で終了
```

- **総試行数は `max_attempts + 1`**。`max_attempts = 2` なら初回 + 追加2回 = 最大3回。
- `_retry_feedback.md` には `include_history_in_feedback = true` のとき過去全試行の指摘が積み上がる。
- 試行が尽きた場合も、最後の試行の成果物は `out/` に昇格する (中身を人間が確認できるようにするため)。ただし `status != "ok"` なので採点対象にはならない。
- `PersonaResult.status == "error"` を即失敗にしないのは、Claude Agent SDK が成果物を書き終えた後に内部エラーを返すケースがあるため (§14)。この場合 `metrics.had_runtime_warning = true` が leaderboard に記録される。

### 6.3 キャンセルとレジューム

**キャンセル** (Ctrl+C または `POST /rounds/{run_id}/cancel`) は、書きかけの成果物を捨てずに終了する。

1. 各ペルソナのタスクが `CancelledError` を受け取り、進行中 attempt の `validation.json` を書き、`out/` へ昇格し、`retry_summary.json` を `final_status="cancelled"` で書く
2. `run_round` が全ペルソナの後始末を待ち、**部分 leaderboard** を書く (完了済みは結果を反映、未完了は `status="cancelled"`)。キャンセル中は重い `score()` を走らせないため、validate まで通っていたペルソナも `score=None` になる
3. `run_competition` が `final.json` / `summary.md` を `status="cancelled"` で書き、`state.json` を更新する。**この経路では `knowledge/common/leaderboard_history.jsonl` に追記しない** (未完了の結果でナレッジを汚さないため)

**レジューム** (`POST /rounds/{run_id}/resume`) は REST API のみ。`current_round` の `leaderboard.json` の有無を見て、完了済みなら次ラウンドから、未完了なら同ラウンドからやり直す。すでに `leaderboard.json` があるラウンドはループ内でもスキップされるため、完了済みラウンドが再実行されることはない。CLI には resume サブコマンドが無く、`run-once --run-id` で同じ run-id を指定すると同様にスキップが効く。

---

## 7. ナレッジ管理

ラウンド間・run 間で学習を持ち越すための仕組み。ペルソナの system_prompt 末尾に Markdown 断片として自動注入される。

### 7.1 階層と優先度

| 層 | パス | 優先度 | 主な書き手 |
|---|---|---|---|
| `shared` | `shared/knowledge/` (repo root 起点) | 弱 | 人間 |
| `common` | `projects/{name}/knowledge/common/` | 中 | 人間 + 基盤 (run 履歴) |
| `private` | `projects/{name}/knowledge/per-persona/{persona_id}/` | 強 | 人間 + 基盤 (lessons) |

注入する層はペルソナ TOML の `[knowledge].include` で選べる (既定は3層すべて)。存在しない層は黙ってスキップされ、全層空でも例外にはならない (空文字が返る)。

### 7.2 マージ規則

| 形式 | 規則 |
|---|---|
| `.md` | **相対パスが同名なら強い層が上書き**。異なる名前は全て併記される |
| `.jsonl` | 全層のエントリを集めて `ts` 降順にソート、**先頭20件**のみ注入 (`DEFAULT_MAX_JSONL`) |
| `.json` / `.toml` | 同名ファイルをキー単位でディープマージ (強い層が勝つ) |

> **運用上の落とし穴**: `shared/knowledge/principles.md` と `projects/{name}/knowledge/common/principles.md` は同じ相対パス `principles.md` なので、案件側が shared 側を**完全に隠す**。両方を効かせたい場合はファイル名を分ける (例: `shared_principles.md`)。実際の注入内容は `kessen knowledge-show` で確認できる。

注入される Markdown の構造:

```markdown
# Knowledge (auto-injected)

## Documents
### <相対パス> (layer=<層>)
<本文>

## Structured Data
### <相対パス>
```json ... ```

## Recent Logs (latest N)
- `<ts>` [<layer>] <kind> persona=<id> round=<n>: <content>
```

### 7.3 JSONLスキーマ（最小定義）

```json
{
  "ts": "2026-05-06T21:30:00+09:00",
  "layer": "private",
  "kind": "lesson",
  "content": "round=1 score=0.883 attempts=1\ncorr(cum,t)=0.883, ...",
  "persona_id": "rsi-contrarian",
  "round": 1,
  "extra": {}
}
```

- `ts` は JST の ISO 8601 (秒精度)。
- `layer` は `shared` / `common` / `private`。
- `kind` は `feedback` / `lesson` / `hypothesis` / `error` / `validation_failed` / `runtime_failed` を想定するが、文字列なので案件側で拡張してよい。
- `persona_id` / `round` / `extra` は空なら書き出し時に省略される。破損行は読み出し時にスキップされる。

#### 7.3.1 基盤が自動で書くナレッジファイル

| パス | 書き手 | タイミング | 内容 |
|---|---|---|---|
| `knowledge/per-persona/{id}/lessons.jsonl` | `round._write_round_knowledge` | 各ラウンド終了時 | そのペルソナの score / status / attempts / 評価note または last_issues |
| `knowledge/common/leaderboard_history.jsonl` | `runner._write_run_history` | run 完了時 (キャンセル時は書かない) | run 総括。最終ラウンドの全ペルソナを順位付きで、`note` と `metrics.strategy_summary` 付きで記録。同一 `run_id` は重複追記しない |

> **2026-05-06 に廃止**: ラウンド勝者のフィードバックを `knowledge/common/feedback.jsonl` に書く経路は削除された。run 完了時の `leaderboard_history.jsonl` が全エントリを含む上位互換であり、中間ラウンドの推移は `_seeds/round-N/_leaderboard_summary.md` として各ペルソナの `in/` に配られるため。

人間が置く典型ファイル: `knowledge/common/principles.md` (案件共通の作法)、`knowledge/per-persona/{id}/playbook.md` (ペルソナ個別の手順書)。

### 7.4 Claude Agent SDK との統合

- ナレッジは system_prompt に**文字列として**注入する。SDK の設定ファイル探索には依存しない。
- すべての SDK 呼び出しで `setting_sources=[]` を指定し、`.claude/` 配下 (CLAUDE.md / settings / スラッシュコマンド) の自動ロードを抑止する。開発者の手元設定がペルソナの挙動に混入すると再現性が失われるため。
- `logs/persona.{id}.log` に `[turn N] AssistantMessage: tool=Read` 形式の進捗が出る。長時間の沈黙は SDK の停滞を疑うサイン。

---

## 8. マルチプロバイダLLM

### 8.1 エンジン指定文字列

`"<provider>:<model>"` 形式。`parse_engine()` が分解し、`:` が無い / いずれかが空なら `ValueError`。

| provider | Persona | Worker | 認証に使う環境変数 |
|---|---|---|---|
| `claude` | `ClaudePersona` | `LLMWorker` (Agent SDK 単発) | `ANTHROPIC_API_KEY` または `claude` CLI ログイン |
| `openai` | `OpenAIPersona` | `LLMWorker` (openai SDK) | `OPENAI_API_KEY` |
| `xai` | `GrokPersona` | `LLMWorker` (openai SDK + `base_url`) | `XAI_API_KEY` |
| `google` | — (未実装) | `LLMWorker` (google-genai SDK) | `GOOGLE_API_KEY` または `GEMINI_API_KEY` |

Claude のモデル部は `to_claude_model_id()` で正規化される: `opus` / `sonnet` / `haiku` / `inherit` はエイリアスとしてそのまま、`claude-` 始まりはそのまま、それ以外は `claude-` を前置 (`sonnet-4-6` → `claude-sonnet-4-6`)。

### 8.2 実装方式

| | Claude | OpenAI / xAI | Google |
|---|---|---|---|
| 経路 | `claude_agent_sdk.query()` | `openai.AsyncOpenAI` の `chat.completions` | `google-genai` の `client.aio.models.generate_content` |
| ツール反復 | あり (Persona) / なし (Worker) | なし | なし |
| 成果物の書き出し | ペルソナ自身が `Write` ツールで書く | 応答テキストを基盤がファイルへ展開 (`=== FILE: ===` / `response.md`) | 同左 |
| `temperature` / トークン上限 | **効かない** (`ClaudeAgentOptions` が受け取らない) | `temperature` / `max_completion_tokens` | `temperature` / `max_output_tokens` |

`LLMWorker` は応答の `finish_reason` を検査し、正常終了 (`STOP` / `stop` / `completed` など) 以外なら WARNING を出す。`MAX_TOKENS` / `length` の場合は「`max_output_tokens` / `max_completion_tokens` を引き上げよ」と具体的に促す。応答が途中で切れた疑いがあるときは、まずこのログを確認する。

### 8.3 認証

`kessen.auth.resolver` に集約する。**環境変数を各モジュールから直接読まない**。

Claude の解決順:

1. `ANTHROPIC_API_KEY` があれば APIキー方式 (`ClaudeAuthMode.API_KEY`)
2. `claude` コマンドが PATH にあればサブスクリプション方式 (`SUBSCRIPTION`)。ログイン状態の検証は SDK に委ねる
3. どちらも無ければ `NONE` (例外は投げない。必要な呼び出し側が `require_claude_auth()` で `MissingCredentialError` にする)

`kessen info` で現在の解決結果を確認できる。APIキーは表示時に必ずマスクされる (`masked_api_key()`)。

---

## 9. REST API

`kessen serve` (既定 `127.0.0.1:8788`) で FastAPI アプリを起動する。OpenAPI ドキュメントは `/docs`。

| メソッド | パス | 説明 |
|---|---|---|
| `GET` | `/health` | `{"status": "ok", "version": "..."}` |
| `POST` | `/projects/{project_name}/rounds` | ラン開始。即座に `202` + run_id を返し、実行はバックグラウンドタスクで進む |
| `GET` | `/projects/{project_name}/rounds` | ラン一覧 (`status` / `limit` でフィルタ、`started_at` 降順) |
| `GET` | `/projects/{project_name}/rounds/{run_id}` | ラン状態 + ラウンド別スコア + `final.json` + `is_active` |
| `GET` | `/projects/{project_name}/rounds/{run_id}/leaderboard` | leaderboard をファイルから返す (`?round=N` で単一ラウンド) |
| `POST` | `/projects/{project_name}/rounds/{run_id}/cancel` | 実行中タスクをキャンセル |
| `POST` | `/projects/{project_name}/rounds/{run_id}/resume` | 中断されたランを再開 |

- `{project_name}` は**ディレクトリ名**。`{repo_root}/projects/{project_name}/project.toml` の存在で解決する (無ければ `404`)。
- `POST /rounds` のボディ: `{"rounds": 3, "run_id": "..."}` (どちらも省略可)。既存 run_id の指定は `409`。
- `cancel` は `pending`/`running` 以外なら何もせず `cancelled: false` を返す。
- `resume` は `running`/`pending` なら `409` (先に cancel が必要)、`completed` なら `409`、全ラウンド完了済みなら `409`。

**認証は実装されていない。** バインド先は既定でループバックであり、そのまま外部公開してはならない (§10 / OPS §9)。

---

## 10. 信頼境界と基盤コンテナ群 (P6 計画)

### 10.1 現状の信頼境界

kessen はペルソナに**ホスト上でのコード実行を許している**。これは意図された設計で、次の前提に立っている。

- ペルソナは既定で `Bash` を含むツールを `permission_mode="acceptEdits"` で使える (ペルソナ TOML の `allowed_tools` で絞れる)
- 案件 Evaluator は生成された Python (例: `out/strategy.py`) を**ホストの venv に import して実行する**
- したがって、**信頼できないモデル / プロンプトを本基盤に投入することは、ホストで任意コードを実行させることと等価**

現状の緩和策は PreToolUse hook (`personas/path_guard.py`) による読み取り制限のみ。

| 制限対象 | 理由 |
|---|---|
| `shared/datawarehouse/` 配下 | データは Evaluator 経由でのみ配給する。ペルソナが原データを直接覗くと評価の前提が崩れる |
| `docs/` 配下 | 開発者向けの仕様書。ペルソナが読むと評価対象の課題に対する「答え」を先取りしかねない |

- 対象ツール: `Read` / `Edit` / `Write` / `Glob` / `Grep` / `NotebookEdit` / `Bash`
- `Bash` は `command` 文字列の正規表現マッチで判定する。**完全な保証ではない** (パス組み立てを難読化すれば回避しうる)。あくまで事故防止のガードレール
- 許可されるもの: `shared/personas/` `shared/knowledge/` `shared/workers/`、案件配下、自分のラウンド作業ディレクトリ
- `engine_options.disable_path_guard = true` で無効化できる (テスト用)

### 10.2 コンテナ実行 (未実装)

上記の制約を構造的に解くため、ペルソナ実行と評価をコンテナに隔離する計画がある。`BaseEvaluator.__init__` の `runtime` 引数はそのための予約枠で、`DockerRunner` 相当のオブジェクトを受け取り、`score()` 内のコード実行を移譲する想定。現時点では常に `None` が渡る。

---

## 11. 実装フェーズ

### 11.1 P0–P5 到達点 (実装済み)

| フェーズ | 内容 | 主な実体 |
|---|---|---|
| P0 | 基盤土台 (パッケージ / ロガー / 認証 / CLI 骨格) | `logging_config` `auth` `cli` |
| P1 | ペルソナ単発実行 | `BasePersona` `ClaudePersona` `build_persona` `kessen persona-run` |
| P2 | ラウンド実行 + リトライ + 評価 | `run_round` `run_competition` `BaseEvaluator` `kessen run-once` |
| P3 | マルチプロバイダ + MCPツール定義 | `OpenAIPersona` `GrokPersona` `call_persona` `registry` |
| P4 | REST API + 状態DB | `kessen serve` `runs.db` `cancel` / `resume` |
| P5 | ナレッジ循環 + Worker 層 | `KnowledgeRetriever` `lessons.jsonl` `leaderboard_history.jsonl` `BaseWorker` `LLMWorker` `call_worker` |

キャンセル時のフラッシュ (2026-05-05)、`tools=` によるツールゲート修正と `output_filename` 導入 (2026-05-06) は P5 期の後追い改修として取り込まれている。

### 11.2 未実装・改善候補

| 項目 | 現状 | 影響 |
|---|---|---|
| P6: 基盤コンテナ + `DockerRunner` | 未着手。`BaseEvaluator.runtime` は常に `None` | ペルソナ生成コードがホストで実行される (§10) |
| `retry.on_exhaustion` | スキーマのみ。コードが値を参照していない | `"zero_score"` を指定しても `"exclude"` と同じ動作 |
| `[workers].ids` | 宣言のみ。基盤は列挙しない | Worker の起動はペルソナ側 (`call_worker`) か案件コードの責務 |
| 自動テスト | `tests/` が存在しない (`pyproject.toml` の `testpaths` は設定済み) | 回帰検知が手動確認頼み |
| CLI からの resume | サブコマンド無し。`--run-id` 指定によるラウンドスキップで代用 | 中断復旧は REST API 経由が正道 |
| `google:` ペルソナ | Worker のみ対応。Persona 側に既定クラスが無い | ペルソナで Gemini を使うには `[persona].class` を明示した独自実装が必要 |
| ナレッジ注入量の制御 | JSONL は常に最新20件固定 (`compose(max_jsonl=...)` は内部APIとしては可変) | 長期運用でどのログを残すかは手動メンテに依存 (OPS §4) |
| `_collect_winner_artifacts` の並び順 | 基盤コードに案件固有のファイル名 (`strategy.py` / `notes.md`) が優先順位として埋まっている | `final.json.winner_artifacts` の表示順のみに影響。「基盤に案件特化を書かない」原則からの逸脱 |

---

## 12. 収録サンプル案件

| 案件 | 目的 | Evaluator | ラウンド | 認証要件 |
|---|---|---|---|---|
| `projects/sample` | 基盤の疎通確認。LLM 応答の質を問わない | `StubEvaluator` (ファイル数 + 文字数) | 1 | Claude のみ |
| `projects/nk225-yorihike` | 実用例。3つのトレード思想が寄り引け戦略を競う | 案件固有 (`evaluators/nk225_evaluator.py`) | 3 | Claude のみ |

`nk225-yorihike` は「案件固有 Evaluator を書き、標準 `ClaudePersona` を `description` だけで差別化する」構成の実例で、案件を新設する際の雛形として読む価値がある。詳細は [projects/nk225-yorihike/README.md](../projects/nk225-yorihike/README.md)。

> `[project].name` はあくまで表示名で、CLI の `--project` と API の `{project_name}` は**ディレクトリ名**で指定する (`--project projects/sample`)。両者は一致させておくのが無難。

---

## 13. 意思決定履歴

| 日付 | 決定 | 理由 |
|---|---|---|
| — | 成果物の受け渡しをファイル (ディレクトリ) に統一 | プロセス跨ぎ・再開・人間の目視確認をすべて同じ仕組みで賄うため |
| — | 総合勝者を「最終ラウンド勝者」でなく「全ラウンド最高スコア」にする | ラウンドを重ねて悪化した場合に、良かった案を取りこぼさないため |
| — | Worker にナレッジを自動注入しない | 何を渡すかをペルソナの判断に委ね、Worker のコンテキストを小さく保つため |
| 2026-05-05 | キャンセル時に部分成果物と部分 leaderboard をフラッシュする (P1改修) | 数十分実行した中間結果を Ctrl+C で失わないため |
| 2026-05-06 | ペルソナの成果物生成を「基盤が書く」から「ペルソナが `Write` する」へ変更 | ペルソナ種別ごとに異なる出力構造を取れる自由度を確保するため |
| 2026-05-06 | `compose_user_prompt` が `in/` のファイル一覧を明示する | `allowed_tools` から `Glob` を外すとエージェントがディレクトリを列挙できなくなるため |
| 2026-05-06 | `tools=` にビルトインツール集合を渡す | `allowed_tools` は auto-allow リストであってゲートではなく、`acceptEdits` と併用すると `Bash` 等が暗黙に通っていたため |
| 2026-05-06 | Worker TOML に `output_filename` を追加 | 出力パスを決定論的にし、後続ペルソナが `Glob` 無しで `Read` できるようにするため |
| 2026-05-06 | `knowledge/common/feedback.jsonl` 経路を廃止 | run 完了時の `leaderboard_history.jsonl` が上位互換。中間推移は `_leaderboard_summary.md` が担う |
| — | `finish_reason` の異常値を WARNING でログする | 応答の途中切断を「バッファ不具合」と誤認する調査コストを避けるため |

---

## 14. 過去の問題と対策

### 14.1 Claude Agent SDK 周辺

| 症状 | 原因 / 対策 |
|---|---|
| 最終メッセージ (FINAL) の後に `exit code 1` / `Fatal error in message reader` が出る | claude CLI サブプロセス側の後始末に起因するノイズであることが多い。成果物が書けていれば validate に委ねる方針にしてある (`PersonaResult.status="error"` でも即失敗にしない)。調査用に `stderr` コールバックで CLI の標準エラー末尾30行を ERROR ログに出す実装が入っている。並列度を上げると出やすいため、案件によっては `max_parallel = 1` を選ぶ |
| `allowed_tools` に入れたのに MCP ツールが呼べない | `mcp_servers` への登録 (`use_kessen_mcp = true`) と `allowed_tools` への追加は**両方必要** |
| `mcp__*` を `tools=` に渡すと SDK が落ちる | MCP ツールはビルトイン名として解釈されるため。`tools=` にはビルトインのみを渡す (実装済み) |
| 開発者の `.claude/` 設定がペルソナに漏れる | 全 SDK 呼び出しで `setting_sources=[]` を指定する (規約) |

### 14.2 LLM 応答

| 症状 | 原因 / 対策 |
|---|---|
| 応答が途中で切れる | まず `finish_reason` の WARNING ログを見る。`MAX_TOKENS` / `length` なら `engine_options.max_output_tokens` / `max_completion_tokens` を引き上げる |
| `output_filename` 指定なのに `=== FILE: ===` の文字列がそのまま保存される | 旧プロンプトのマーカー説明が残っている場合の症状。`output_filename` 指定時はマーカー説明を出さない実装になっている |
| ペルソナが `temperature` を設定しても効かない | `ClaudeAgentOptions` は `temperature` / `max_tokens` を受け取らない。Claude では CLI / モデル既定が使われる (§8.2) |

### 14.3 案件・設定

| 症状 | 原因 / 対策 |
|---|---|
| TOML のキー名を間違えるとロード時に例外 | トップレベルモデルは `extra="forbid"`。意図的な自由記述は `[engine_options]` / `[evaluator]` のみ |
| ペルソナ ID とファイル名の不一致で `ValueError` | `personas/{id}.toml` の `[persona].id` はファイル名と一致必須 (Worker も同様) |
| 案件固有クラスが import できない | `class` は `"module:ClassName"` 形式か、案件配下に `__init__.py` があるかを確認する。`sys.path` には `project_dir` が挿入される (§5.4) |
| ナレッジを置いたのに注入されない | 同名 `.md` が強い層に存在して隠れている可能性。`kessen knowledge-show` で実際の注入内容を確認する (§7.2) |
| 案件を `projects/` 以外に置くと `shared/` が解決されない | `shared/` の探索は `project_dir.parent.parent` 起点 (§3) |

### 14.4 記録の雛形

新しい問題に遭遇したら、次の形式で本節に追記する。

```markdown
### YYYY-MM-DD: <短い問題タイトル>

- 症状:
- 原因:
- 対策 (コード変更 / 運用手順):
- 再発検知の方法:
```

---

## 15. 参照

- 運用手順・デバッグ: [OPS.md](OPS.md)
- 新規案件の追加: [NEW_PROJECT.md](NEW_PROJECT.md)
- サンプル案件の詳細: [projects/nk225-yorihike/README.md](../projects/nk225-yorihike/README.md)
- 開発ガイドライン: リポジトリ直下の `CLAUDE.md`

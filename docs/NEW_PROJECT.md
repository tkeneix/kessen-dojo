# 新規案件の追加ガイド

`projects/{name}/` を1つ作るだけで新しいコンペを開催できる。基盤 (`src/kessen/`) には案件特化コードを書かない。

前提知識: [PRD.md §3 ディレクトリ構成](PRD.md) / [§4 拡張点](PRD.md) / [§5 設定ファイル仕様](PRD.md)。

---

## 0. 全体像

| ステップ | 作るもの | 必須 |
|---|---|---|
| 1 | ディレクトリ | ✅ |
| 2 | `project.toml` | ✅ |
| 3 | `personas/{id}.toml` (2体以上) | ✅ |
| 4 | `task/` (初期入力) | ✅ (実質) |
| 5 | Evaluator | 標準で足りるなら不要 |
| 6 | `knowledge/` | 任意 |
| 7 | dry-run → 1ラウンド → フルラン | ✅ |

**案件ディレクトリはリポジトリ直下の `projects/` に置く。** `shared/` の探索が `project_dir.parent.parent` 起点のため、別の場所に置くと共有ペルソナ・共有 Worker が解決されない。

---

## 1. ディレクトリを作る

```bash
cd <repo-root>
mkdir -p projects/my-case/{personas,task,knowledge/common}
```

最終形の目安:

```
projects/my-case/
├── project.toml
├── README.md              # 案件の説明 (推奨)
├── personas/
│   ├── alpha.toml
│   └── beta.toml
├── task/                  # round-1 で各ペルソナの in/ にコピーされる
│   └── instruction.md
├── knowledge/
│   ├── common/            # 案件共通のナレッジ (基盤も run 履歴を追記する)
│   └── per-persona/{id}/  # ペルソナ個別のナレッジ (基盤も lessons を追記する)
├── evaluators/            # 案件固有の評価器を書く場合
│   ├── __init__.py
│   └── my_evaluator.py
└── runs/                  # 実行時に自動生成 (git 管理外)
```

---

## 2. `project.toml` を書く

まずは標準の `StubEvaluator` で疎通を取るのが早い (LLM ジャッジ不要・決定的・追加認証なし)。

```toml
[project]
name = "my-case"
rounds = 1                 # 疎通確認のうちは 1
max_parallel = 1           # 安定するまで逐次実行

[evaluator]
class = "kessen.evaluator.scorers.stub:StubEvaluator"
min_files = 1
file_count_weight = 1.0
char_count_weight = 0.001

[participants]
ids = ["alpha", "beta"]

[retry]
max_attempts = 1           # 総試行数は max_attempts + 1 = 2
on_exhaustion = "exclude"
abort_run_on_all_failed = false
```

キーの詳細は [PRD §5.1](PRD.md)。**トップレベルは `extra="forbid"`** なので、綴りを間違えるとロード時に例外になる。

---

## 3. ペルソナを定義する

`personas/{id}.toml` の `[persona].id` は**ファイル名と一致必須**。

```toml
# projects/my-case/personas/alpha.toml
[persona]
id = "alpha"
engine = "claude:sonnet-4-6"
description = """
<このペルソナの思想・立場・アプローチ>

## 生成する成果物
- `report.md` — 必須。以下のセクションをこの順で含めること:
  1. 結論
  2. 根拠 (入力データの引用付き)
  3. 想定される反論
"""

[knowledge]
include = ["shared", "common", "private"]

[engine_options]
max_turns = 12
permission_mode = "acceptEdits"
```

**`description` の書き方がこの基盤の肝**になる。基盤はファイルを代わりに生成しない。「どのファイル名で、どんな様式で書くか」を `description` に明記しないと、ペルソナは何も出力しないか、評価器が想定しない名前で出力する ([PRD §4.1](PRD.md))。

差別化のコツ:

- 思想・制約・禁止事項を具体的に書く (「順張りは採用しない」のような排他条件は効きやすい)
- 出力ファイル名と必須セクションを明示する
- 全ペルソナで**共通の要求**は `description` ではなく `task/instruction.md` か `knowledge/common/` に置く

`class` を省略すると engine の provider から既定クラスが引かれる ([PRD §5.4](PRD.md))。案件固有の Persona クラスが必要になるのは、実行手順そのものを変えたいとき (Worker の指揮など) に限られる。

---

## 4. `task/` に初期入力を置く

`projects/{name}/task/` の中身が round-1 で各ペルソナの `in/` に丸ごとコピーされる (ディレクトリ規約)。

```
task/
├── instruction.md      # 課題・評価方法・制約 (全ペルソナ共通)
└── data_preview.csv    # 必要なら入力データやそのプレビュー
```

`instruction.md` に書くべきこと:

1. 何を作るのか (成果物の目的)
2. **どう評価されるのか** — Evaluator の判定基準を隠さず書く。隠すとペルソナは的外れな最適化をする
3. 制約 (使ってよいライブラリ、禁止事項、外部 I/O の可否)
4. 出力フォーマットの要求

> `docs/` と `shared/datawarehouse/` はペルソナから読めない ([PRD §10](PRD.md))。これらを参照するよう指示してはいけない。

---

## 5. Evaluator を用意する

### 5.1 標準で足りる場合

| Evaluator | 使いどころ |
|---|---|
| `kessen.evaluator.scorers.stub:StubEvaluator` | 疎通確認。ファイル数と文字数で機械的に採点 |
| `kessen.evaluator.scorers.llm_judge:LLMJudgeEvaluator` | 機械的な指標を定義できない案件。ルーブリックを渡して Claude に採点させる |

`LLMJudgeEvaluator` の設定例:

```toml
[evaluator]
class = "kessen.evaluator.scorers.llm_judge:LLMJudgeEvaluator"
judge_model = "claude:haiku"
min_files = 1
max_chars_per_file = 8000
rubric = """
0.0 から 1.0 のスコアを以下の観点で判定してください:
1. 結論の明確さ
2. 根拠とデータの対応
3. 反論への備え
"""
```

### 5.2 案件固有の Evaluator を書く場合

```python
# projects/my-case/evaluators/my_evaluator.py
from __future__ import annotations

from pathlib import Path

from kessen.evaluator.base import BaseEvaluator, ScoreResult, ValidationResult


class MyEvaluator(BaseEvaluator):
    """config は project.toml の [evaluator] から class を除いた dict が入る。"""

    async def validate(self, persona_out_dir: Path) -> ValidationResult:
        """軽量チェックのみ。重い処理は score() へ。"""
        report = persona_out_dir / "report.md"
        if not report.exists():
            return ValidationResult(
                ok=False,
                issues=["out/report.md が存在しない"],
                suggestions=["out/ 直下に report.md を作成してください"],
            )
        text = report.read_text(encoding="utf-8")
        issues: list[str] = []
        for section in ("## 結論", "## 根拠"):
            if section not in text:
                issues.append(f"必須セクション {section} が見つからない")
        return ValidationResult(
            ok=not issues,
            issues=issues,
            suggestions=["description で指定された全セクションを含めてください"] if issues else [],
        )

    async def score(self, persona_out_dir: Path) -> ScoreResult:
        """validate 通過後の本評価。"""
        files = self.list_output_files(persona_out_dir)   # '_' 始まりは自動除外
        total = sum(len(p.read_text(encoding="utf-8", errors="ignore")) for p in files)
        score = min(1.0, total / 8000)
        return ScoreResult(
            score=round(score, 6),
            note=f"{len(files)} files, {total} chars",
            metrics={"files": len(files), "chars": total},
        )
```

```bash
touch projects/my-case/evaluators/__init__.py
```

```toml
[evaluator]
class = "evaluators.my_evaluator:MyEvaluator"
```

実装の指針:

- **`validate` は軽く、`score` は重く。** `validate` はリトライのたびに呼ばれる。ここでの `issues` / `suggestions` がそのまま `_retry_feedback.md` としてペルソナに返るので、**修正可能な具体的な指摘**を書く。
- `score` は数値が大きいほど良い前提。相対順位が付けばよく、範囲の制約はない。
- `ScoreResult.metrics` に `strategy_summary` を入れると run 履歴に転記され、次 run のペルソナに伝わる ([PRD §7.3.1](PRD.md))。
- 生成コードを実行する評価器を書く場合、**それはホスト上での任意コード実行**であることを理解しておく ([PRD §10](PRD.md))。モジュール名の衝突を避けるため、import は毎回ユニークなモジュール名で行う (`nk225_evaluator.py` の `_import_strategy` が実例)。

---

## 6. ナレッジを置く (任意)

| 置き場 | 用途 |
|---|---|
| `knowledge/common/*.md` | 案件共通の作法。全ペルソナの system_prompt に入る |
| `knowledge/per-persona/{id}/*.md` | ペルソナ個別のプレイブック |

`shared/knowledge/` と**同じファイル名**を使うと shared 側が完全に隠れる ([PRD §7.2](PRD.md))。共存させたいならファイル名を分ける。

`.jsonl` は基盤が自動追記する領域なので、人間は `.md` を使う。

---

## 7. 動かして確認する

### 7.1 プロンプトの確認 (LLM 呼出なし)

```bash
mkdir -p /tmp/my-case/{in,out}
cp projects/my-case/task/* /tmp/my-case/in/

uv run kessen persona-run \
  --project projects/my-case --persona alpha \
  --in-dir /tmp/my-case/in --out-dir /tmp/my-case/out --dry-run

cat /tmp/my-case/out/_dry_run_system_prompt.md
```

確認ポイント: `# Role` に `description` が入っているか / ナレッジが期待通り注入されているか / 入力ファイル一覧が正しいか。

### 7.2 ナレッジ注入の確認

```bash
uv run kessen knowledge-show --project projects/my-case --persona alpha
```

### 7.3 1ラウンド実行

```bash
uv run kessen run-once --project projects/my-case --rounds 1
```

確認ポイント:

```bash
RUN=projects/my-case/runs/$(ls -t projects/my-case/runs | head -1)
cat $RUN/summary.md
jq '.entries[] | {persona_id, score, status, attempts, note}' $RUN/round-1/leaderboard.json
cat $RUN/round-1/alpha/attempts/attempt-1/validation.json    # 失敗した場合
```

### 7.4 フルラン

疎通が取れたら `project.toml` の `rounds` を増やす (2〜3から)。ラウンドを重ねると、勝者の `out/` が `_seeds/round-N/previous_winner/` 経由で全ペルソナの `in/` に配られ、`lessons.jsonl` が system_prompt に注入される。

---

## 8. チェックリスト

- [ ] 案件ディレクトリが `projects/{name}/` にある
- [ ] `[persona].id` と TOML ファイル名が一致している (Worker も同様)
- [ ] `description` に**生成すべきファイル名と様式**が書かれている
- [ ] Evaluator の `validate` が要求するファイル名と、`description` の指定が一致している
- [ ] `task/instruction.md` に評価基準が明記されている
- [ ] `[evaluator].class` が `"module:ClassName"` 形式で、案件配下なら `__init__.py` がある
- [ ] 秘匿情報を `task/` や `knowledge/` に置いていない (そのままプロンプトに載る)
- [ ] `--dry-run` でプロンプトを目視確認した
- [ ] `--rounds 1` が通ってから複数ラウンドに広げた

---

## 9. よくあるつまずき

| 症状 | 原因 / 対処 |
|---|---|
| ペルソナが何も出力しない | `description` に出力ファイル名の指定が無い。基盤はファイルを代わりに作らない |
| 全員 `validation_failed` | `validate` の要求と `description` / `instruction.md` の指示が食い違っている。`validation.json` の `issues` を読む |
| 全ペルソナの出力が似通う | `description` の差別化が弱い。禁止事項・排他条件を具体的に書く |
| 相対パスのデータが見つからない | Evaluator 内の相対パスは `project_dir` 起点で解決する。リポジトリルートから実行しているか確認 |
| `extra_forbidden` エラー | TOML のキー名の綴り違い。自由記述が許されるのは `[engine_options]` と `[evaluator]` のみ |
| 案件クラスが import できない | `sys.path` に入るのは `project_dir`。`evaluators/__init__.py` の有無と `"module:Class"` 形式を確認 |

その他の症状は [OPS.md §10](OPS.md) を参照。

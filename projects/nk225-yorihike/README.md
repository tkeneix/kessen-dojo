# nk225-yorihike

日経225先物の **寄り引け (寄付建て→引け決済)** 戦略を3つの異なるトレード思想のペルソナが3ラウンド競い合うサンプル案件。

## 設計

### 3つのペルソナ

| persona_id | 戦略思想 | engine |
|---|---|---|
| `rsi-contrarian` | ローレンス・A・コナーズの RSI逆張り (短期RSI / 超売られゾーンでロング) | `claude:sonnet-4-6` |
| `momentum-turtles` | リチャード・デニスのタートルズ手法 (ドンチャンチャネルブレイクアウト / トレンドフォロー) | `claude:sonnet-4-6` |
| `short-only` | 売り専門 (前日陽線の翌日など特徴的な日に寄り売り→引け買戻し) | `claude:sonnet-4-6` |

各ペルソナは `out/strategy.py` に `generate(rows) -> list[int]` を実装する。
案件特化 Persona クラスは作らず、標準 `ClaudePersona` を description で差別化する構成。

### 戦略インターフェイス

```python
def generate(rows: list[dict]) -> list[int]:
    """日経225先物の翌日寄り引けトレードシグナルを生成する。

    Args:
        rows: OHLCVデータ。日付昇順の list[dict]。
            各 dict のキー:
              "date":   str ("YYYY-MM-DD")
              "open" / "high" / "low" / "close" / "volume": float | None

    Returns:
        list[int]: rows と同じ長さ。各要素は -1, 0, +1 のいずれか。
            +1 → 翌日 rows[t+1] の寄りで買い、引けで決済 (ロング)
             0 → 翌日はノーポジション
            -1 → 翌日 rows[t+1] の寄りで売り、引けで買戻し (ショート)
        ※ signals[-1] は翌日データが無いため評価で使われない (長さは合わせる)。

    look-ahead bias 防止 (構造的):
      - signals[t] は rows[t+1] の寄り引けに適用される (1日シフトペアリング)。
      - そのため generate 内で rows[0..t] まで自由に参照してよい (rows[t]のOHLC含む)。
      - rows[t+1] 以降の参照は禁止 (未来情報先取り)。

    その他の制約:
      - 利用可能: 標準ライブラリ + pandas / numpy / scipy / scikit-learn (kessen-dojo の venv 同梱)。それ以外の外部依存は不可。
      - 外部I/O禁止 (rows のみから計算)。例外を投げない。
    """
```

### Evaluator (`evaluators/nk225_evaluator.py`)

`Nk225YorihikeEvaluator(BaseEvaluator)` — 案件固有実装。評価器自体は stdlib のみで書かれているが、ペルソナ側 strategy.py は kessen-dojo venv の pandas/numpy/scipy/scikit-learn も利用可。

**`validate()`** (軽量チェック、リトライループから呼ばれる)
- `out/strategy.py` 存在
- import 可能
- `generate` 関数が定義されている
- 先頭120日で試走 → 戻り値が `list[int]` 形式、長さ一致、要素が `{-1, 0, 1}`

**`score()`** (本評価)
1. `tools/data/nikkei_futures_2020_2026.csv` を読込 (全1541日 / 2020-01-06〜2026-04-30)
2. `signals = strategy.generate(rows)` を呼出
3. **1日シフトペアリング** で日次リターン: `signals[t] × (rows[t+1].close - rows[t+1].open) / rows[t+1].open` (look-ahead bias を構造的に防止)
4. ウォームアップ期 (先頭30日) を除外して **累積リターン列** を作成
5. 累積リターン列と順番列 `1, 2, 3, ..., N` の **Pearson 相関係数** = score
6. `n_active < min_active_days(=20)` の場合は score にペナルティ (validate でなく score で減点)

→ 相関が高い = 損益曲線が単調右肩上がり (ドローダウン少なく時間に対して線形に伸びる) ことを意味する。
合計リターンの大きさより **安定性** を評価する設計。

### ラウンド設計

3ラウンド。各ラウンド終了時に勝者の `out/` 一式と leaderboard が次ラウンドの `_seeds/round-N+1/previous_winner/` にコピーされ、各ペルソナの `in/` に展開される (kessen 標準フロー)。
失敗ペルソナは `knowledge/per-persona/{id}/lessons.jsonl` に自動記録され、次ラウンドのプロンプトに注入される。

## ディレクトリ構成

```
projects/nk225-yorihike/
├── README.md                           本ファイル
├── project.toml                        ラウンド数 / 参加者 / Evaluator 設定 / retry
├── personas/
│   ├── rsi-contrarian.toml
│   ├── momentum-turtles.toml
│   └── short-only.toml
├── evaluators/
│   ├── __init__.py
│   └── nk225_evaluator.py              Nk225YorihikeEvaluator
├── task/                               round-1 の各ペルソナの in/ にコピーされる初期seed
│   ├── instruction.md                  ストラテジー仕様 + 評価方法
│   └── sample_data.csv                 OHLCV 冒頭30日プレビュー
└── runs/{run-id}/                      実行時に自動生成
    ├── round-{1,2,3}/
    │   ├── {persona-id}/
    │   │   ├── in/                     task/ + 前ラウンド勝者seed + _retry_feedback.md
    │   │   ├── out/                    strategy.py / notes.md
    │   │   └── attempts/attempt-N/     リトライ毎のスナップショット
    │   └── leaderboard.json
    ├── _seeds/round-{2,3}/             次ラウンド初期seed
    └── final.json                      総合勝者
```

## 設定

### `project.toml`

```toml
[project]
name = "nk225-yorihike"
rounds = 3
max_parallel = 1                          # SDK subprocess並列のnoisy logを回避

[evaluator]
class = "evaluators.nk225_evaluator:Nk225YorihikeEvaluator"
data_path = "../../tools/data/nikkei_futures_2020_2026.csv"
warmup_days = 30                          # 累積計算からの除外日数
min_active_days = 20                      # active日数不足ペナルティ閾値

[participants]
ids = ["rsi-contrarian", "momentum-turtles", "short-only"]

[retry]
max_attempts = 2                          # validate失敗時に1回追加試行
on_exhaustion = "exclude"
abort_run_on_all_failed = false
```

### Evaluator config キー

| キー | デフォルト | 意味 |
|---|---|---|
| `data_path` | `"../../tools/data/nikkei_futures_2020_2026.csv"` | OHLCV CSV のパス (project_dir 相対 or 絶対) |
| `warmup_days` | `30` | 累積リターン計算から除外する先頭日数 |
| `min_active_days` | `20` | `signal != 0` の日数がこれを下回ると score にペナルティ |

## 実行

### 前提

| 項目 | 内容 |
|---|---|
| Python | 3.12+ |
| パッケージ管理 | `uv` (リポルートで `uv sync` 済み) |
| データファイル | `tools/data/nikkei_futures_2020_2026.csv` (リポ同梱) |
| 認証 | 下記いずれか |

**認証はいずれか1つ:**

```bash
# 方法A: APIキー (チーム/本番)
export ANTHROPIC_API_KEY=sk-ant-...

# 方法B: Claude Code CLI サブスク (個人開発)
claude /login                      # 1度ログインしておけば SDK が継承する
```

OpenAI / xAI のキーは **不要** (3ペルソナ全て `claude:sonnet-4-6`)。

### A. CLI 1発実行 (推奨、デバッグ向き)

```bash
# リポルートで実行
uv run kessen run-once --project projects/nk225-yorihike

# ラウンド数を上書き (例: まず1ラウンドで試す)
uv run kessen run-once --project projects/nk225-yorihike --rounds 1
```

実行ログは `logs/kessen.log` (JST + 7世代ローテ) に出力されます。

### B. REST API 経由 (常駐運用)

```bash
# 別ターミナルでサーバ起動
uv run kessen serve --port 8788

# ラウンド開始 (即時 202 + run-id 返却)
curl -X POST http://127.0.0.1:8788/projects/nk225-yorihike/rounds \
  -H 'Content-Type: application/json' -d '{}'
# → {"run_id":"2026-05-01T15-30-00", ...}

# 状態取得
curl http://127.0.0.1:8788/projects/nk225-yorihike/rounds/2026-05-01T15-30-00

# Leaderboard取得
curl http://127.0.0.1:8788/projects/nk225-yorihike/rounds/2026-05-01T15-30-00/leaderboard

# 中断したラウンドを再開
curl -X POST http://127.0.0.1:8788/projects/nk225-yorihike/rounds/2026-05-01T15-30-00/resume
```

### C. dry-run (LLM呼出なし、プロンプト合成だけ確認)

```bash
mkdir -p /tmp/nk225-dry/{in,out}
cp projects/nk225-yorihike/task/* /tmp/nk225-dry/in/
uv run kessen persona-run \
  --project projects/nk225-yorihike \
  --persona rsi-contrarian \
  --in-dir /tmp/nk225-dry/in --out-dir /tmp/nk225-dry/out \
  --dry-run
# → out/_dry_run_system_prompt.md / _dry_run_user_prompt.md が出力される
```

## 実行結果の見方

`projects/nk225-yorihike/runs/{run-id}/` 配下に成果物が出力される:

```
runs/2026-05-01T15-30-00/
├── state.json                          # ラウンド進捗 (running / completed / etc.)
├── final.json                          # 全ラウンド集計 + overall_winner
├── round-1/
│   ├── leaderboard.json                # 全participantのscore/status/attempts
│   ├── rsi-contrarian/
│   │   ├── in/                         # 与えた入力 (task/* + 前ラウンド勝者seed)
│   │   ├── out/strategy.py             # 生成された戦略
│   │   ├── out/notes.md                # (任意) 設計ノート
│   │   └── attempts/attempt-N/         # リトライ毎のスナップショット + validation.json
│   ├── momentum-turtles/...
│   └── short-only/...
├── round-2/...                         # round-1勝者のout/がseedされた状態で再戦
├── round-3/...
└── _seeds/round-{2,3}/previous_winner/ # 次ラウンドへの引継ぎ
```

確認の典型動線:

```bash
RUN_ID=$(ls -t projects/nk225-yorihike/runs | head -1)
RUN=projects/nk225-yorihike/runs/$RUN_ID

# 総合勝者
jq .overall_winner $RUN/final.json

# ラウンド毎のスコア
jq '.entries[] | {persona_id, score, status, note}' $RUN/round-1/leaderboard.json

# 勝者の戦略コードを確認
WINNER=$(jq -r .winner $RUN/round-3/leaderboard.json)
cat $RUN/round-3/$WINNER/out/strategy.py
```

## 実行コスト目安 (Sonnet 4.6 / 実測ベース)

> 注: 実測根拠は 2026-05-01 の rsi-contrarian 単発実行 (3:43)。
> Sonnet は Haiku の 4〜5倍の思考時間がかかり、加えてペルソナが Bash で自分のコードを `python -c "import strategy"` 自己検証する挙動を持つため、想定より長くなりやすい。

| 単位 | 所要時間 (目安) | 備考 |
|---|---|---|
| 1ペルソナ (`claude:sonnet-4-6`, 戦略生成タスク) | **3〜5 分** | 7〜10 turns 消費。strategy.py 書込 ≈ 3分 + 自己検証/notes.md 追記 ≈ 30〜60秒 |
| 1ラウンド (3ペルソナ逐次, `max_parallel=1`) | **10〜15 分** | リトライ無し前提 |
| フルラン 3ラウンド | **30〜45 分** | 全員リトライ無しの理想ケース。validate 失敗が出ると倍増もあり得る |
| ペルソナのトークン使用量 | 1試行 5K〜20K tokens | strategy.py の生成量 + Bash 出力で増減 |

**運用推奨**:

1. まず `--rounds 1` で動作確認 (約12分) → 1ラウンドの中身を確認
2. 問題なければ `--rounds 3` のフルラン (約40分)
3. ターミナル / `logs/persona.{id}.log` に `[turn N] AssistantMessage: tool=Read` のような進捗ログが秒単位で出るので「動いているか」は確認可能 (沈黙時は SDK の停滞 / 無限思考を疑う)

**速度を稼ぎたい場合の選択肢** (品質トレードオフあり):

- ペルソナの `[engine_options]` で `max_turns = 8` に絞る → 自己検証ループを早期終了 (ただし validate 失敗→リトライで逆に長くなる可能性)
- ペルソナの `[engine_options]` で `allowed_tools = ["Read", "Write", "Edit", "Glob", "Grep"]` (Bash 除外) → 自己実行による検証を構造的に止める (もっとも効果的)
- engine を `claude:haiku` に変更 → 4〜5倍速だが戦略の質は明確に下がる

## トラブルシュート

| 症状 | 原因 / 対処 |
|---|---|
| `nk225 data file not found: ...` | リポルートから実行していない。`cd kessen-dojo && uv run kessen run-once ...` |
| `MissingCredentialError` | `ANTHROPIC_API_KEY` 未設定かつ `claude /login` 未実施。どちらか実施 |
| `validation_failed` (3ペルソナ全員) | `runs/{run-id}/round-1/{persona}/attempts/attempt-N/validation.json` の `issues` を確認。`generate` 関数の戻り値型ずれ等が頻出 |
| SDK の noisy `exit code 1` ログ | 並列実行で出やすい。本案件は `max_parallel=1` 既定なので通常は出ない。出ても成果物が valid なら leaderboard は `ok` |
| 途中で Ctrl+C → run が `interrupted` 状態 | `POST /rounds/{run_id}/resume` で続行可能 (REST API モードのみ) |

## 評価指標の挙動 (検証済み)

ベンチマーク用の単純戦略で Evaluator の挙動を確認済み:

| 戦略 | n_active | total_return | corr スコア |
|---|---|---|---|
| 常時ロング (バイ&ホールド) | 1510/1510 | +32.07% | **0.745** |
| 直近5日リターン陰の翌日のみロング | 266/1510 | +20.42% | **0.883** |
| 常時ノーポジ | 0/1510 | 0% | 0.0 (ペナルティ後) |

(評価日数が 1510 = 1541 - 1(シフト) - 30(warmup) なのは、`signals[-1]` を捨てる + ウォームアップ除外のため。)

→ 「最終リターン」より「右肩上がりの安定性」を評価する設計が機能している
(リターン低めでも安定的に積み上がる戦略の方が高スコア)。

不正な strategy.py は validate でリジェクトされ、リトライ機構が `_retry_feedback.md` 経由でペルソナに修正提案を返す。

## データソース

`tools/data/nikkei_futures_2020_2026.csv` (kessen-dojo リポルート配下)

| カラム | 例 |
|---|---|
| `date` | `2020-01-06` |
| `open` / `high` / `low` / `close` | 23180 / 23320 / 23100 / 23100 |
| `volume` | (CSV内では0) |

**期間**: 2020-01-06 〜 2026-04-30 / 全1541日。

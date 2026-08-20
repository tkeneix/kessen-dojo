# kessen-dojo OPS

運用ガイド。環境構築・認証・実行・ログ・DB・デバッグ・トラブルシューティングを扱う。仕様とアーキテクチャは [PRD.md](PRD.md)、案件の新設は [NEW_PROJECT.md](NEW_PROJECT.md)。

---

## 1. 環境構築

### 1.1 前提

| 項目 | 要件 |
|---|---|
| Python | 3.12 以上 (`.python-version` は 3.12) |
| パッケージ管理 | `uv` のみ。`pip` 直叩き禁止 |
| OS | macOS / Linux (JST 前提のログ整形。タイムゾーンはコード側で固定) |
| Claude 認証 | APIキー または Claude Code CLI ログイン (§2) |
| 任意 | `jq` (結果確認に便利) |

### 1.2 セットアップ

```bash
cd <repo-root>

# 仮想環境 + 依存解決 (.venv が作られる)
uv sync

# 動作確認
uv run kessen --version        # kessen, version 0.1.0
uv run kessen info             # 認証モードの解決結果を表示
uv run kessen --help           # サブコマンド一覧
```

`uv run` は `.venv` を使う。シェルに別の `VIRTUAL_ENV` が設定されていると警告が出るが、`.venv` が優先されるので無視してよい (気になる場合は `deactivate` してから実行する)。

> 現時点で `tests/` は存在しない (PRD §11.2)。`uv run pytest` を実行しても収集対象が無い。

### 1.3 起動方法の選択

| 方法 | コマンド | 向いている場面 |
|---|---|---|
| CLI 単発 | `uv run kessen run-once --project projects/<name>` | 開発・デバッグ。ログが手元に流れる |
| REST API 常駐 | `uv run kessen serve` | 複数ランの管理、キャンセル / 再開が必要な運用 |
| ペルソナ単体 | `uv run kessen persona-run ...` | 1ペルソナのプロンプトと出力だけ確認したいとき |

---

## 2. 認証セットアップ

### 2.1 優先順位

Claude の認証は `kessen.auth.resolver` が次の順で解決する (PRD §8.3)。

1. `ANTHROPIC_API_KEY` が設定されている → **APIキー方式**
2. `claude` コマンドが PATH にある → **サブスクリプション方式** (Claude Code CLI のログイン状態を SDK が継承)
3. どちらも無い → 未認証。実行時に `MissingCredentialError`

現在どちらで解決されるかは `kessen info` で確認する。表示される APIキーは常にマスクされる。

```
[Claude]
  mode: subscription
  cli_path: /opt/homebrew/bin/claude
```

### 2.2 APIキー方式

チーム利用・CI・サーバ常駐に向く。

```bash
export ANTHROPIC_API_KEY="<your-api-key>"
uv run kessen info     # mode: api_key と表示されること
```

APIキーが設定されていると、CLI ログイン済みでも**APIキーが優先**される。サブスク経由に戻したい場合は `unset ANTHROPIC_API_KEY`。

### 2.3 サブスクリプション方式（個人開発）

```bash
claude /login          # 1度ブラウザ認証しておけば SDK 側が継承する
uv run kessen info     # mode: subscription と表示されること
```

Worker の `claude:` engine も同じ SDK 経路を通るため、この認証をそのまま使える (PRD §8.2)。

### 2.4 OpenAI / xAI / Google

| provider | 環境変数 | 用途 |
|---|---|---|
| OpenAI | `OPENAI_API_KEY` | `openai:` のペルソナ / Worker |
| xAI (Grok) | `XAI_API_KEY` | `xai:` のペルソナ / Worker |
| Google (Gemini) | `GOOGLE_API_KEY` (別名 `GEMINI_API_KEY`) | `google:` の Worker |

未設定の provider を使う案件を実行すると `MissingCredentialError` になる。収録サンプル案件はいずれも Claude のみで動くため、これらは不要。

### 2.5 認証情報の保管

- テンプレートは `.env.example`。コピーして値を埋める。

  ```bash
  cp .env.example .env
  # .env を編集してキーを設定
  source .env
  ```

- `.env` は `.gitignore` 済み。**実キーを含むファイルをコミットしない**。
- キーをコマンドライン引数やログに出さない。基盤側はログにキーを書かない実装だが、案件コードでも同じ規律を守る。
- 共有マシンで運用する場合、環境変数よりシェル起動時に読む秘匿ファイル (パーミッション 600) の方が事故が少ない。

---

## 3. 実行モード

### 3.1 CLI: `run-once`

```bash
# 案件を最後まで走らせる (project.toml の rounds に従う)
uv run kessen run-once --project projects/nk225-yorihike

# ラウンド数を上書きして短く試す
uv run kessen run-once --project projects/nk225-yorihike --rounds 1

# run-id を明示 (同じ id を再指定すると完了済ラウンドはスキップされる)
uv run kessen run-once --project projects/nk225-yorihike --run-id 2026-05-01T15-30-00
```

- `--project` には**案件ディレクトリへのパス**を渡す (プロジェクト名ではない)。
- 終了時に順位表と勝者成果物のパスがターミナルに整形出力される。
- 終了コード: `0` 正常 / `2` 実行時例外 / `3` `aborted` (全員失敗で打ち切り)。
- このモードは状態DBを使わない。結果は `projects/{name}/runs/{run-id}/` に残る。

### 3.2 REST API: `serve`

```bash
uv run kessen serve                                  # 127.0.0.1:8788
uv run kessen serve --port 9000 --db ./state/runs.db # ポートとDBを指定
```

| オプション | 既定 | 意味 |
|---|---|---|
| `--host` | `127.0.0.1` | バインドアドレス (§9 参照。安易に `0.0.0.0` にしない) |
| `--port` | `8788` | ポート |
| `--db` | `./runs.db` | SQLite ファイル (環境変数 `KESSEN_DB_PATH` でも指定可) |
| `--repo-root` | CWD 推定 | 案件探索の起点 (環境変数 `KESSEN_REPO_ROOT`) |
| `--reload` | off | 開発用オートリロード |

典型的な操作:

```bash
BASE=http://127.0.0.1:8788
PROJ=nk225-yorihike

# ヘルスチェック
curl -s $BASE/health

# ラン開始 (即 202 + run_id)
RUN_ID=$(curl -s -X POST $BASE/projects/$PROJ/rounds \
           -H 'Content-Type: application/json' -d '{"rounds": 1}' | jq -r .run_id)

# 進捗確認
curl -s $BASE/projects/$PROJ/rounds/$RUN_ID | jq '{status: .run.status, round: .run.current_round, active: .is_active}'

# 順位表
curl -s $BASE/projects/$PROJ/rounds/$RUN_ID/leaderboard | jq .

# 中断 / 再開
curl -s -X POST $BASE/projects/$PROJ/rounds/$RUN_ID/cancel
curl -s -X POST $BASE/projects/$PROJ/rounds/$RUN_ID/resume
```

`{project_name}` は `projects/` 配下の**ディレクトリ名**。OpenAPI は `$BASE/docs` で参照できる。

### 3.3 補助スクリプト

`run-once-rounds3.sh` はリポジトリ直下で `.env` を読み込み、収録案件を3ラウンド実行する薄いラッパ。運用に合わせて `scripts/` に自前のラッパを置いてもよい。

---

## 4. ナレッジ運用

### 4.1 階層と書込責任

| 層 | パス | 人間が書くもの | 基盤が自動で書くもの |
|---|---|---|---|
| shared | `shared/knowledge/` | 全案件共通の作法 | — |
| common | `projects/{name}/knowledge/common/` | 案件共通の作法・データ仕様 | `leaderboard_history.jsonl` (run 完了時) |
| private | `projects/{name}/knowledge/per-persona/{id}/` | ペルソナ個別のプレイブック | `lessons.jsonl` (ラウンド完了時) |

自動生成される `.jsonl` は**手で編集しない**。方針を変えたい場合は人間用の `.md` を追加する。

### 4.2 注入内容の確認

```bash
uv run kessen knowledge-show --project projects/sample --persona echo-claude
```

実際に system_prompt へ入る Markdown がそのまま出力される。「ナレッジを置いたのに効いていない」と感じたら、まずこれで確認する。同名 `.md` が強い層に存在すると弱い層は**完全に隠れる** (PRD §7.2)。

### 4.3 JSONL の肥大化対策

- retriever が注入するのは全 `.jsonl` を時系列マージした**最新20件**のみ。ファイルが大きくなっても system_prompt は膨らまないが、読み込みコストは増える。
- 定期メンテの目安:

  ```bash
  # 行数の確認
  wc -l projects/*/knowledge/**/*.jsonl

  # 古い行のアーカイブ (直近500行だけ残す例)
  f=projects/<name>/knowledge/common/leaderboard_history.jsonl
  tail -500 "$f" > "$f.tmp" && mv "$f.tmp" "$f"
  ```

- 破損行 (不正 JSON) は読み出し時に黙ってスキップされる。手で編集した後は `knowledge-show` で読めることを確認する。

### 4.4 ナレッジの棚卸し

ラウンドを重ねると `lessons.jsonl` に失敗記録が溜まり、ペルソナが過去の失敗に引きずられることがある。案件の方向性を変えたときは、対象ペルソナの `lessons.jsonl` を退避 (リネーム) して再スタートするのが手早い。

---

## 5. ログ運用

### 5.1 出力先

`kessen.logging_config.setup_logger(name)` がロガー名ごとにファイルを作る。出力先は**実行時のカレントディレクトリ配下の `logs/`** (`.gitignore` 済み)。

| ロガー名 | ファイル | 内容 |
|---|---|---|
| `kessen` | `logs/kessen.log` | CLI / オーケストレータ / 評価器 |
| `kessen.api` | `logs/kessen.api.log` | API サーバとバックグラウンドラン |
| `kessen.tools` | `logs/kessen.tools.log` | MCP ツール呼び出し |
| `persona.{id}` | `logs/persona.{id}.log` | ペルソナの進捗 (`[turn N] ...`) |
| `persona.{id}.path_guard` | `logs/persona.{id}.path_guard.log` | アクセス拒否の記録 |
| `worker.{persona}.{worker}` | `logs/worker.{persona}.{worker}.log` | Worker の呼び出しと finish_reason 警告 |

同じ内容が標準出力にも流れる (`to_stdout=True`)。

### 5.2 フォーマットとローテーション

- フォーマット: `2026-05-06T21:30:00+09:00 [INFO] name:file.py:123 - message`
- タイムスタンプは **JST の ISO 8601 (秒精度)**
- ローテーション: 1ファイル 10MB、**7世代** (`{name}.log.1` … `.7`)
- 例外時は `logger.exception` により完全なスタックトレースを出す (CLAUDE.md の親ルール準拠)
- **機密情報をログに出さない**のは呼び出し側の責務。案件コードでも守る

### 5.3 ログレベル

既定は `INFO`。より詳しく見たい場合は、実行スクリプト側でロガーを取得して個別に引き上げる。

```python
import logging
logging.getLogger("persona.rsi-contrarian").setLevel(logging.DEBUG)
```

`DEBUG` にすると claude CLI サブプロセスの stderr が `[claude-cli stderr]` として毎行流れる (エラー時は `INFO` でも末尾30行が ERROR で出る)。

---

## 6. 状態DB (`runs.db`)

REST API モードでのみ使う SQLite。CLI の `run-once` は DB を使わない。

### 6.1 スキーマ

```sql
CREATE TABLE runs (
    run_id          TEXT PRIMARY KEY,
    project         TEXT NOT NULL,
    status          TEXT NOT NULL,   -- pending|running|completed|failed|cancelled|aborted|interrupted
    current_round   INTEGER NOT NULL DEFAULT 0,
    total_rounds    INTEGER NOT NULL,
    started_at      TEXT NOT NULL,   -- ISO8601 JST
    finished_at     TEXT,
    overall_winner  TEXT,
    project_dir     TEXT NOT NULL,
    error           TEXT
);
CREATE INDEX idx_runs_project_status ON runs(project, status);
CREATE INDEX idx_runs_started_at     ON runs(started_at DESC);

CREATE TABLE round_scores (
    run_id      TEXT NOT NULL,
    round       INTEGER NOT NULL,
    persona_id  TEXT NOT NULL,
    score       REAL,                -- NULL は勝者選定から除外
    status      TEXT NOT NULL,       -- ok|validation_failed|runtime_failed|cancelled
    attempts    INTEGER NOT NULL DEFAULT 1,
    note        TEXT,
    PRIMARY KEY (run_id, round, persona_id),
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);
CREATE INDEX idx_round_scores_run ON round_scores(run_id, round);
```

接続時に `journal_mode=WAL` と `foreign_keys=ON` を有効化し、`isolation_level=None` (autocommit) で開く。

### 6.2 運用上の性質

- サーバ起動時に `cleanup_stale_running()` が走り、前回プロセスの残骸 (`running` / `pending`) を `interrupted` に落とす。これらは `resume` の対象になる。
- 結果の**正はファイル側** (`runs/{run-id}/final.json` と各 `leaderboard.json`)。DB は一覧性とキャンセル対象の特定のためのインデックスと考える。DB を消しても成果物は失われない。
- WAL のため `runs.db-wal` / `runs.db-shm` が併存する。バックアップは3ファイルまとめて、またはサーバ停止後に取る。

### 6.3 障害時の手順

```bash
# 整合性チェック
sqlite3 runs.db "PRAGMA integrity_check;"

# 状態の確認
sqlite3 runs.db "SELECT run_id, project, status, current_round, total_rounds FROM runs ORDER BY started_at DESC LIMIT 20;"

# 破損時: DB を作り直す (成果物ファイルは残る)
mv runs.db runs.db.broken && mv runs.db-wal runs.db-wal.broken 2>/dev/null
uv run kessen serve   # 起動時にスキーマが再作成される
```

DB を作り直すと過去ランの一覧は失われるが、`projects/{name}/runs/` 配下の成果物と `final.json` は無傷。

---

## 7. デバッグ Tips

### 7.1 ペルソナの単体実行

```bash
mkdir -p /tmp/kessen-dbg/in /tmp/kessen-dbg/out
cp projects/sample/task/* /tmp/kessen-dbg/in/

uv run kessen persona-run \
  --project projects/sample --persona echo-claude \
  --in-dir /tmp/kessen-dbg/in --out-dir /tmp/kessen-dbg/out
```

### 7.2 プロンプトだけ確認する (LLM 呼出なし・課金なし)

```bash
uv run kessen persona-run \
  --project projects/sample --persona echo-claude \
  --in-dir /tmp/kessen-dbg/in --out-dir /tmp/kessen-dbg/out --dry-run

# out/_dry_run_system_prompt.md  … ナレッジ注入込みの system_prompt
# out/_dry_run_user_prompt.md    … 入力ファイル一覧を含む user_prompt
```

ペルソナの `description` を書き換えたとき、意図通りに `# Role` へ入っているかをここで確認するのが最短。

### 7.3 リトライの中身を追う

```bash
RUN=projects/<name>/runs/<run-id>

# なぜ validate に落ちたか
cat $RUN/round-1/<persona-id>/attempts/attempt-1/validation.json

# 何回試行して最終的にどうなったか
cat $RUN/round-1/<persona-id>/retry_summary.json

# ペルソナに返した修正指示
cat $RUN/round-1/<persona-id>/in/_retry_feedback.md
```

### 7.4 結果の読み方

```bash
RUN_ID=$(ls -t projects/<name>/runs | head -1)
RUN=projects/<name>/runs/$RUN_ID

cat $RUN/summary.md                                    # 人間可読サマリ
jq '{winner: .overall_winner, round: .overall_winner_round, score: .overall_winner_score}' $RUN/final.json
jq '.entries[] | {persona_id, score, status, attempts, note}' $RUN/round-1/leaderboard.json
jq -r '.winner_artifacts[]' $RUN/final.json            # 勝者の成果物パス
```

### 7.5 ナレッジ注入の確認

§4.2 の `knowledge-show` を使う。ペルソナごとに結果が変わる (private 層が異なるため)。

### 7.6 アクセス拒否の確認

ペルソナが `docs/` や `shared/datawarehouse/` を読もうとすると PreToolUse hook が拒否し、`logs/persona.{id}.path_guard.log` に WARNING が残る。ペルソナが同じ場所を何度も試している場合は、`task/instruction.md` の書き方がその参照を促してしまっている可能性が高い。

---

## 8. パフォーマンスとコスト

### 8.1 並列度

`project.toml` の `[project].max_parallel` がラウンド内の同時実行数。

| 値 | 効果 | 注意 |
|---|---|---|
| `1` | 逐次実行。ログが読みやすく、SDK サブプロセスのノイズが出にくい | ラウンド時間はペルソナ数に比例 |
| `2`〜`N` | ラウンド時間を短縮 | claude CLI のサブプロセスが同数立つ。ノイズログやレート制限に注意 |

まず `max_parallel = 1` で挙動を確認し、安定してから上げる。

### 8.2 コストと所要時間の見積り

実行時間はモデルとタスクで大きく変わる。目安を掴む手順:

1. `--rounds 1` で1ラウンドだけ実行し、実測する
2. 1ラウンドの所要 × ラウンド数 + リトライ余裕 (1.5〜2倍) を見込む
3. `logs/persona.{id}.log` の `[turn N]` の刻みで、止まっているのか考え続けているのかを判断する

削減の効きどころ:

| 手段 | 効果 | トレードオフ |
|---|---|---|
| `engine_options.max_turns` を下げる | ターン数の上限で頭打ちにする | 早期打ち切りで validate 失敗 → リトライで逆に伸びることがある |
| `allowed_tools` から `Bash` を外す | 自己検証ループを構造的に止める。効果が大きい | ペルソナが自分のコードを試せなくなり品質が落ちる場合がある |
| 軽いモデルに変える (`claude:haiku` 等) | 数倍速くなる | 生成物の質は明確に落ちる |
| `max_chars_per_file` を下げる (非Claude系) | 入力トークン削減 | 入力の切り捨てで文脈を失う |

### 8.3 コンテキスト圧迫の回避

- ナレッジ JSONL は最新20件しか入らないが、`.md` は**全文**が入る。プレイブックが肥大化すると毎回のプロンプトを圧迫する。
- `in/` に置くファイルが多いほど user_prompt が長くなる (非Claude系は本文まで inline される)。前ラウンドの成果物をすべて配るのではなく、`previous_winner/` に絞る現在の設計を踏襲する。

---

## 9. セキュリティ運用

1. **API サーバを外部公開しない。** 認証機構は実装されていない。既定の `127.0.0.1` バインドを維持し、リモートから使う場合は SSH ポートフォワードか、認証付きリバースプロキシの背後に置く。`--host 0.0.0.0` はローカルネットワークが信頼できる場合に限る。
2. **ペルソナはホストでコードを実行しうる。** 既定の `allowed_tools` には `Bash` が含まれ、案件 Evaluator は生成された Python をホストの venv に import する (PRD §10)。信頼できない入力・プロンプトを投入しない。第三者に実行させる用途では `allowed_tools` から `Bash` を外す、コンテナ内で kessen ごと動かす、といった追加の隔離が必要。
3. **path guard を無効化しない。** `engine_options.disable_path_guard = true` はテスト専用。常用の案件設定には入れない。
4. **秘匿情報を案件ディレクトリに置かない。** `task/` や `knowledge/` の中身はそのままプロンプトに載る。
5. **成果物の外部共有前に中身を確認する。** `final.json` / `summary.md` にはホストの絶対パスが含まれる。ログにもディレクトリ構造が残る。
6. **`.env` と鍵をコミットしない。** `.gitignore` で除外済みだが、`git add -f` などで意図せず入らないよう注意する。

---

## 10. トラブルシューティング

| 症状 | 確認 / 対処 |
|---|---|
| `MissingCredentialError` | `kessen info` で解決結果を確認。`ANTHROPIC_API_KEY` を設定するか `claude /login` |
| `persona '<id>' not found in ...` | `personas/{id}.toml` の有無と、TOML 内 `[persona].id` がファイル名と一致しているか |
| `worker '<id>' not found in ...` | 同上 (`workers/{id}.toml`)。`shared/workers/` へのフォールバックも確認 |
| pydantic の `extra_forbidden` | TOML のキー名の綴り違い。自由記述が許されるのは `[engine_options]` と `[evaluator]` のみ |
| `... is not a BaseEvaluator subclass` | `class` の指し先が基底クラスを継承していない。`"module:ClassName"` 形式かも確認 |
| 案件固有クラスが `ModuleNotFoundError` | 案件配下のパッケージに `__init__.py` があるか。`--project` に正しい案件ディレクトリを渡しているか |
| データファイルが見つからない | 相対パスは `project_dir` 起点で解決される。リポジトリルートから実行しているかを確認 |
| 全ペルソナが `validation_failed` | `attempts/attempt-N/validation.json` の `issues` を読む。`task/instruction.md` の要求と Evaluator の `validate` がずれている典型 |
| SDK の `exit code 1` ノイズ | 成果物が valid なら leaderboard は `ok` になる。並列度を下げると出にくい (PRD §14.1) |
| 応答が途中で切れる | `finish_reason` の WARNING を確認し、`max_output_tokens` / `max_completion_tokens` を上げる |
| Ctrl+C 後に途中結果を見たい | `runs/{run-id}/final.json` が `status="cancelled"` で書かれている。部分 leaderboard も残る |
| 中断したランを続けたい | REST API の `resume`。CLI の場合は同じ `--run-id` で `run-once` を再実行する |
| ナレッジが効かない | `knowledge-show` で確認。同名 `.md` の上書き (PRD §7.2) が最頻の原因 |
| ログが出ない / 場所が分からない | `logs/` は**実行時のカレントディレクトリ**配下に作られる。リポジトリルートから実行する |

---

## 11. 参照

- 仕様・アーキテクチャ: [PRD.md](PRD.md)
- 新規案件の追加手順: [NEW_PROJECT.md](NEW_PROJECT.md)
- サンプル案件: [projects/nk225-yorihike/README.md](../projects/nk225-yorihike/README.md)
- 開発ガイドライン: リポジトリ直下の `CLAUDE.md`

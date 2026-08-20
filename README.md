# kessen-dojo

決戦道場 — 複数ペルソナのAIエージェントが評価指標で競い合い、ラウンド勝ち抜き方式で最良案を選び取る、常駐型エージェントフレームワーク。

同じ課題に思想の異なるペルソナを並列で当て、評価器で validate → retry → score を回し、勝者の成果物を次ラウンドの起点として配り直す。結果とナレッジはファイルに残り、ラウンドと run をまたいで持ち越される。

## ドキュメント

- [docs/PRD.md](docs/PRD.md) — 仕様 / アーキテクチャ / 設定ファイル / 意思決定履歴 / 既知の問題
- [docs/OPS.md](docs/OPS.md) — 運用ガイド / 認証 / ログ / 状態DB / デバッグ / トラブルシュート
- [docs/NEW_PROJECT.md](docs/NEW_PROJECT.md) — 新規案件の追加手順
- [projects/nk225-yorihike/README.md](projects/nk225-yorihike/README.md) — 収録サンプル案件の詳細

## クイックスタート

```bash
# 環境構築 (uv のみ。pip 直叩き禁止)
uv sync

# 動作確認
uv run kessen --version
uv run kessen info          # 認証モードの解決結果を表示

# プロンプト合成だけ確認 (LLM 呼出なし)
mkdir -p /tmp/kessen-dbg/in /tmp/kessen-dbg/out
cp projects/sample/task/* /tmp/kessen-dbg/in/
uv run kessen persona-run --project projects/sample --persona echo-claude \
  --in-dir /tmp/kessen-dbg/in --out-dir /tmp/kessen-dbg/out --dry-run

# コンペを1ラウンド実行 (LLM を呼ぶ)
uv run kessen run-once --project projects/sample --rounds 1
```

結果は `projects/{name}/runs/{run-id}/` に出る (`summary.md` / `final.json` / 各ラウンドの `leaderboard.json`)。

## CLI

| コマンド | 用途 |
|---|---|
| `kessen info` | インストール状態と認証モードの表示 |
| `kessen run-once --project <dir>` | 1案件のコンペを最終結果まで実行 |
| `kessen persona-run --project <dir> --persona <id>` | ペルソナの単発実行 (`--dry-run` でプロンプトのみ) |
| `kessen serve` | REST API サーバを常駐起動 (既定 `127.0.0.1:8788`) |
| `kessen knowledge-show --project <dir> --persona <id>` | ペルソナに注入されるマージ済みナレッジの表示 |

## 認証

```bash
# A: APIキー方式（外部公開・チーム利用）
export ANTHROPIC_API_KEY="<your-api-key>"

# B: サブスクリプション方式（個人開発）
claude /login   # Claude Code CLI 経由
```

`kessen info` で現在の認証モードを確認できる。詳細は [docs/OPS.md §2](docs/OPS.md)。

## 構成

- `src/kessen/` — 基盤コード (案件非依存)
- `shared/` — 案件横断のナレッジ・ペルソナ
- `projects/{name}/` — 案件単位の独立空間 (拡張ポイント: `personas/` `evaluators/` `workers/` `persona_classes/`)
- `docs/` — 仕様 / 運用ドキュメント

## 注意

REST API には認証機構が無く、ペルソナは既定でホスト上のコード実行 (`Bash`) を伴う。運用上の前提は [docs/OPS.md §9](docs/OPS.md) と [docs/PRD.md §10](docs/PRD.md) を参照。

## ステータス

P0〜P5 完了 (ペルソナ実行 / ラウンド + リトライ / マルチプロバイダ / REST API + 状態DB / ナレッジ循環 + Worker 層)。
未実装・改善候補は [docs/PRD.md §11.2](docs/PRD.md) を参照。自動テスト (`tests/`) は未整備。

## ライセンス

Apache-2.0 (`pyproject.toml` で宣言。LICENSE ファイルは未配置)

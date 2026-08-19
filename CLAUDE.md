# kessen-dojo

決戦道場 — 複数ペルソナのAIエージェントが評価指標で競い合い、ラウンド勝ち抜き方式で最良案を選び取る常駐型基盤。Claude Agent SDK + uv ベース。

詳細: 仕様 `docs/PRD.md` / 運用 `docs/OPS.md`

## メンタルモデル

- `src/kessen/` — 基盤コード（汎用、案件非依存）
- `shared/` — 案件横断のナレッジ・ペルソナ
- `projects/{name}/` — 案件単位の独立空間（拡張ポイント: persona_classes/, workers/, evaluators/, containers/）
- `docs/` — 仕様/運用ドキュメント

## 開発時のルール

- パッケージ管理は `uv` のみ。pip 直叩き禁止
- ログは `kessen.logging_config.setup_logger()` 経由（JST + 7世代ローテ）
- 認証は `kessen.auth.resolver` 経由（環境変数を直接 `os.environ` から読まない）
- 機密情報をログ出力しない
- ペルソナのSDK呼出では必ず `setting_sources=[]` を指定し `.claude/` 自動ロードを抑止
- 例外時は完全なスタックトレース + コンテキスト情報を出力（CLAUDE.md 親ルール準拠）

## 拡張ポイント

新しい案件を追加する場合は `projects/{name}/` 配下を整備（PRD §3 参照）。基盤側 `src/kessen/` には案件特化コードを書かない。

## 進捗

P0〜P5 完了。P6 (基盤コンテナ + DockerRunner) 以降は `docs/PRD.md §11` 参照。

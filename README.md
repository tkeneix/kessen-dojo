# kessen-dojo

決戦道場 — 複数ペルソナのAIエージェントが評価指標で競い合い、ラウンド勝ち抜き方式で最良案を選び取る、常駐型エージェントフレームワーク。

## ドキュメント

- [docs/PRD.md](docs/PRD.md) — 仕様 / アーキテクチャ / 意思決定履歴 / 過去問題と対策
- [docs/OPS.md](docs/OPS.md) — 運用ガイド / 認証 / ナレッジ / Docker / Tips

## クイックスタート

```bash
# 環境構築
uv sync

# 動作確認
uv run kessen --version
uv run kessen info

# テスト
uv run pytest
```

## 認証

```bash
# A: APIキー方式（外部公開・チーム利用）
export ANTHROPIC_API_KEY=sk-ant-xxxxx

# B: サブスクリプション方式（個人開発）
claude /login   # Claude Code CLI 経由
```

`kessen info` で現在の認証モードを確認できる。詳細は `docs/OPS.md §2`。

## ステータス

P0 完了（基盤土台）。P1〜P9 の実装計画は [docs/PRD.md §11](docs/PRD.md) を参照。

## ライセンス

未定（Apache-2.0 想定）

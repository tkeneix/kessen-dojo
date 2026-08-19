# 日経225先物 寄り引けトレード戦略コンペ

`out/strategy.py` に **`generate(rows)` 関数** を実装し、寄り引け (寄付で建てて引けで決済) のシグナルを返してください。

## 戦略インターフェイス

```python
def generate(rows: list[dict]) -> list[int]:
    """日経225先物の翌日寄り引けトレードシグナルを生成する。

    Args:
        rows: OHLCVデータ。日付昇順で並ぶ list[dict]。
            各 dict のキー:
              - "date":   str   ("YYYY-MM-DD" 形式、例 "2020-01-06")
              - "open":   float | None
              - "high":   float | None
              - "low":    float | None
              - "close":  float | None
              - "volume": float | None

    Returns:
        list[int]: rows と同じ長さ。各要素は次のいずれか。
              +1 → 翌日 rows[t+1] の寄りで買い、引けで決済 (ロング)
               0 → 翌日はノーポジション
              -1 → 翌日 rows[t+1] の寄りで売り、引けで買戻し (ショート)

        ※ signals[-1] は翌日データが無いため評価で使われない (戻り値の長さは合わせること)。
```

## 重要: シグナルの時点解釈 (look-ahead bias 構造的防止)

**`signals[t]` は `rows[t+1]` の寄り引けに対して適用される。** 評価は次の式で行う:

```
return[t] = signals[t] × (rows[t+1].close - rows[t+1].open) / rows[t+1].open
```

→ つまり `signals[t]` は **「rows[t] のクローズ時点までの情報をもとに、翌営業日に取るポジションを決める」** という意味になる。

そのため:

- **`generate` 内で `rows[0..t]` まで自由に参照してよい** (rows[t] のopen/high/low/closeも当然OK)。
- ただし `rows[t+1]` 以降を参照することは禁止 (未来情報先取り)。リスト index 以上にアクセスしないように。
- このフレーミングにより、generate内でrows[t]を見てしまっても look-ahead bias にならない (時間的にシフトされる)。

## 評価方法 (Evaluator がやること)

1. 各ペルソナの `out/strategy.py` を import → `generate(rows)` 呼出
2. 1日シフトペアリングで日次リターンを計算: `signals[t] × (rows[t+1].close - rows[t+1].open) / rows[t+1].open`
3. ウォームアップ期 (先頭30日) を除外して **累積リターン列** を作成
4. 累積リターン列と順番列 `1, 2, 3, ..., N` の **Pearson 相関係数** を計算
5. 相関が **高い** ほど高スコア (= 損益曲線が単調に右肩上がりに積み上がっている)

→ 相関 = 安定性指標。「最終リターンの大きさ」ではなく「ドローダウンが少なく時間に対して線形に伸びる」ほど高評価。

## 制約

- 利用可能ライブラリ: **Python 標準ライブラリ + pandas / numpy / scipy / scikit-learn** (kessen-dojo の venv に同梱)
- それ以外の外部依存 (yfinance / requests / 自前ダウンロード等) は **使えない**
- 外部ファイル / ネットワークアクセス禁止 (`generate` 内で `open()` / `urllib` 等の I/O 禁止、引数 rows のみから計算)
- `generate` 内で例外を出さない (None / 欠損 / ゼロ除算に対する防御を入れる)
- 戻り値の int 値は厳密に -1, 0, +1 のいずれか
- 戻り値の長さは `len(rows)` と一致させる (末尾の `signals[-1]` は使われないが、長さは合わせる)

## 推奨アウトプット

- `out/strategy.py` (**必須**) — `generate` 関数を含む戦略コード
- `out/notes.md` (任意) — 採用したシグナルロジック / 想定される強み弱みの説明

## データプレビュー

`in/sample_data.csv` に冒頭30日分が入っています。本評価ではこれと同形式の **2020-01-06 以降** の全データ (1500行強) で `generate` を呼び出します。

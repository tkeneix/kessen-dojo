"""nk225-yorihike 案件の Evaluator。

各ペルソナの out/strategy.py に定義された `generate(rows) -> list[int]` を呼び出し、
日経225先物の寄り引けリターン (signal × (close - open) / open) の累積に対し、
順番列 (1, 2, ..., N) との Pearson 相関係数をスコアとする。
相関が高いほど損益曲線が単調右肩上がりに安定していると見なす。

設計上の理由:
- 評価器自体は stdlib のみで実装 (案件依存を最小化、可読性優先)
- ペルソナ側 strategy.py は kessen-dojo venv の pandas/numpy/scipy/scikit-learn も利用可
"""

from __future__ import annotations

import csv
import importlib.util
import math
import statistics
import sys
import uuid
from pathlib import Path
from types import ModuleType

from kessen.evaluator.base import BaseEvaluator, ScoreResult, ValidationResult


class Nk225YorihikeEvaluator(BaseEvaluator):
    """日経225先物 寄り引けトレード戦略の累積リターン安定性評価。

    config:
      data_path: str           # OHLCV CSV のパス (project_dir 相対 or 絶対)
      warmup_days: int = 30    # 累積計算から除外する先頭日数 (相関の不安定さ回避)
      min_active_days: int = 20  # validate閾値: signal != 0 の日が最低限必要な数
    """

    async def validate(self, persona_out_dir: Path) -> ValidationResult:
        issues: list[str] = []
        suggestions: list[str] = []

        strategy_path = persona_out_dir / "strategy.py"
        if not strategy_path.exists():
            return ValidationResult(
                ok=False,
                issues=["out/strategy.py が存在しない"],
                suggestions=[
                    "out/ 直下に strategy.py を作成し、`def generate(rows) -> list[int]` を実装してください",
                ],
            )

        try:
            mod = self._import_strategy(strategy_path)
        except Exception as e:
            return ValidationResult(
                ok=False,
                issues=[f"strategy.py の import に失敗: {type(e).__name__}: {e}"],
                suggestions=[
                    "標準ライブラリ + pandas/numpy/scipy/sklearn は利用可。それ以外の外部依存 (yfinance 等) は使えません",
                    "import 文や構文エラーを見直してください",
                ],
            )

        if not hasattr(mod, "generate") or not callable(mod.generate):
            return ValidationResult(
                ok=False,
                issues=["generate 関数が定義されていない"],
                suggestions=["`def generate(rows: list[dict]) -> list[int]:` を実装してください"],
            )

        # 実データの先頭120日で試走 → 形式チェック
        rows = self._load_rows()
        sample_rows = rows[: min(120, len(rows))]
        try:
            signals = mod.generate(sample_rows)
        except Exception as e:
            return ValidationResult(
                ok=False,
                issues=[f"generate(rows) の試走で例外: {type(e).__name__}: {e}"],
                suggestions=[
                    "generate は外部 I/O (ファイル/ネット) にアクセスせず、引数 rows のみから計算してください",
                    "None / 欠損 / ゼロ除算に対する防御を入れてください",
                ],
            )

        if not isinstance(signals, list):
            issues.append(f"generate の戻り値が list でない: {type(signals).__name__}")
            suggestions.append("戻り値は list[int] (要素は -1, 0, +1) としてください")
            return ValidationResult(ok=False, issues=issues, suggestions=suggestions)

        if len(signals) != len(sample_rows):
            issues.append(f"signals の長さ {len(signals)} が rows {len(sample_rows)} と不一致")
            suggestions.append("引数 rows と同じ長さの list を返してください")

        for i, s in enumerate(signals[:30]):
            if s not in (-1, 0, 1):
                issues.append(f"signals[{i}] = {s!r} は許容値 {{-1, 0, 1}} 以外")
                suggestions.append("各要素は厳密に -1, 0, +1 の int を返してください")
                break

        return ValidationResult(ok=not issues, issues=issues, suggestions=suggestions)

    async def score(self, persona_out_dir: Path) -> ScoreResult:
        warmup = int(self.config.get("warmup_days", 30))
        min_active = int(self.config.get("min_active_days", 20))

        rows = self._load_rows()
        mod = self._import_strategy(persona_out_dir / "strategy.py")
        signals = mod.generate(rows)

        # 1日シフトペアリング: signals[t] は rows[t+1] の寄り引けに適用される。
        # → generate 内で rows[0..t] まで参照しても look-ahead bias にならない (構造的防止)。
        # signals[-1] は翌日データが無いため評価で使われない。
        bar_returns: list[float] = []
        for r_next, s in zip(rows[1:], signals[:-1]):
            o = r_next["open"]
            c = r_next["close"]
            if o is None or c is None or o == 0:
                bar_returns.append(0.0)
                continue
            bar_returns.append(int(s) * ((c - o) / o))

        active = bar_returns[warmup:]
        active_signals = signals[warmup:-1] if len(signals) > 0 else []
        if not active:
            return ScoreResult(
                score=-1.0,
                note=f"warmup({warmup}) 後にデータが残っていない (rows={len(rows)})",
                metrics={"n_days": 0},
            )

        cum: list[float] = []
        running = 0.0
        for x in active:
            running += x
            cum.append(running)

        x_axis = list(range(1, len(cum) + 1))
        corr = self._pearson(x_axis, cum)

        n_active = sum(1 for s in active_signals if s != 0)
        n_long = sum(1 for s in active_signals if s == 1)
        n_short = sum(1 for s in active_signals if s == -1)
        total_return = cum[-1] if cum else 0.0

        # ポジションが取られなさすぎる戦略は意味が薄い → スコアを減点
        # (validate ではなく score で減点する: 評価不可ではなく「弱い」だけ)
        if n_active < min_active:
            corr = corr * (n_active / max(min_active, 1))
            note_extra = f" [n_active<{min_active} -> penalty applied]"
        else:
            note_extra = ""

        note = (
            f"corr(cum,t)={corr:.4f}, total_return={total_return * 100:.2f}%, "
            f"active={n_active}/{len(active)} (long={n_long}, short={n_short}){note_extra}"
        )

        return ScoreResult(
            score=round(corr, 6),
            note=note,
            metrics={
                "correlation": round(corr, 6),
                "total_return": round(total_return, 6),
                "n_days": len(active),
                "n_active": n_active,
                "n_long": n_long,
                "n_short": n_short,
                "warmup": warmup,
            },
        )

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------

    def _resolve_data_path(self) -> Path:
        raw = self.config.get("data_path", "../../tools/data/nikkei_futures_2020_2026.csv")
        p = Path(raw)
        if not p.is_absolute():
            p = (self.project_dir / p).resolve()
        if not p.exists():
            raise FileNotFoundError(f"nk225 data file not found: {p}")
        return p

    def _load_rows(self) -> list[dict]:
        path = self._resolve_data_path()
        rows: list[dict] = []
        with path.open(encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for r in reader:
                rows.append(
                    {
                        "date": r.get("date", ""),
                        "open": _parse_float(r.get("open")),
                        "high": _parse_float(r.get("high")),
                        "low": _parse_float(r.get("low")),
                        "close": _parse_float(r.get("close")),
                        "volume": _parse_float(r.get("volume")),
                    }
                )
        return rows

    def _import_strategy(self, path: Path) -> ModuleType:
        # ペルソナ間 / リトライ間で sys.modules が衝突しないよう毎回ユニーク名で読む
        mod_name = f"_nk225_strategy_{uuid.uuid4().hex}"
        spec = importlib.util.spec_from_file_location(mod_name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"could not load spec for {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(mod_name, None)
            raise
        return module

    @staticmethod
    def _pearson(x: list[float], y: list[float]) -> float:
        if len(x) < 2 or len(x) != len(y):
            return 0.0
        mx = statistics.fmean(x)
        my = statistics.fmean(y)
        num = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y))
        sx = math.sqrt(sum((xi - mx) ** 2 for xi in x))
        sy = math.sqrt(sum((yi - my) ** 2 for yi in y))
        if sx == 0 or sy == 0:
            return 0.0
        return num / (sx * sy)


def _parse_float(s: object) -> float | None:
    if s is None or s == "":
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None

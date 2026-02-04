from __future__ import annotations

import pandas as pd


def _frac_to_decimal(frac: str) -> float | None:
    """Convert fractional odds like '7/2' to decimal like 4.5 (includes stake)."""
    if not frac or "/" not in frac:
        return None
    try:
        a, b = frac.strip().split("/")
        a = float(a)
        b = float(b)
        if b == 0:
            return None
        return 1.0 + (a / b)
    except Exception:
        return None


def build_runner_scores(runners: pd.DataFrame) -> pd.DataFrame:
    """Lightweight scoring. You can tighten this later; the pipeline is the key."""
    df = runners.copy()

    # market prob from odds
    df["best_price_dec"] = df["best_price_dec"].astype(float)
   df["market_prob_raw"] = 1.0 / df["best_price_dec"]

# Normalize market prob within each race to remove bookmaker overround
df["market_prob"] = df.groupby("race_id")["market_prob_raw"].transform(
    lambda s: s / max(1e-9, s.sum())
)

df["value_edge"] = df["model_prob"] - df["market_prob"]
    df["confidence"] = (df["score_raw"] * 100).round(1)

    # tidy ordering
    cols_first = [
        "date", "course", "off_time", "race_name", "distance", "going", "class_band",
        "runner", "draw", "weight", "age", "sex", "trainer", "jockey",
        "best_price_frac", "best_price_dec", "market_prob", "model_prob", "value_edge", "confidence"
    ]
    cols = [c for c in cols_first if c in df.columns] + [c for c in df.columns if c not in cols_first]
    return df[cols].sort_values(["date", "course", "off_time", "value_edge"], ascending=[True, True, True, False])


def build_value_bets(scored: pd.DataFrame, min_edge: float) -> pd.DataFrame:
    df = scored.copy()
    df = df[df["value_edge"] >= float(min_edge)]
    # add a simple stake suggestion (flat) - you can change later
    df["suggested_stake_units"] = 1
    return df.sort_values("value_edge", ascending=False).head(50)

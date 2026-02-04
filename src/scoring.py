from __future__ import annotations

import pandas as pd


def build_runner_scores(runners: pd.DataFrame) -> pd.DataFrame:
    df = runners.copy()

    # numeric safety
    df["best_price_dec"] = pd.to_numeric(df["best_price_dec"], errors="coerce")
    df = df.dropna(subset=["best_price_dec"]).copy()

    # raw market probability
    df["market_prob_raw"] = 1.0 / df["best_price_dec"]

    # normalize market prob within race (removes overround)
    df["market_prob"] = df.groupby("race_id")["market_prob_raw"].transform(
        lambda s: s / max(1e-9, s.sum())
    )

    # rating feature
    df["rating"] = pd.to_numeric(df.get("rating", 0), errors="coerce").fillna(0)
    df["rating_norm"] = df.groupby("race_id")["rating"].transform(
        lambda s: (s - s.min()) / max(1e-9, (s.max() - s.min()))
    )

    # recency
    df["days_since"] = pd.to_numeric(df.get("days_since", 60), errors="coerce").fillna(60).clip(0, 120)
    df["recency"] = 1.0 - (df["days_since"].clip(0, 60) / 60.0)

    # course & distance
    df["cd"] = (
        df.get("course_distance", "")
        .astype(str)
        .str.contains("cd", case=False, na=False)
        .astype(int)
    )

    # composite score
    df["score_raw"] = (
        0.55 * df["rating_norm"]
        + 0.25 * df["recency"]
        + 0.20 * df["cd"]
    ).clip(0, 1)

    # model probability per race
    df["model_prob"] = df.groupby("race_id")["score_raw"].transform(
        lambda s: s / max(1e-9, s.sum())
    )

    # value edge vs normalized market prob
    df["value_edge"] = df["model_prob"] - df["market_prob"]

    # convenience
    df["runner_count"] = df.groupby("race_id")["runner"].transform("count")
    df["confidence"] = (df["score_raw"] * 100).round(1)

    return df.sort_values(
        ["date", "course", "off_time", "value_edge"],
        ascending=[True, True, True, False],
    )


def build_value_bets(scored: pd.DataFrame, min_edge: float) -> pd.DataFrame:
    df = scored.copy()
    df["value_edge"] = pd.to_numeric(df["value_edge"], errors="coerce")
    df = df.dropna(subset=["value_edge"]).copy()
    df = df[df["value_edge"] >= float(min_edge)].copy()
    if df.empty:
        return df
    df["suggested_stake_units"] = 1
    return df.sort_values("value_edge", ascending=False).head(50)

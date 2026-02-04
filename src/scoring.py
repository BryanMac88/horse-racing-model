from __future__ import annotations

import pandas as pd


def build_runner_scores(runners: pd.DataFrame) -> pd.DataFrame:
    df = runners.copy()

    # Odds → raw market probability
    df["best_price_dec"] = pd.to_numeric(df["best_price_dec"], errors="coerce")
    df = df.dropna(subset=["best_price_dec"]).copy()
    df["market_prob_raw"] = 1.0 / df["best_price_dec"]

    # NORMALISE market probability per race (removes overround)
    df["market_prob"] = df.groupby("race_id")["market_prob_raw"].transform(
        lambda s: s / max(1e-9, s.sum())
    )

    # Rating normalisation per race
    df["rating"] = pd.to_numeric(df.get("rating", 0), errors="coerce").fillna(0)
    df["rating_norm"] = df.groupby("race_id")["rating"].transform(
        lambda s: (s - s.min()) / max(1e-9, (s.max() - s.min()))
    )

    # Recency score (0–1)
    df["days_since"] = pd.to_numeric(df.get("days_since", 60), errors="coerce").fillna(60)
    df["days_since"] = df["days_since"].clip(0, 120)
    df["recency"] = 1.0 - (df["days_since"].clip(0, 60) / 60.0)

    # Course & distance flag
    df["cd"] = (
        df.get("course_distance", "")
        .astype(str)
        .str.contains("cd", case=False, na=False)
        .astype(int)
    )

    # Composite model score (0–1)
    df["score_raw"] = (
        0.55 * df["rating_norm"]
        + 0.25 * df["recency"]
        + 0.20 * df["cd"]
    ).clip(0, 1)

    # Convert scores → probabilities per race
    df["model_prob"] = df.groupby("race_id")["score_raw"].transform(
        lamb

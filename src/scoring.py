from __future__ import annotations

import numpy as np
import pandas as pd


def _going_bucket(going: str) -> str:
    g = (going or "").lower()
    if any(k in g for k in ("soft", "heavy", "yielding", "slow", "muddy")):
        return "soft"
    if any(k in g for k in ("good", "firm", "standard", "fast", "hard")):
        return "good"
    return "other"


def build_runner_scores(runners: pd.DataFrame) -> pd.DataFrame:
    df = runners.copy()

    df["best_price_dec"] = pd.to_numeric(df["best_price_dec"], errors="coerce")
    df = df.dropna(subset=["best_price_dec"]).copy()
    if df.empty:
        return df

    df["market_prob_raw"] = 1.0 / df["best_price_dec"]
    df["market_prob"] = df.groupby("race_id")["market_prob_raw"].transform(
        lambda s: s / max(1e-9, s.sum())
    )

    df["days_since"] = pd.to_numeric(df.get("days_since", 60), errors="coerce").fillna(60).clip(0, 365)
    df["recency"] = 1.0 - (df["days_since"].clip(0, 60) / 60.0)

    df["recent_form_score"] = (
        pd.to_numeric(df.get("recent_form_score", 0), errors="coerce").fillna(0.0).clip(0, 1)
    )
    df["wins_last5"] = pd.to_numeric(df.get("wins_last5", 0), errors="coerce").fillna(0)
    df["places_last5"] = pd.to_numeric(df.get("places_last5", 0), errors="coerce").fillna(0)
    df["avg_pos_last3"] = pd.to_numeric(df.get("avg_pos_last3", 10), errors="coerce").fillna(10)
    df["form_runs"] = pd.to_numeric(df.get("form_runs", 0), errors="coerce").fillna(0)

    df["soft_place_rate"] = pd.to_numeric(df.get("soft_place_rate", 0), errors="coerce").fillna(0)
    df["good_place_rate"] = pd.to_numeric(df.get("good_place_rate", 0), errors="coerce").fillna(0)
    df["soft_win_rate"] = pd.to_numeric(df.get("soft_win_rate", 0), errors="coerce").fillna(0)
    df["good_win_rate"] = pd.to_numeric(df.get("good_win_rate", 0), errors="coerce").fillna(0)

    race_going = df.get("going", pd.Series([""] * len(df))).astype(str)
    df["going_bucket"] = race_going.map(_going_bucket)
    df["going_fit"] = 0.0
    soft_mask = df["going_bucket"] == "soft"
    good_mask = df["going_bucket"] == "good"
    df.loc[soft_mask, "going_fit"] = df.loc[soft_mask, "soft_place_rate"].clip(0, 1)
    df.loc[good_mask, "going_fit"] = df.loc[good_mask, "good_place_rate"].clip(0, 1)

    df["rating"] = pd.to_numeric(df.get("rating", np.nan), errors="coerce")
    has_rating = df["rating"].notna() & (df["rating"] > 0)
    df["rating_norm"] = 0.0
    if has_rating.any():
        df.loc[has_rating, "rating_norm"] = (
            df[has_rating]
            .groupby("race_id")["rating"]
            .transform(lambda s: (s - s.min()) / max(1e-9, (s.max() - s.min())))
        )

    df["runner_count"] = df.groupby("race_id")["runner"].transform("count")
    df["field_factor"] = (1.0 / df["runner_count"].clip(lower=4)).clip(0.05, 0.25)

    cd_raw = df.get("course_distance", "")
    if not isinstance(cd_raw, pd.Series):
        df["cd"] = 0
    else:
        df["cd"] = cd_raw.astype(str).str.contains("cd", case=False, na=False).astype(int)

    df["place_rate_last5"] = (df["places_last5"] / 5.0).clip(0, 1)
    df["win_rate_last5"] = (df["wins_last5"] / 5.0).clip(0, 1)
    df["pos_score"] = (1.0 - ((df["avg_pos_last3"] - 1.0) / 10.0)).clip(0, 1)

    df["score_raw"] = (
        0.28 * df["recent_form_score"]
        + 0.18 * df["place_rate_last5"]
        + 0.12 * df["win_rate_last5"]
        + 0.12 * df["going_fit"]
        + 0.10 * df["recency"]
        + 0.08 * df["pos_score"]
        + 0.07 * df["rating_norm"]
        + 0.05 * df["field_factor"]
    ).clip(0, 1)

    has_form = df["form_runs"] > 0
    df["form_weight"] = np.where(has_form, 0.55, 0.20)
    df["score_blended"] = (
        df["form_weight"] * df["score_raw"] + (1.0 - df["form_weight"]) * df["market_prob"]
    ).clip(1e-6, 1.0)

    df["model_prob"] = df.groupby("race_id")["score_blended"].transform(
        lambda s: s / max(1e-9, s.sum())
    )
    df["value_edge"] = df["model_prob"] - df["market_prob"]
    df["confidence"] = (df["score_blended"] * 100).round(1)

    # % columns for Sheets readability
    df["market_prob_pct"] = (df["market_prob"] * 100).round(1)
    df["model_prob_pct"] = (df["model_prob"] * 100).round(1)
    df["value_edge_pct"] = (df["value_edge"] * 100).round(1)
    df["recent_form_pct"] = (df["recent_form_score"] * 100).round(1)
    df["going_fit_pct"] = (df["going_fit"] * 100).round(1)

    return df.sort_values(
        ["date", "course", "off_time", "value_edge"],
        ascending=[True, True, True, False],
    )


def build_value_bets(scored: pd.DataFrame, min_edge: float) -> pd.DataFrame:
    df = scored.copy()
    if df.empty:
        return df
    df["value_edge"] = pd.to_numeric(df["value_edge"], errors="coerce")
    df = df.dropna(subset=["value_edge"]).copy()
    df = df[df["value_edge"] >= float(min_edge)].copy()
    if df.empty:
        return df
    df["suggested_stake_units"] = 1
    return df.sort_values("value_edge", ascending=False).head(50)

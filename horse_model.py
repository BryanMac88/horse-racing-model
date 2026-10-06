from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from src.sources.irishracing import IrishRacingClient
from src.scoring import build_runner_scores, build_value_bets
from src.sheets import SheetsWriter

TZ = ZoneInfo("Europe/Dublin")
NIGHT_BEFORE_HOUR = 22
MIN_MOVE_PCT = 0.015
MAX_RUNNERS_FOR_SIGNAL = 18


def env(name: str, default: str | None = None) -> str:
    v = os.getenv(name)
    if v is None or v == "":
        if default is None:
            raise RuntimeError(f"Missing required env var: {name}")
        return default
    return v


def _normalize_off_time(s: str) -> str:
    t = str(s).strip()
    if not t:
        return t
    t = t.replace(".", ":")
    m = pd.Series([t]).str.extract(r"^(\d{1,2}):(\d{2})$").iloc[0]
    if pd.isna(m[0]) or pd.isna(m[1]):
        return t
    return f"{int(m[0]):02d}:{int(m[1]):02d}"


def _snapshot_period(local_dt: datetime, target_date, after_cutoff: bool) -> str:
    hour = local_dt.hour
    snap_date = local_dt.date()
    if snap_date == (target_date - timedelta(days=1)) and hour >= 20:
        return "NIGHT_BEFORE"
    if snap_date == target_date and hour < 7:
        return "NIGHT_BEFORE"
    if snap_date == target_date and 7 <= hour < 12:
        return "MORNING"
    if snap_date == target_date and hour >= 12:
        return "AFTERNOON"
    if after_cutoff and snap_date == target_date - timedelta(days=1):
        return "NIGHT_BEFORE"
    return "OTHER"


def _prep_snapshots_df(raw: pd.DataFrame) -> pd.DataFrame:
    if raw is None or raw.empty:
        return pd.DataFrame()
    df = raw.copy()
    df.columns = [str(c).strip() for c in df.columns]
    required = {"snapshot_time", "race_id", "runner", "best_price_dec"}
    if not required.issubset(set(df.columns)):
        return pd.DataFrame()
    df["snapshot_time"] = pd.to_datetime(df["snapshot_time"], errors="coerce", utc=True)
    df["best_price_dec"] = pd.to_numeric(df["best_price_dec"], errors="coerce")
    df = df.dropna(subset=["snapshot_time", "best_price_dec", "race_id", "runner"]).copy()
    df["race_id"] = df["race_id"].astype(str).str.strip()
    df["runner"] = df["runner"].astype(str).str.strip()
    df["snapshot_local"] = df["snapshot_time"].dt.tz_convert(TZ)
    if "off_time" in df.columns:
        df["off_time_norm"] = df["off_time"].astype(str).apply(_normalize_off_time)
    else:
        df["off_time_norm"] = ""
    df["race_dt"] = pd.NaT
    if "date" in df.columns and "off_time_norm" in df.columns:
        race_dt = pd.to_datetime(
            df["date"].astype(str) + " " + df["off_time_norm"].astype(str), errors="coerce"
        )
        race_dt = race_dt.dt.tz_localize(TZ, nonexistent="shift_forward", ambiguous="NaT")
        df["race_dt"] = race_dt
    return df


def _latest_per_runner(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    d = df.sort_values("snapshot_local")
    return d.groupby(["race_id", "runner"], as_index=False).tail(1)


def _first_per_runner(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    d = df.sort_values("snapshot_local")
    return d.groupby(["race_id", "runner"], as_index=False).head(1)


def build_price_tabs(snap_log: pd.DataFrame, target_date) -> dict:
    empty = pd.DataFrame()
    if snap_log is None or snap_log.empty:
        return {"PRICES_NIGHT_BEFORE": empty, "PRICES_MORNING": empty, "PRICES_LATEST": empty}

    d = snap_log.copy()
    if "date" in d.columns:
        d = d[d["date"].astype(str) == target_date.isoformat()].copy()
    if d.empty:
        return {"PRICES_NIGHT_BEFORE": empty, "PRICES_MORNING": empty, "PRICES_LATEST": empty}

    if "period" not in d.columns or d["period"].astype(str).str.len().eq(0).all():
        d["period"] = d["snapshot_local"].apply(
            lambda x: _snapshot_period(x, target_date, after_cutoff=False) if pd.notna(x) else "OTHER"
        )

    night = d[d["period"].astype(str) == "NIGHT_BEFORE"].copy()
    if night.empty:
        night = _first_per_runner(d)
        night["period"] = "NIGHT_BEFORE_FALLBACK"
    else:
        night = _first_per_runner(night)

    morning = d[d["period"].astype(str) == "MORNING"].copy()
    if morning.empty:
        morning = d[
            (d["snapshot_local"].dt.date == target_date)
            & (d["snapshot_local"].dt.hour >= 7)
            & (d["snapshot_local"].dt.hour < 12)
        ].copy()
    morning = _latest_per_runner(morning) if not morning.empty else empty

    latest = _latest_per_runner(d)

    def slim(x: pd.DataFrame) -> pd.DataFrame:
        if x is None or x.empty:
            return empty
        cols = [
            c for c in [
                "snapshot_time", "snapshot_local", "period", "date", "course", "off_time",
                "race_name", "race_id", "runner", "best_price_dec",
            ] if c in x.columns
        ]
        return x[cols].sort_values([c for c in ["course", "off_time", "runner"] if c in cols])

    return {
        "PRICES_NIGHT_BEFORE": slim(night),
        "PRICES_MORNING": slim(morning),
        "PRICES_LATEST": slim(latest),
    }


def compute_movers_from_to(start_df: pd.DataFrame, end_df: pd.DataFrame) -> pd.DataFrame:
    if start_df is None or end_df is None or start_df.empty or end_df.empty:
        return pd.DataFrame()
    s = start_df.copy()
    e = end_df.copy()
    key = ["race_id", "runner"]
    for frame in (s, e):
        frame["race_id"] = frame["race_id"].astype(str).str.strip()
        frame["runner"] = frame["runner"].astype(str).str.strip()
        frame["best_price_dec"] = pd.to_numeric(frame["best_price_dec"], errors="coerce")
    s = s.dropna(subset=["best_price_dec"])
    e = e.dropna(subset=["best_price_dec"])
    if s.empty or e.empty:
        return pd.DataFrame()

    s2 = s[key + ["best_price_dec"]].rename(columns={"best_price_dec": "price_start"})
    meta_cols = [c for c in ["date", "course", "off_time", "race_name"] if c in e.columns]
    e2 = e[key + ["best_price_dec"] + meta_cols].rename(columns={"best_price_dec": "price_now"})
    if "snapshot_local" in s.columns:
        s2["time_start"] = s["snapshot_local"].values
    if "snapshot_local" in e.columns:
        e2["time_now"] = e["snapshot_local"].values

    merged = s2.merge(e2, on=key, how="inner")
    if merged.empty:
        return pd.DataFrame()

    merged["pct_change"] = (merged["price_now"] - merged["price_start"]) / merged["price_start"]
    merged["direction"] = merged["pct_change"].apply(
        lambda x: "SHORTENING" if x < 0 else ("DRIFTING" if x > 0 else "UNCHANGED")
    )
    merged["abs_pct"] = merged["pct_change"].abs()
    merged = merged[merged["abs_pct"] >= MIN_MOVE_PCT].copy()
    merged["pct_change_display"] = (merged["pct_change"] * 100).round(1)

    keep = [c for c in [
        "date", "course", "off_time", "race_name", "race_id", "runner",
        "price_start", "price_now", "pct_change", "pct_change_display",
        "direction", "time_start", "time_now",
    ] if c in merged.columns]
    return merged[keep].sort_values("pct_change", ascending=True)


def compute_movers_last_two(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    d = df.dropna(subset=["snapshot_local", "best_price_dec", "race_id", "runner"]).copy()
    d = d.sort_values(["race_id", "runner", "snapshot_local"])
    key = ["race_id", "runner"]
    last_two = d.groupby(key).tail(2).copy()
    counts = last_two.groupby(key).size().reset_index(name="n")
    valid = counts[counts["n"] >= 2][key]
    if valid.empty:
        return pd.DataFrame()
    last_two = last_two.merge(valid, on=key, how="inner")
    last_two["rn"] = last_two.groupby(key).cumcount()
    prev = last_two[last_two["rn"] == 0].copy()
    curr = last_two[last_two["rn"] == 1].copy()
    curr_out = curr[key].copy()
    curr_out["price_now"] = curr["best_price_dec"].values
    curr_out["time_now"] = curr["snapshot_local"].values
    for c in ["date", "course", "off_time", "race_name"]:
        curr_out[c] = curr[c].values if c in curr.columns else ""
    prev_out = prev[key].copy()
    prev_out["price_prev"] = prev["best_price_dec"].values
    prev_out["time_prev"] = prev["snapshot_local"].values
    merged = prev_out.merge(curr_out, on=key, how="inner")
    if merged.empty:
        return pd.DataFrame()
    merged["pct_change"] = (merged["price_now"] - merged["price_prev"]) / merged["price_prev"]
    merged["direction"] = merged["pct_change"].apply(lambda x: "SHORTENING" if x < 0 else "DRIFTING")
    merged = merged[merged["pct_change"].abs() >= MIN_MOVE_PCT].copy()
    merged["pct_change_display"] = (merged["pct_change"] * 100).round(1)
    keep = [
        "date", "course", "off_time", "race_name", "race_id", "runner",
        "price_prev", "price_now", "pct_change", "pct_change_display",
        "direction", "time_prev", "time_now",
    ]
    return merged[keep].sort_values("pct_change", ascending=True)


def compute_movers_night_before(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty or "race_dt" not in df.columns:
        return pd.DataFrame()
    d = df.dropna(subset=["race_dt"]).copy()
    if d.empty:
        return pd.DataFrame()
    d["night_start"] = (d["race_dt"].dt.normalize() - pd.Timedelta(days=1)) + pd.Timedelta(
        hours=NIGHT_BEFORE_HOUR
    )
    d["cutoff_30m"] = d["race_dt"] - pd.Timedelta(minutes=30)
    key = ["race_id", "runner"]
    night = d[d["snapshot_local"] >= d["night_start"]].sort_values("snapshot_local")
    night_first = night.groupby(key, as_index=False).first()
    pre = d[d["snapshot_local"] <= d["cutoff_30m"]].sort_values("snapshot_local")
    pre_last = pre.groupby(key, as_index=False).last()
    merged = night_first.merge(
        pre_last[key + ["best_price_dec", "snapshot_local"]],
        on=key, how="inner", suffixes=("_start", "_30m"),
    )
    if merged.empty:
        return pd.DataFrame()
    merged = merged.rename(columns={
        "best_price_dec_start": "start_price_night_before",
        "best_price_dec_30m": "price_30min_before",
        "snapshot_local_start": "time_start",
        "snapshot_local_30m": "time_30min_before",
    })
    merged["pct_change"] = (
        merged["price_30min_before"] - merged["start_price_night_before"]
    ) / merged["start_price_night_before"]
    merged["direction"] = merged["pct_change"].apply(lambda x: "SHORTENING" if x < 0 else "DRIFTING")
    merged = merged[merged["pct_change"].abs() >= MIN_MOVE_PCT].copy()
    for c in ["date", "course", "off_time", "race_name"]:
        if c not in merged.columns:
            merged[c] = ""
    keep = [
        "date", "course", "off_time", "race_name", "race_id", "runner",
        "start_price_night_before", "price_30min_before", "pct_change", "direction",
        "time_start", "time_30min_before",
    ]
    return merged[keep].sort_values("pct_change", ascending=True)


def compute_persistent_shorteners(snap_df: pd.DataFrame) -> pd.DataFrame:
    if snap_df is None or snap_df.empty:
        return pd.DataFrame()
    d = snap_df.dropna(subset=["race_id", "runner", "snapshot_local", "best_price_dec"]).copy()
    d = d.sort_values(["race_id", "runner", "snapshot_local"])
    key = ["race_id", "runner"]
    last4 = d.groupby(key).tail(4).copy()
    last4["prev_price"] = last4.groupby(key)["best_price_dec"].shift(1)
    last4["down"] = (last4["best_price_dec"] < last4["prev_price"]).astype(int)
    return (
        last4.groupby(key, as_index=False)["down"]
        .sum()
        .rename(columns={"down": "shorten_steps_last4"})
    )


def build_signals(scored, movers_2h, movers_night, persistence) -> pd.DataFrame:
    df = scored.copy()
    df["runner_count"] = pd.to_numeric(df.get("runner_count", 0), errors="coerce").fillna(0)
    df = df[df["runner_count"] <= MAX_RUNNERS_FOR_SIGNAL].copy()

    df["value_edge"] = pd.to_numeric(df.get("value_edge", 0), errors="coerce").fillna(0.0)
    df["win_score"] = pd.to_numeric(df.get("win_score", 0), errors="coerce").fillna(0.0)
    df["is_favorite"] = pd.to_numeric(df.get("is_favorite", 0), errors="coerce").fillna(0).astype(int)
    df["form_supports_fav"] = pd.to_numeric(df.get("form_supports_fav", 0), errors="coerce").fillna(0).astype(int)
    df["form_quality"] = pd.to_numeric(df.get("form_quality", 0), errors="coerce").fillna(0.0)

    def _mover_col(src, name):
        if src is None or src.empty or "pct_change" not in src.columns:
            return pd.DataFrame(columns=["race_id", "runner", name])
        t = src[["race_id", "runner", "pct_change"]].rename(columns={"pct_change": name})
        t["race_id"] = t["race_id"].astype(str).str.strip()
        t["runner"] = t["runner"].astype(str).str.strip()
        return t

    m2 = _mover_col(movers_2h, "mover_2h_pct")
    mn = _mover_col(movers_night, "mover_night_pct")
    ps = (
        persistence
        if persistence is not None and not persistence.empty
        else pd.DataFrame(columns=["race_id", "runner", "shorten_steps_last4"])
    )
    if not ps.empty:
        ps = ps.copy()
        ps["race_id"] = ps["race_id"].astype(str).str.strip()
        ps["runner"] = ps["runner"].astype(str).str.strip()

    df["race_id"] = df["race_id"].astype(str).str.strip()
    df["runner"] = df["runner"].astype(str).str.strip()
    out = (
        df.merge(m2, on=["race_id", "runner"], how="left")
        .merge(mn, on=["race_id", "runner"], how="left")
        .merge(ps, on=["race_id", "runner"], how="left")
    )
    out["mover_2h_pct"] = pd.to_numeric(out.get("mover_2h_pct", 0), errors="coerce").fillna(0.0)
    out["mover_night_pct"] = pd.to_numeric(out.get("mover_night_pct", 0), errors="coerce").fillna(0.0)
    out["shorten_steps_last4"] = (
        pd.to_numeric(out.get("shorten_steps_last4", 0), errors="coerce").fillna(0).astype(int)
    )
    out["shorten_boost"] = (
        0.15 * (-out["mover_2h_pct"]).clip(lower=0)
        + 0.20 * (-out["mover_night_pct"]).clip(lower=0)
        + 0.05 * out["shorten_steps_last4"].clip(0, 3)
    )
    out["signal_score"] = (
        1.00 * out["win_score"]
        + 0.25 * out["form_quality"]
        + 0.15 * out["is_favorite"]
        + 0.20 * out["form_supports_fav"]
        + out["shorten_boost"]
        + 0.10 * out["value_edge"].clip(lower=0)
    )
    out["value_edge_pct"] = (out["value_edge"] * 100).round(1)
    out["win_score_pct"] = (out["win_score"] * 100).round(1)
    out["mover_2h_display_pct"] = (out["mover_2h_pct"] * 100).round(1)
    out["mover_night_display_pct"] = (out["mover_night_pct"] * 100).round(1)
    return out.sort_values(
        ["date", "course", "off_time", "signal_score"], ascending=[True, True, True, False]
    )


def build_bets_to_place(signals: pd.DataFrame) -> pd.DataFrame:
    if signals is None or signals.empty:
        return pd.DataFrame()
    df = signals.copy()
    df["signal_score"] = pd.to_numeric(df.get("signal_score", 0), errors="coerce").fillna(0.0)
    df["win_score"] = pd.to_numeric(df.get("win_score", 0), errors="coerce").fillna(0.0)
    df["value_edge"] = pd.to_numeric(df.get("value_edge", 0), errors="coerce").fillna(0.0)
    df["is_favorite"] = pd.to_numeric(df.get("is_favorite", 0), errors="coerce").fillna(0).astype(int)
    df["form_supports_fav"] = pd.to_numeric(df.get("form_supports_fav", 0), errors="coerce").fillna(0).astype(int)
    df["form_quality"] = pd.to_numeric(df.get("form_quality", 0), errors="coerce").fillna(0.0)
    df["mover_2h_pct"] = pd.to_numeric(df.get("mover_2h_pct", 0), errors="coerce").fillna(0.0)
    df["mover_night_pct"] = pd.to_numeric(df.get("mover_night_pct", 0), errors="coerce").fillna(0.0)

    weak = (
        (df["win_score"] < 0.08)
        & (df["is_favorite"] == 0)
        & (df["form_quality"] < 0.25)
        & (df["mover_2h_pct"] > -MIN_MOVE_PCT)
        & (df["mover_night_pct"] > -MIN_MOVE_PCT)
    )
    df = df[~weak].copy()
    if df.empty:
        return df

    df["pick_tier"] = 1
    df.loc[df["form_supports_fav"] == 1, "pick_tier"] = 3
    solid = (
        (df["form_quality"] >= 0.40)
        | (df["win_score"] >= 0.22)
        | (df["mover_2h_pct"] <= -MIN_MOVE_PCT)
        | (df["mover_night_pct"] <= -MIN_MOVE_PCT)
    )
    df.loc[(df["pick_tier"] < 3) & solid, "pick_tier"] = 2
    df["rank_key"] = df["pick_tier"] * 10.0 + df["signal_score"]
    df["rank_in_race"] = df.groupby("race_id")["rank_key"].rank(ascending=False, method="first")

    primary = df[df["rank_in_race"] == 1].copy()
    primary["pick_role"] = "PRIMARY_WINNER"
    primary["suggested_stake_units"] = np.where(primary["pick_tier"] >= 2, 1.0, 0.5)

    second = df[df["rank_in_race"] == 2].copy()
    if not second.empty and not primary.empty:
        merged = second.merge(
            primary[["race_id", "signal_score"]].rename(columns={"signal_score": "primary_score"}),
            on="race_id", how="left",
        )
        keep_second = merged["signal_score"] >= (merged["primary_score"] * 0.85)
        second = merged[keep_second].copy()
        second["pick_role"] = "SECONDARY"
        second["suggested_stake_units"] = 0.5
        out = pd.concat([primary, second], ignore_index=True)
    else:
        out = primary

    out["bet_key"] = (
        out["date"].astype(str) + "|" + out["course"].astype(str) + "|"
        + out["off_time"].astype(str) + "|" + out["runner"].astype(str)
    )
    if "value_edge_pct" not in out.columns:
        out["value_edge_pct"] = (out["value_edge"] * 100).round(1)
    if "win_score_pct" not in out.columns:
        out["win_score_pct"] = (out["win_score"] * 100).round(1)
    return out.sort_values(
        ["date", "course", "off_time", "pick_tier", "signal_score"],
        ascending=[True, True, True, False, False],
    )


def update_bet_recs_log(writer: SheetsWriter, bets_to_place: pd.DataFrame) -> None:
    if bets_to_place is None or bets_to_place.empty or "bet_key" not in bets_to_place.columns:
        return
    existing = writer.read_df("BET_RECS_LOG")
    existing_keys = (
        set(existing["bet_key"].astype(str).tolist())
        if (not existing.empty and "bet_key" in existing.columns)
        else set()
    )
    new_rows = bets_to_place[~bets_to_place["bet_key"].astype(str).isin(existing_keys)].copy()
    if new_rows.empty:
        return
    out = pd.DataFrame()
    out["timestamp_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    out["bet_key"] = new_rows["bet_key"]
    for c in [
        "date", "course", "off_time", "race_name", "runner", "best_price_dec",
        "signal_score", "win_score", "value_edge", "value_edge_pct",
        "is_favorite", "form_supports_fav", "pick_role",
        "mover_2h_pct", "mover_night_pct", "shorten_steps_last4", "suggested_stake_units",
    ]:
        out[c] = new_rows.get(c, "")
    out["result"] = ""
    out["pnl_units"] = ""
    out["notes"] = ""
    writer.append_df("BET_RECS_LOG", out)


def build_dashboard(races, runners, movers2h, moversnight, bets) -> pd.DataFrame:
    return pd.DataFrame([
        {"metric": "last_run_local", "value": datetime.now(timezone.utc).astimezone(TZ).isoformat(timespec="seconds")},
        {"metric": "races_target_day", "value": int(len(races)) if races is not None else 0},
        {"metric": "runners_target_day", "value": int(len(runners)) if runners is not None else 0},
        {"metric": "movers_2h_rows", "value": int(len(movers2h)) if movers2h is not None else 0},
        {"metric": "movers_night_rows", "value": int(len(moversnight)) if moversnight is not None else 0},
        {"metric": "bets_to_place", "value": int(len(bets)) if bets is not None else 0},
    ])


def _order_runner_cols(df: pd.DataFrame) -> pd.DataFrame:
    preferred = [
        "date", "course", "off_time", "runner", "best_price_dec",
        "is_favorite", "win_score_pct", "market_prob_pct", "model_prob_pct",
        "value_edge_pct", "form_quality_pct", "form_supports_fav",
        "recent_form_pct", "days_since", "form_string", "form_runs",
        "going", "going_fit_pct", "wins_last5", "places_last5",
        "race_name", "race_id",
    ]
    cols = [c for c in preferred if c in df.columns] + [c for c in df.columns if c not in preferred]
    return df[cols]


def main() -> int:
    sheet_name = env("SHEET_NAME")
    region = env("REGION", "all").lower()
    min_edge = float(env("MIN_VALUE_EDGE", "0.00"))

    client = IrishRacingClient(region=region)
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(TZ)
    today = now_local.date()
    tomorrow = today + timedelta(days=1)
    after_cutoff = now_local.hour >= NIGHT_BEFORE_HOUR
    target_label = "TOMORROW" if after_cutoff else "TODAY"
    target_date = tomorrow if after_cutoff else today

    races_df, runners_df = client.fetch_for_date(target_date)
    if races_df.empty or runners_df.empty:
        print(f"No races/runners found for target_date={target_date} ({target_label}).")
        return 0

    runners_df = client.enrich_with_best_prices(runners_df)
    runners_df = client.enrich_with_form(runners_df, max_horses=80, max_workers=8, max_runs=8)

    scored = build_runner_scores(runners_df)
    value_bets = build_value_bets(scored, min_edge=min_edge)
    writer = SheetsWriter(sheet_name=sheet_name, credentials_path="credentials.json")
    period = _snapshot_period(now_local, target_date, after_cutoff)

    writer.write_df("TARGET_DAY", pd.DataFrame([{
        "run_utc": now_utc.isoformat(timespec="seconds"),
        "run_local_dublin": now_local.isoformat(timespec="seconds"),
        "dublin_hour": int(now_local.hour),
        "after_22_rule": bool(after_cutoff),
        "target_label": target_label,
        "target_date": target_date.isoformat(),
        "snapshot_period": period,
        "night_before_hour_local": NIGHT_BEFORE_HOUR,
        "region": region,
    }]))

    writer.write_df("RACES_TARGET", races_df)
    writer.write_df("RUNNERS_TARGET", _order_runner_cols(scored))
    writer.write_df("VALUE_BETS_TARGET", value_bets)

    snapshots_new = scored[
        ["date", "course", "off_time", "race_name", "race_id", "runner", "best_price_dec"]
    ].copy()
    snapshots_new.insert(0, "snapshot_time", now_utc.isoformat(timespec="seconds"))
    snapshots_new.insert(1, "target_label", target_label)
    snapshots_new.insert(2, "period", period)
    writer.write_df("MARKET_SNAPSHOTS_TARGET", snapshots_new)
    writer.append_df("MARKET_SNAPSHOTS_LOG", snapshots_new)

    snap_log = _prep_snapshots_df(writer.read_df("MARKET_SNAPSHOTS_LOG"))
    if not snap_log.empty and "date" in snap_log.columns:
        snap_for_day = snap_log[snap_log["date"].astype(str) == target_date.isoformat()].copy()
    else:
        snap_for_day = snap_log

    if not snap_for_day.empty:
        snap_for_day = snap_for_day.copy()
        snap_for_day["period"] = snap_for_day["snapshot_local"].apply(
            lambda x: _snapshot_period(x, target_date, after_cutoff) if pd.notna(x) else "OTHER"
        )

    price_tabs = build_price_tabs(snap_for_day, target_date)
    writer.write_df("PRICES_NIGHT_BEFORE", price_tabs["PRICES_NIGHT_BEFORE"])
    writer.write_df("PRICES_MORNING", price_tabs["PRICES_MORNING"])
    writer.write_df("PRICES_LATEST", price_tabs["PRICES_LATEST"])

    movers_main = compute_movers_from_to(
        price_tabs["PRICES_NIGHT_BEFORE"], price_tabs["PRICES_LATEST"]
    )
    movers_morning = compute_movers_from_to(
        price_tabs["PRICES_MORNING"], price_tabs["PRICES_LATEST"]
    )
    movers_2h = compute_movers_last_two(snap_for_day)
    persistence = compute_persistent_shorteners(snap_for_day)

    biggest = movers_main.copy() if movers_main is not None else pd.DataFrame()
    if biggest.empty and movers_2h is not None and not movers_2h.empty:
        biggest = movers_2h.rename(columns={"price_prev": "price_start", "time_prev": "time_start"})

    writer.write_df("MARKET_MOVERS", biggest)
    writer.write_df("MARKET_MOVERS_2H", movers_2h)
    writer.write_df("MARKET_MOVERS_MORNING", movers_morning)

    if biggest is not None and not biggest.empty:
        shorteners = biggest[biggest["direction"] == "SHORTENING"].head(40)
        drifters = biggest[biggest["direction"] == "DRIFTING"].sort_values(
            "pct_change", ascending=False
        ).head(40)
    else:
        shorteners = pd.DataFrame()
        drifters = pd.DataFrame()
    writer.write_df("BIGGEST_SHORTENERS", shorteners)
    writer.write_df("BIGGEST_DRIFTERS", drifters)

    signals = build_signals(scored, movers_2h, movers_main, persistence)
    writer.write_df("SIGNALS", signals)
    bets_to_place = build_bets_to_place(signals)
    writer.write_df("BETS_TO_PLACE", bets_to_place)
    update_bet_recs_log(writer, bets_to_place)

    dashboard = build_dashboard(races_df, scored, movers_2h, movers_main, bets_to_place)
    writer.write_df("DASHBOARD", dashboard)
    writer.write_df("RUN_LOG", pd.DataFrame([{
        "run_utc": now_utc.isoformat(timespec="seconds"),
        "run_local_dublin": now_local.isoformat(timespec="seconds"),
        "region": region,
        "target_label": target_label,
        "target_date": target_date.isoformat(),
        "snapshot_period": period,
        "races": int(len(races_df)),
        "runners": int(len(scored)),
        "movers_main": int(len(biggest)) if biggest is not None else 0,
        "movers_2h": int(len(movers_2h)) if movers_2h is not None else 0,
        "bets": int(len(bets_to_place)),
        "snap_log_rows": int(len(snap_for_day)) if snap_for_day is not None else 0,
    }]))

    print(
        f"Update complete. target={target_label} {target_date} period={period} | "
        f"races={len(races_df)} runners={len(scored)} "
        f"movers={len(biggest) if biggest is not None else 0} "
        f"movers_2h={len(movers_2h) if movers_2h is not None else 0} "
        f"bets={len(bets_to_place)} snap_rows={len(snap_for_day) if snap_for_day is not None else 0}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

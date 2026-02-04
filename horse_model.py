from __future__ import annotations

import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from src.sources.irishracing import IrishRacingClient
from src.scoring import build_runner_scores, build_value_bets
from src.sheets import SheetsWriter

TZ = ZoneInfo("Europe/Dublin")


def env(name: str, default: str | None = None) -> str:
    v = os.getenv(name)
    if v is None or v == "":
        if default is None:
            raise RuntimeError(f"Missing required env var: {name}")
        return default
    return v


def compute_market_movers(snapshots: pd.DataFrame) -> pd.DataFrame:
    """
    Market movers comparing:
      - night_before: first snapshot after 18:00 (previous day, Dublin time)
      - pre_30m: last snapshot at or before race_time - 30 minutes (Dublin time)

    Requires snapshot rows over time. Will be empty until you have enough history.
    """
    if snapshots is None or snapshots.empty:
        return pd.DataFrame()

    df = snapshots.copy()

    # Normalize column names just in case
    df.columns = [str(c).strip() for c in df.columns]

    required = {"snapshot_time", "date", "off_time", "race_id", "runner", "best_price_dec"}
    if not required.issubset(set(df.columns)):
        return pd.DataFrame()

    df["snapshot_time"] = pd.to_datetime(df["snapshot_time"], errors="coerce", utc=True)
    df["best_price_dec"] = pd.to_numeric(df["best_price_dec"], errors="coerce")
    df = df.dropna(subset=["snapshot_time", "best_price_dec", "date", "off_time", "race_id", "runner"]).copy()

    # Build race datetime in Dublin time
    race_dt = pd.to_datetime(df["date"].astype(str) + " " + df["off_time"].astype(str), errors="coerce")
    df = df[race_dt.notna()].copy()
    df["race_dt"] = race_dt.dt.tz_localize(TZ, nonexistent="shift_forward", ambiguous="NaT")
    df = df[df["race_dt"].notna()].copy()

    df["snapshot_local"] = df["snapshot_time"].dt.tz_convert(TZ)

    # Night before start = previous day 18:00 local
    df["night_start"] = (df["race_dt"].dt.normalize() - pd.Timedelta(days=1)) + pd.Timedelta(hours=18)
    # 30 minutes before off
    df["cutoff_30m"] = df["race_dt"] - pd.Timedelta(minutes=30)

    key = ["race_id", "runner"]

    # night_before: first snapshot after night_start
    night = df[df["snapshot_local"] >= df["night_start"]].sort_values("snapshot_local")
    night_first = night.groupby(key, as_index=False).first()

    # pre_30m: last snapshot <= cutoff_30m
    pre = df[df["snapshot_local"] <= df["cutoff_30m"]].sort_values("snapshot_local")
    pre_last = pre.groupby(key, as_index=False).last()

    merged = night_first.merge(
        pre_last[key + ["best_price_dec", "snapshot_local"]],
        on=key,
        how="inner",
        suffixes=("_night", "_30m"),
    )

    if merged.empty:
        return pd.DataFrame()

    merged = merged.rename(
        columns={
            "best_price_dec_night": "price_night_before",
            "best_price_dec_30m": "price_30min_before",
            "snapshot_local_night": "time_night_before",
            "snapshot_local_30m": "time_30min_before",
        }
    )

    merged["pct_change"] = (merged["price_30min_before"] - merged["price_night_before"]) / merged["price_night_before"]
    merged["direction"] = merged["pct_change"].apply(lambda x: "SHORTENING" if x < 0 else "DRIFTING")

    # Keep readable race columns if present
    for c in ["date", "course", "off_time", "race_name"]:
        if c not in merged.columns:
            merged[c] = ""

    keep_cols = [
        "date", "course", "off_time", "race_name", "runner",
        "price_night_before", "price_30min_before", "pct_change", "direction",
        "time_night_before", "time_30min_before",
    ]

    merged = merged[keep_cols].sort_values("pct_change", ascending=True)  # most negative = biggest shorten
    return merged


def main() -> int:
    sheet_name = env("SHEET_NAME")
    region = env("REGION", "all").lower()
    min_edge = float(env("MIN_VALUE_EDGE", "0.00"))

    client = IrishRacingClient(region=region)

    races_df, runners_df = client.fetch_today()
    if races_df.empty or runners_df.empty:
        print("No races/runners found.")
        return 0

    runners_df = client.enrich_with_best_prices(runners_df)

    scored = build_runner_scores(runners_df)
    value_bets = build_value_bets(scored, min_edge=min_edge)

    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(TZ)

    writer = SheetsWriter(sheet_name=sheet_name, credentials_path="credentials.json")

    # Overwrite standard tabs
    writer.write_df("TODAYS_RACES", races_df)
    writer.write_df("RUNNERS", scored)
    writer.write_df("VALUE_BETS", value_bets)
    writer.write_df("RUN_LOG", pd.DataFrame([{
        "run_utc": now_utc.isoformat(timespec="seconds"),
        "run_local": now_local.isoformat(timespec="seconds"),
        "region": region,
        "races": int(len(races_df)),
        "runners": int(len(scored)),
        "value_bets": int(len(value_bets)),
    }]))

    # Build snapshot rows for this run
    snapshots = scored[[
        "date", "course", "off_time", "race_name", "race_id", "runner", "best_price_dec"
    ]].copy()
    snapshots.insert(0, "snapshot_time", now_utc.isoformat(timespec="seconds"))

    # Append snapshots (or create if missing)
    try:
        writer.append_df("MARKET_SNAPSHOTS", snapshots)
    except Exception:
        # If something is weird with the tab, overwrite it cleanly
        writer.write_df("MARKET_SNAPSHOTS", snapshots)

    # Read snapshots back and compute movers
    try:
        snap_ws = writer.book.worksheet("MARKET_SNAPSHOTS")
        snap_vals = snap_ws.get_all_values()
    except Exception:
        snap_vals = []

    # --- SELF-HEALING HEADER LOGIC ---
    header = snap_vals[0] if snap_vals else []
    has_header = isinstance(header, list) and ("snapshot_time" in header)

    if has_header and len(snap_vals) >= 2:
        snap_df = pd.DataFrame(snap_vals[1:], columns=header)
        movers = compute_market_movers(snap_df)
        writer.write_df("MARKET_MOVERS", movers)
    else:
        # Old/invalid snapshot sheet format: rewrite it cleanly and skip movers this run
        writer.write_df("MARKET_SNAPSHOTS", snapshots)
        writer.write_df("MARKET_MOVERS", pd.DataFrame())
    # --------------------------------

    print("Update complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

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


def _prep_snapshots_df(snapshots: pd.DataFrame) -> pd.DataFrame:
    """Common cleaning for snapshots read from Google Sheets."""
    if snapshots is None or snapshots.empty:
        return pd.DataFrame()

    df = snapshots.copy()
    df.columns = [str(c).strip() for c in df.columns]

    required = {"snapshot_time", "date", "off_time", "race_id", "runner", "best_price_dec"}
    if not required.issubset(set(df.columns)):
        return pd.DataFrame()

    df["snapshot_time"] = pd.to_datetime(df["snapshot_time"], errors="coerce", utc=True)
    df["best_price_dec"] = pd.to_numeric(df["best_price_dec"], errors="coerce")

    df = df.dropna(subset=["snapshot_time", "best_price_dec", "date", "off_time", "race_id", "runner"]).copy()

    # Race datetime in Dublin time
    race_dt = pd.to_datetime(df["date"].astype(str) + " " + df["off_time"].astype(str), errors="coerce")
    df = df[race_dt.notna()].copy()
    df["race_dt"] = race_dt.dt.tz_localize(TZ, nonexistent="shift_forward", ambiguous="NaT")
    df = df[df["race_dt"].notna()].copy()

    df["snapshot_local"] = df["snapshot_time"].dt.tz_convert(TZ)
    return df


def compute_market_movers_night_before(df: pd.DataFrame) -> pd.DataFrame:
    """
    Official movers:
      - Night before start price = first snapshot after 18:00 previous day (Dublin time)
      - Pre-race price = last snapshot at or before off_time - 30 minutes (Dublin time)
    """
    if df is None or df.empty:
        return pd.DataFrame()

    # Night before start = previous day 18:00 local
    df = df.copy()
    df["night_start"] = (df["race_dt"].dt.normalize() - pd.Timedelta(days=1)) + pd.Timedelta(hours=18)
    df["cutoff_30m"] = df["race_dt"] - pd.Timedelta(minutes=30)

    key = ["race_id", "runner"]

    night = df[df["snapshot_local"] >= df["night_start"]].sort_values("snapshot_local")
    night_first = night.groupby(key, as_index=False).first()

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
            "best_price_dec_night": "start_price_night_before",
            "best_price_dec_30m": "price_30min_before",
            "snapshot_local_night": "time_start_night_before",
            "snapshot_local_30m": "time_30min_before",
        }
    )

    merged["pct_change"] = (
        (merged["price_30min_before"] - merged["start_price_night_before"]) / merged["start_price_night_before"]
    )
    merged["direction"] = merged["pct_change"].apply(lambda x: "SHORTENING" if x < 0 else "DRIFTING")

    for c in ["date", "course", "off_time", "race_name"]:
        if c not in merged.columns:
            merged[c] = ""

    keep = [
        "date", "course", "off_time", "race_name", "runner",
        "start_price_night_before", "price_30min_before", "pct_change", "direction",
        "time_start_night_before", "time_30min_before",
    ]
    return merged[keep].sort_values("pct_change", ascending=True)


def compute_market_movers_last_2_hours(df: pd.DataFrame) -> pd.DataFrame:
    """
    Live movers:
      - Price 2 hours ago = first snapshot at/after (now - 2h)
      - Current price = latest snapshot
    This gives you 'where the money is going RIGHT NOW' even without night-before history.
    """
    if df is None or df.empty:
        return pd.DataFrame()

    now_local = datetime.now(timezone.utc).astimezone(TZ)
    window_start = now_local - pd.Timedelta(hours=2)

    df = df.copy()
    key = ["race_id", "runner"]

    # Only use snapshots within last 2 hours for the "start" point
    recent = df[df["snapshot_local"] >= window_start].sort_values("snapshot_local")
    start_2h = recent.groupby(key, as_index=False).first()

    # Latest snapshot overall (current price)
    latest = df.sort_values("snapshot_local").groupby(key, as_index=False).last()

    merged = start_2h.merge(
        latest[key + ["best_price_dec", "snapshot_local"]],
        on=key,
        how="inner",
        suffixes=("_2h", "_now"),
    )

    if merged.empty:
        return pd.DataFrame()

    merged = merged.rename(
        columns={
            "best_price_dec_2h": "price_2h_ago",
            "best_price_dec_now": "price_now",
            "snapshot_local_2h": "time_2h_ago",
            "snapshot_local_now": "time_now",
        }
    )

    merged["pct_change_2h"] = (merged["price_now"] - merged["price_2h_ago"]) / merged["price_2h_ago"]
    merged["direction_2h"] = merged["pct_change_2h"].apply(lambda x: "SHORTENING" if x < 0 else "DRIFTING")

    for c in ["date", "course", "off_time", "race_name"]:
        if c not in merged.columns:
            merged[c] = ""

    keep = [
        "date", "course", "off_time", "race_name", "runner",
        "price_2h_ago", "price_now", "pct_change_2h", "direction_2h",
        "time_2h_ago", "time_now",
    ]
    return merged[keep].sort_values("pct_change_2h", ascending=True)


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

    # Snapshot rows for this run
    snapshots_new = scored[[
        "date", "course", "off_time", "race_name", "race_id", "runner", "best_price_dec"
    ]].copy()
    snapshots_new.insert(0, "snapshot_time", now_utc.isoformat(timespec="seconds"))

    # Append snapshots (or create if missing/broken)
    try:
        writer.append_df("MARKET_SNAPSHOTS", snapshots_new)
    except Exception:
        writer.write_df("MARKET_SNAPSHOTS", snapshots_new)

    # Read snapshots back
    try:
        snap_ws = writer.book.worksheet("MARKET_SNAPSHOTS")
        snap_vals = snap_ws.get_all_values()
    except Exception:
        snap_vals = []

    header = snap_vals[0] if snap_vals else []
    has_header = isinstance(header, list) and ("snapshot_time" in header)

    if not (has_header and len(snap_vals) >= 2):
        # self-heal
        writer.write_df("MARKET_SNAPSHOTS", snapshots_new)
        writer.write_df("MARKET_MOVERS", pd.DataFrame())
        writer.write_df("MARKET_MOVERS_2H", pd.DataFrame())
        print("Update complete (snapshots healed; movers pending).")
        return 0

    snap_df = pd.DataFrame(snap_vals[1:], columns=header)
    snap_df = _prep_snapshots_df(snap_df)

    movers_night = compute_market_movers_night_before(snap_df)
    movers_2h = compute_market_movers_last_2_hours(snap_df)

    writer.write_df("MARKET_MOVERS", movers_night)
    writer.write_df("MARKET_MOVERS_2H", movers_2h)

    print("Update complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

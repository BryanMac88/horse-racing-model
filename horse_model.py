from __future__ import annotations

import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from src.sources.irishracing import IrishRacingClient
from src.scoring import build_runner_scores, build_value_bets
from src.sheets import SheetsWriter

TZ = ZoneInfo("Europe/Dublin")

# ✅ Your chosen "night before" start time (Dublin time)
NIGHT_BEFORE_HOUR = 22  # 22:00


def env(name: str, default: str | None = None) -> str:
    v = os.getenv(name)
    if v is None or v == "":
        if default is None:
            raise RuntimeError(f"Missing required env var: {name}")
        return default
    return v


def _normalize_off_time(s: str) -> str:
    """
    Convert times like:
      "7.00" -> "07:00"
      "19.30" -> "19:30"
      "7:00" -> "07:00"
    """
    t = str(s).strip()
    if not t:
        return t
    t = t.replace(".", ":")
    m = pd.Series([t]).str.extract(r"^(\d{1,2}):(\d{2})$").iloc[0]
    if pd.isna(m[0]) or pd.isna(m[1]):
        return t
    hh = int(m[0])
    mm = int(m[1])
    return f"{hh:02d}:{mm:02d}"


def _prep_snapshots_df(raw: pd.DataFrame) -> pd.DataFrame:
    """
    Clean snapshots read from Google Sheets.
    - Always creates snapshot_local
    - Tries to create race_dt (needed for night-before movers)
    """
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

    # Normalize off_time if present
    if "off_time" in df.columns:
        df["off_time_norm"] = df["off_time"].astype(str).apply(_normalize_off_time)
    else:
        df["off_time_norm"] = ""

    # Build race_dt when possible (needed for night-before movers)
    df["race_dt"] = pd.NaT
    if "date" in df.columns and "off_time_norm" in df.columns:
        race_dt = pd.to_datetime(
            df["date"].astype(str) + " " + df["off_time_norm"].astype(str),
            errors="coerce",
        )
        race_dt = race_dt.dt.tz_localize(TZ, nonexistent="shift_forward", ambiguous="NaT")
        df["race_dt"] = race_dt

    return df


def compute_market_movers_night_before(df: pd.DataFrame) -> pd.DataFrame:
    """
    Official movers:
      - Start price = first snapshot AFTER 22:00 previous day (Dublin time)
      - Pre-race price = last snapshot at/before off_time - 30 minutes
    """
    if df is None or df.empty or "race_dt" not in df.columns:
        return pd.DataFrame()

    d = df.dropna(subset=["race_dt"]).copy()
    if d.empty:
        return pd.DataFrame()

    # Windows
    d["night_start"] = (d["race_dt"].dt.normalize() - pd.Timedelta(days=1)) + pd.Timedelta(hours=NIGHT_BEFORE_HOUR)
    d["cutoff_30m"] = d["race_dt"] - pd.Timedelta(minutes=30)

    key = ["race_id", "runner"]

    night = d[d["snapshot_local"] >= d["night_start"]].sort_values("snapshot_local")
    night_first = night.groupby(key, as_index=False).first()

    pre = d[d["snapshot_local"] <= d["cutoff_30m"]].sort_values("snapshot_local")
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


def compute_market_movers_live_last_two_snapshots(df: pd.DataFrame) -> pd.DataFrame:
    """
    Live movers (robust, immediate):
      - Uses the last TWO snapshots per runner (no time window)
      - Populates as soon as there are >=2 snapshots
    """
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
    last_two["rn"] = last_two.groupby(key).cumcount()  # 0 then 1

    prev = last_two[last_two["rn"] == 0].copy()
    curr = last_two[last_two["rn"] == 1].copy()

    merged = prev.merge(
        curr[key + ["best_price_dec", "snapshot_local", "date", "course", "off_time", "race_name"]],
        on=key,
        how="inner",
        suffixes=("_prev", "_curr"),
    )

    if merged.empty:
        return pd.DataFrame()

    merged = merged.rename(
        columns={
            "best_price_dec_prev": "price_prev",
            "best_price_dec_curr": "price_now",
            "snapshot_local_prev": "time_prev",
            "snapshot_local_curr": "time_now",
        }
    )

    merged["pct_change"] = (merged["price_now"] - merged["price_prev"]) / merged["price_prev"]
    merged["direction"] = merged["pct_change"].apply(lambda x: "SHORTENING" if x < 0 else "DRIFTING")

    keep = [
        "date_curr", "course_curr", "off_time_curr", "race_name_curr", "runner",
        "price_prev", "price_now", "pct_change", "direction",
        "time_prev", "time_now",
    ]
    merged = merged[keep].rename(
        columns={
            "date_curr": "date",
            "course_curr": "course",
            "off_time_curr": "off_time",
            "race_name_curr": "race_name",
        }
    )

    return merged.sort_values("pct_change", ascending=True)


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

    # Main tabs
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
        "night_before_hour_local": NIGHT_BEFORE_HOUR,
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
        # self-heal and skip movers this run
        writer.write_df("MARKET_SNAPSHOTS", snapshots_new)
        writer.write_df("MARKET_MOVERS", pd.DataFrame())
        writer.write_df("MARKET_MOVERS_2H", pd.DataFrame())
        print("Update complete (snapshots healed; movers pending).")
        return 0

    snap_df = pd.DataFrame(snap_vals[1:], columns=header)
    snap_df = _prep_snapshots_df(snap_df)

    # Movers
    movers_night = compute_market_movers_night_before(snap_df)
    movers_live = compute_market_movers_live_last_two_snapshots(snap_df)

    writer.write_df("MARKET_MOVERS", movers_night)
    writer.write_df("MARKET_MOVERS_2H", movers_live)

    print("Update complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

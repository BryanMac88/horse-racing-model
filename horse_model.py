from __future__ import annotations

import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from src.sources.irishracing import IrishRacingClient
from src.scoring import build_runner_scores, build_value_bets
from src.sheets import SheetsWriter

TZ = ZoneInfo("Europe/Dublin")
NIGHT_BEFORE_HOUR = 22  # 22:00 Dublin time

# Filters / tuning
MIN_MOVE_PCT = 0.02          # ignore tiny moves under 2% (noise)
MAX_RUNNERS_FOR_SIGNAL = 18  # big fields produce lots of false shorteners


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
    hh = int(m[0])
    mm = int(m[1])
    return f"{hh:02d}:{mm:02d}"


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

    # normalize off_time for race_dt parsing (needed for night-before movers)
    if "off_time" in df.columns:
        df["off_time_norm"] = df["off_time"].astype(str).apply(_normalize_off_time)
    else:
        df["off_time_norm"] = ""

    df["race_dt"] = pd.NaT
    if "date" in df.columns and "off_time_norm" in df.columns:
        race_dt = pd.to_datetime(
            df["date"].astype(str) + " " + df["off_time_norm"].astype(str),
            errors="coerce",
        )
        race_dt = race_dt.dt.tz_localize(TZ, nonexistent="shift_forward", ambiguous="NaT")
        df["race_dt"] = race_dt

    return df


def compute_movers_last_two(df: pd.DataFrame) -> pd.DataFrame:
    """
    Immediate movers: uses the last TWO snapshots per runner.
    Robust output columns: date/course/off_time/race_name always from the latest snapshot.
    """
    if df is None or df.empty:
        return pd.DataFrame()

    d = df.dropna(subset=["snapshot_local", "best_price_dec", "race_id", "runner"]).copy()
    d = d.sort_values(["race_id", "runner", "snapshot_local"])

    key = ["race_id", "runner"]

    # last two snapshots per runner
    last_two = d.groupby(key).tail(2).copy()
    counts = last_two.groupby(key).size().reset_index(name="n")
    valid = counts[counts["n"] >= 2][key]
    if valid.empty:
        return pd.DataFrame()

    last_two = last_two.merge(valid, on=key, how="inner")
    last_two["rn"] = last_two.groupby(key).cumcount()  # 0 then 1

    prev = last_two[last_two["rn"] == 0].copy()
    curr = last_two[last_two["rn"] == 1].copy()

    # Merge ONLY current race metadata from curr to avoid suffix chaos
    curr_cols = key + ["best_price_dec", "snapshot_local"]
    for c in ["date", "course", "off_time", "race_name"]:
        if c in curr.columns:
            curr_cols.append(c)

    merged = prev[key + ["best_price_dec", "snapshot_local"]].merge(
        curr[curr_cols],
        on=key,
        how="inner",
        suffixes=("_prev", "_now"),
    )

    if merged.empty:
        return pd.DataFrame()

    merged = merged.rename(
        columns={
            "best_price_dec_prev": "price_prev",
            "best_price_dec": "price_now",
            "snapshot_local_prev": "time_prev",
            "snapshot_local": "time_now",
        }
    )

    merged["pct_change"] = (merged["price_now"] - merged["price_prev"]) / merged["price_prev"]
    merged["direction"] = merged["pct_change"].apply(lambda x: "SHORTENING" if x < 0 else "DRIFTING")

    # filter noise
    merged = merged[merged["pct_change"].abs() >= MIN_MOVE_PCT].copy()

    # Ensure readable columns exist (fill blanks if missing)
    for c in ["date", "course", "off_time", "race_name"]:
        if c not in merged.columns:
            merged[c] = ""

    keep = [
        "date", "course", "off_time", "race_name", "race_id", "runner",
        "price_prev", "price_now", "pct_change", "direction",
        "time_prev", "time_now",
    ]
    return merged[keep].sort_values("pct_change", ascending=True)


def compute_movers_night_before(df: pd.DataFrame) -> pd.DataFrame:
    """
    Official movers:
      - Start price = first snapshot after 22:00 previous day (Dublin time)
      - Pre-race price = last snapshot <= off_time - 30 minutes
    """
    if df is None or df.empty or "race_dt" not in df.columns:
        return pd.DataFrame()

    d = df.dropna(subset=["race_dt"]).copy()
    if d.empty:
        return pd.DataFrame()

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
        suffixes=("_start", "_30m"),
    )

    if merged.empty:
        return pd.DataFrame()

    merged = merged.rename(
        columns={
            "best_price_dec_start": "start_price_night_before",
            "best_price_dec_30m": "price_30min_before",
            "snapshot_local_start": "time_start",
            "snapshot_local_30m": "time_30min_before",
        }
    )

    merged["pct_change"] = (
        (merged["price_30min_before"] - merged["start_price_night_before"]) / merged["start_price_night_before"]
    )
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
    """
    Persistence: look at last 4 snapshots and count how many times price decreased.
    """
    if snap_df is None or snap_df.empty:
        return pd.DataFrame()

    d = snap_df.dropna(subset=["race_id", "runner", "snapshot_local", "best_price_dec"]).copy()
    d = d.sort_values(["race_id", "runner", "snapshot_local"])
    key = ["race_id", "runner"]

    last4 = d.groupby(key).tail(4).copy()
    last4["prev_price"] = last4.groupby(key)["best_price_dec"].shift(1)
    last4["down"] = (last4["best_price_dec"] < last4["prev_price"]).astype(int)

    pers = last4.groupby(key, as_index=False)["down"].sum().rename(columns={"down": "shorten_steps_last4"})
    return pers


def build_signals(scored: pd.DataFrame, movers_2h: pd.DataFrame, movers_night: pd.DataFrame, persistence: pd.DataFrame) -> pd.DataFrame:
    df = scored.copy()

    df["runner_count"] = pd.to_numeric(df.get("runner_count", 0), errors="coerce").fillna(0)
    df = df[df["runner_count"] <= MAX_RUNNERS_FOR_SIGNAL].copy()

    df["value_edge"] = pd.to_numeric(df.get("value_edge", 0), errors="coerce").fillna(0.0)

    m2 = movers_2h[["race_id", "runner", "pct_change"]].rename(columns={"pct_change": "mover_2h_pct"}) if not movers_2h.empty else pd.DataFrame(columns=["race_id", "runner", "mover_2h_pct"])
    mn = movers_night[["race_id", "runner", "pct_change"]].rename(columns={"pct_change": "mover_night_pct"}) if not movers_night.empty else pd.DataFrame(columns=["race_id", "runner", "mover_night_pct"])
    ps = persistence[["race_id", "runner", "shorten_steps_last4"]] if not persistence.empty else pd.DataFrame(columns=["race_id", "runner", "shorten_steps_last4"])

    for t in (m2, mn, ps):
        if not t.empty:
            t["race_id"] = t["race_id"].astype(str).str.strip()
            t["runner"] = t["runner"].astype(str).str.strip()

    df["race_id"] = df["race_id"].astype(str).str.strip()
    df["runner"] = df["runner"].astype(str).str.strip()

    out = df.merge(m2, on=["race_id", "runner"], how="left") \
            .merge(mn, on=["race_id", "runner"], how="left") \
            .merge(ps, on=["race_id", "runner"], how="left")

    out["mover_2h_pct"] = pd.to_numeric(out.get("mover_2h_pct", 0), errors="coerce").fillna(0.0)
    out["mover_night_pct"] = pd.to_numeric(out.get("mover_night_pct", 0), errors="coerce").fillna(0.0)
    out["shorten_steps_last4"] = pd.to_numeric(out.get("shorten_steps_last4", 0), errors="coerce").fillna(0).astype(int)

    out["shorten_2h_score"] = (-out["mover_2h_pct"]).clip(lower=0)
    out["shorten_night_score"] = (-out["mover_night_pct"]).clip(lower=0)

    out["signal_score"] = (
        1.0 * out["value_edge"].clip(lower=0) +
        0.8 * out["shorten_2h_score"] +
        1.2 * out["shorten_night_score"] +
        0.15 * out["shorten_steps_last4"]
    )

    out = out.sort_values(["date", "course", "off_time", "signal_score"], ascending=[True, True, True, False])

    cols = [
        "date", "course", "off_time", "race_name", "race_id",
        "runner", "best_price_dec",
        "model_prob", "market_prob", "value_edge",
        "mover_2h_pct", "mover_night_pct", "shorten_steps_last4",
        "signal_score", "confidence", "runner_count",
    ]
    cols = [c for c in cols if c in out.columns] + [c for c in out.columns if c not in cols]
    return out[cols]


def build_bets_to_place(signals: pd.DataFrame) -> pd.DataFrame:
    if signals is None or signals.empty:
        return pd.DataFrame()

    df = signals.copy()
    df["signal_score"] = pd.to_numeric(df.get("signal_score", 0), errors="coerce").fillna(0.0)
    df["value_edge"] = pd.to_numeric(df.get("value_edge", 0), errors="coerce").fillna(0.0)
    df["mover_2h_pct"] = pd.to_numeric(df.get("mover_2h_pct", 0), errors="coerce").fillna(0.0)
    df["mover_night_pct"] = pd.to_numeric(df.get("mover_night_pct", 0), errors="coerce").fillna(0.0)

    has_reason = (df["value_edge"] > 0) | (df["mover_2h_pct"] <= -MIN_MOVE_PCT) | (df["mover_night_pct"] <= -MIN_MOVE_PCT)
    df = df[has_reason].copy()
    if df.empty:
        return df

    df["rank_in_race"] = df.groupby("race_id")["signal_score"].rank(ascending=False, method="first")
    df = df[df["rank_in_race"] <= 2].copy()

    df["suggested_stake_units"] = 1
    df["bet_key"] = df["date"].astype(str) + "|" + df["course"].astype(str) + "|" + df["off_time"].astype(str) + "|" + df["runner"].astype(str)
    return df.sort_values(["date", "course", "off_time", "signal_score"], ascending=[True, True, True, False])


def update_bet_recs_log(writer: SheetsWriter, bets_to_place: pd.DataFrame) -> None:
    if bets_to_place is None or bets_to_place.empty or "bet_key" not in bets_to_place.columns:
        return

    existing = writer.read_df("BET_RECS_LOG")
    existing_keys = set()
    if not existing.empty and "bet_key" in existing.columns:
        existing_keys = set(existing["bet_key"].astype(str).tolist())

    new_rows = bets_to_place[~bets_to_place["bet_key"].astype(str).isin(existing_keys)].copy()
    if new_rows.empty:
        return

    out = pd.DataFrame()
    out["timestamp_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    out["bet_key"] = new_rows["bet_key"]
    for c in ["date", "course", "off_time", "race_name", "runner", "best_price_dec", "signal_score", "value_edge",
              "mover_2h_pct", "mover_night_pct", "shorten_steps_last4", "suggested_stake_units"]:
        out[c] = new_rows.get(c, "")

    out["result"] = ""
    out["pnl_units"] = ""
    out["notes"] = ""

    cols = ["timestamp_utc", "bet_key", "date", "course", "off_time", "race_name", "runner",
            "best_price_dec", "signal_score", "value_edge", "mover_2h_pct", "mover_night_pct",
            "shorten_steps_last4", "suggested_stake_units", "result", "pnl_units", "notes"]
    out = out[cols]
    writer.append_df("BET_RECS_LOG", out)


def build_dashboard(races: pd.DataFrame, runners: pd.DataFrame, movers2h: pd.DataFrame, moversnight: pd.DataFrame, bets: pd.DataFrame) -> pd.DataFrame:
    rows = []
    rows.append({"metric": "last_run_local", "value": datetime.now(timezone.utc).astimezone(TZ).isoformat(timespec="seconds")})
    rows.append({"metric": "races_today", "value": int(len(races)) if races is not None else 0})
    rows.append({"metric": "runners_today", "value": int(len(runners)) if runners is not None else 0})
    rows.append({"metric": "movers_2h_rows", "value": int(len(movers2h)) if movers2h is not None else 0})
    rows.append({"metric": "movers_night_rows", "value": int(len(moversnight)) if moversnight is not None else 0})
    rows.append({"metric": "bets_to_place", "value": int(len(bets)) if bets is not None else 0})
    return pd.DataFrame(rows)


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

    writer.write_df("TODAYS_RACES", races_df)
    writer.write_df("RUNNERS", scored)
    writer.write_df("VALUE_BETS", value_bets)
    writer.write_df("RUN_LOG", pd.DataFrame([{
        "run_utc": now_utc.isoformat(timespec="seconds"),
        "run_local": now_local.isoformat(timespec="seconds"),
        "region": region,
        "races": int(len(races_df)),
        "runners": int(len(scored)),
        "night_before_hour_local": NIGHT_BEFORE_HOUR,
        "min_move_pct": MIN_MOVE_PCT,
        "max_runners_for_signal": MAX_RUNNERS_FOR_SIGNAL,
    }]))

    # snapshots append
    snapshots_new = scored[[
        "date", "course", "off_time", "race_name", "race_id", "runner", "best_price_dec"
    ]].copy()
    snapshots_new.insert(0, "snapshot_time", now_utc.isoformat(timespec="seconds"))

    try:
        writer.append_df("MARKET_SNAPSHOTS", snapshots_new)
    except Exception:
        writer.write_df("MARKET_SNAPSHOTS", snapshots_new)

    snap_df_raw = writer.read_df("MARKET_SNAPSHOTS")
    if snap_df_raw.empty or "snapshot_time" not in snap_df_raw.columns:
        writer.write_df("MARKET_SNAPSHOTS", snapshots_new)
        writer.write_df("MARKET_MOVERS", pd.DataFrame())
        writer.write_df("MARKET_MOVERS_2H", pd.DataFrame())
        writer.write_df("SIGNALS", pd.DataFrame())
        writer.write_df("BETS_TO_PLACE", pd.DataFrame())
        writer.write_df("DASHBOARD", build_dashboard(races_df, scored, pd.DataFrame(), pd.DataFrame(), pd.DataFrame()))
        print("Update complete (snapshots healed; movers/signals pending).")
        return 0

    snap_df = _prep_snapshots_df(snap_df_raw)

    movers_2h = compute_movers_last_two(snap_df)
    movers_night = compute_movers_night_before(snap_df)
    persistence = compute_persistent_shorteners(snap_df)

    writer.write_df("MARKET_MOVERS_2H", movers_2h)
    writer.write_df("MARKET_MOVERS", movers_night)

    signals = build_signals(scored, movers_2h, movers_night, persistence)
    writer.write_df("SIGNALS", signals)

    bets_to_place = build_bets_to_place(signals)
    writer.write_df("BETS_TO_PLACE", bets_to_place)

    update_bet_recs_log(writer, bets_to_place)

    dashboard = build_dashboard(races_df, scored, movers_2h, movers_night, bets_to_place)
    writer.write_df("DASHBOARD", dashboard)

    print("Update complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

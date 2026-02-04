from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Tuple

import pandas as pd
import requests
from bs4 import BeautifulSoup

UA = "Mozilla/5.0 (compatible; HorseRacingSheetsBot/1.0)"
BASE = "https://www.irishracing.com"


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def _frac_to_decimal(frac: str) -> float | None:
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


def _today_label() -> str:
    # Example: Wed-4th-Feb-2026
    dt = datetime.now()
    day = dt.day
    suffix = "th"
    if 10 <= day % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{dt.strftime('%a')}-{day}{suffix}-{dt.strftime('%b')}-{dt.strftime('%Y')}"


@dataclass(frozen=True)
class Race:
    date: str
    date_label: str
    course: str
    off_time: str
    race_name: str
    distance: str
    going: str
    class_band: str
    race_url: str
    race_key: str  # unique id we create: f"{course}_{off_time}_{race_name}"


class IrishRacingClient:
    def __init__(self, region: str = "all", timeout: int = 20) -> None:
        self.region = region
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": UA})

    def fetch_today(self) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """
        Pull TODAY by date label page:
          /racecards/<DATE_LABEL>
        Then for each meeting:
          /racecards/<DATE_LABEL>/<COURSE>
        We parse runners + "Probable SP" from the course page.
        """
        date_label = _today_label()
        day_url = f"{BASE}/racecards/{date_label}"

        html = self.session.get(day_url, timeout=self.timeout).text
        soup = BeautifulSoup(html, "lxml")

        meeting_links = []
        for a in soup.select("a[href^='/racecards/']"):
            href = a.get("href", "")
            # meeting page pattern: /racecards/<date>/<course>
            if re.fullmatch(rf"/racecards/{re.escape(date_label)}/[^/]+", href):
                meeting_links.append(href)

        meeting_links = list(dict.fromkeys(meeting_links))

        races_all = []
        runners_all = []

        for href in meeting_links:
            course_url = BASE + href
            course = href.split("/")[-1]
            races_df, runners_df = self._parse_course_all_races(course_url, date_label, course)
            if not races_df.empty:
                races_all.append(races_df)
            if not runners_df.empty:
                runners_all.append(runners_df)

        races = pd.concat(races_all, ignore_index=True) if races_all else pd.DataFrame()
        runners = pd.concat(runners_all, ignore_index=True) if runners_all else pd.DataFrame()

        return races, runners

    def enrich_with_best_prices(self, runners_df: pd.DataFrame) -> pd.DataFrame:
        # Already populated from Probable SP on racecards
        df = runners_df.copy()
        df["best_price_dec"] = pd.to_numeric(df["best_price_dec"], errors="coerce")
        df = df.dropna(subset=["best_price_dec"]).copy()
        return df

    # -------------------------
    # Internal parsing
    # -------------------------
    def _parse_course_all_races(self, url: str, date_label: str, course: str) -> Tuple[pd.DataFrame, pd.DataFrame]:
        html = self.session.get(url, timeout=self.timeout).text
        soup = BeautifulSoup(html, "lxml")
        text = soup.get_text("\n", strip=True)

        # Going appears as "Going - <text>."
        going = ""
        m_going = re.search(r"Going\s*-\s*(.+?)\.", text)
        if m_going:
            going = _clean(m_going.group(1))

        date_iso = self._label_to_date(date_label)

        # Split by race time markers like "5.00", "12.28" etc.
        # We keep blocks that contain "Probable SP -"
        blocks = re.split(r"\n(?=\d{1,2}\.\d{2}\n)", text)

        races_rows = []
        runner_rows = []

        for block in blocks:
            m_time = re.match(r"(\d{1,2}\.\d{2})\n", block)
            if not m_time:
                continue
            off_time = m_time.group(1)

            # Race name: often appears immediately after time as a line with words
            # We take the first non-empty line after time that isn't "No" / "Form"
            lines = [ln.strip() for ln in block.split("\n") if ln.strip()]
            if len(lines) < 3:
                continue

            race_name = ""
            for ln in lines[1:8]:
                if ln.lower() in ("no", "form", "horse age weight", "trainer", "jockey", "or"):
                    continue
                if re.match(r"^\d+\^\{", ln):
                    continue
                if re.search(r"Probable SP", ln):
                    break
                race_name = ln
                break

            # Distance often like "6f." or "2m. 3f. 17yds."
            dist = ""
            m_dist = re.search(r"(\d+m\.\s*\d+f\.\s*\d+yds\.|\d+m\.\s*\d+f\.|\d+f\.)", block)
            if m_dist:
                dist = _clean(m_dist.group(1)).replace(" .", ".")

            class_band = ""
            m_class = re.search(r"\(Class\s*(\d+)\)", block)
            if m_class:
                class_band = f"Class {m_class.group(1)}"

            # Probable SP line
            m_psp = re.search(r"Probable SP\s*-\s*(.+)", block)
            if not m_psp:
                # no market line -> skip (usually means page section not a proper race)
                continue
            psp = m_psp.group(1)

            # Build a race key we can group on
            race_key = f"{course}_{off_time}_{re.sub(r'[^A-Za-z0-9]+','_',race_name)[:40]}"

            races_rows.append({
                "date": date_iso,
                "date_label": date_label,
                "course": course,
                "off_time": off_time,
                "race_name": race_name,
                "distance": dist,
                "going": going,
                "class_band": class_band,
                "race_url": url,
                "race_id": race_key,
            })

            # Parse Probable SP list: "7/4 Teardrops, 9/2 Laurens Dream, 5/1 Asadjumeirah, ..."
            # We'll map horse -> frac odds.
            horse_to_odds = {}
            for part in psp.split(","):
                part = part.strip()
                m = re.match(r"(\d+/\d+)\s+(.+)$", part)
                if not m:
                    continue
                frac = m.group(1).strip()
                name = _clean(m.group(2))
                # remove trailing "Others."
                name = re.sub(r"\.\s*$", "", name)
                horse_to_odds[name.lower()] = frac

            # Runner names appear in the block as standalone lines with trainer/jockey/OR nearby.
            # We'll take candidates that look like a name and exist in horse_to_odds (best effort).
            for ln in lines:
                # likely horse line contains letters and not too long
                if len(ln) < 2 or len(ln) > 60:
                    continue
                if ln.lower().startswith("probable sp"):
                    break
                if re.search(r"\b(Probable|Image:|Midnite|Handicap|Maiden|Novice|H'cap|Stakes)\b", ln, re.IGNORECASE):
                    continue

                # if this line matches a horse in the odds list, keep it
                key = ln.lower()
                if key in horse_to_odds:
                    frac = horse_to_odds[key]
                    dec = _frac_to_decimal(frac)
                    if not dec:
                        continue
                    runner_rows.append({
                        "date": date_iso,
                        "date_label": date_label,
                        "course": course,
                        "off_time": off_time,
                        "race_name": race_name,
                        "distance": dist,
                        "going": going,
                        "class_band": class_band,
                        "race_id": race_key,
                        "runner": ln,
                        "best_price_frac": frac,
                        "best_price_dec": float(dec),
                        "rating": "",
                        "days_since": 60,
                        "course_distance": "",
                        "trainer": "",
                        "jockey": "",
                        "weight": "",
                        "age": "",
                        "sex": "",
                        "draw": "",
                    })

        races_df = pd.DataFrame(races_rows)
        runners_df = pd.DataFrame(runner_rows).drop_duplicates(subset=["race_id", "runner"]) if runner_rows else pd.DataFrame()
        return races_df, runners_df

    def _label_to_date(self, label: str) -> str:
        # label like Wed-4th-Feb-2026
        try:
            parts = label.split("-")
            day = re.sub(r"\D", "", parts[1])
            mon = parts[2]
            year = parts[3]
            dt = datetime.strptime(f"{day} {mon} {year}", "%d %b %Y")
            return dt.date().isoformat()
        except Exception:
            return datetime.utcnow().date().isoformat()

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Tuple

import pandas as pd
import requests
from bs4 import BeautifulSoup


UA = "Mozilla/5.0 (compatible; HorseRacingSheetsBot/1.0; +https://github.com/)"

RACECARDS_URL = "https://www.irishracing.com/racecards"
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


@dataclass(frozen=True)
class RaceRef:
    date_label: str   # e.g. Wed-4th-Feb-2026
    course: str       # e.g. Newcastle
    race_id: str      # e.g. 1900
    off_time: str     # e.g. 7.00


class IrishRacingClient:
    def __init__(self, region: str = "all", timeout: int = 20) -> None:
        self.region = region
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": UA})

    def fetch_today(self) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """Returns (races_df, runners_df)."""
        html = self.session.get(RACECARDS_URL, timeout=self.timeout).text
        soup = BeautifulSoup(html, "lxml")

        # The racecards page contains many meetings; we only keep GB/IRE if requested.
        # We find meeting headers by looking for course names then collect following race links.
        links = soup.select("a[href*='/racecards/']")
        # These are individual race links like /racecards/Wed-4th-Feb-2026/Newcastle/1900
        race_hrefs = []
        for a in links:
            href = a.get("href", "")
            if re.search(r"^/racecards/[^/]+/[^/]+/\d+$", href):
                race_hrefs.append(href)

        race_hrefs = list(dict.fromkeys(race_hrefs))  # unique preserve order

        races: list[dict] = []
        runners: list[pd.DataFrame] = []

        for href in race_hrefs:
            try:
                rr = self._parse_race_ref(href)
            except Exception:
                continue

            # region filter is best-effort (site doesn't label region in the link)
            if self.region in ("gb", "ire"):
                # crude heuristic: Irish courses often include (IRE) in runners, but too late.
                # So we keep all here; region filter will be applied later on runners if needed.
                pass

            race_url = BASE + href
            race_df, runners_df = self._fetch_racecard(race_url, rr)
            if not race_df.empty:
                races.append(race_df.iloc[0].to_dict())
            if not runners_df.empty:
                runners.append(runners_df)

        races_df = pd.DataFrame(races)
        runners_df = pd.concat(runners, ignore_index=True) if runners else pd.DataFrame()

        # standardize date column
        if "date" in races_df.columns:
            races_df["date"] = pd.to_datetime(races_df["date"]).dt.date.astype(str)
        if "date" in runners_df.columns:
            runners_df["date"] = pd.to_datetime(runners_df["date"]).dt.date.astype(str)

        return races_df, runners_df

    def enrich_with_best_prices(self, runners_df: pd.DataFrame) -> pd.DataFrame:
        """Adds best_price_frac and best_price_dec per runner using odds comparison pages."""
        if runners_df.empty:
            return runners_df

        df = runners_df.copy()
        best_frac = []
        best_dec = []

        for _, r in df.iterrows():
            date_label = r["date_label"]
            course = r["course"]
            race_id = str(r["race_id"])
            runner = r["runner"]

            url = f"{BASE}/odds-comparison/{date_label}/{course}/{race_id}"
            frac = self._best_price_for_runner(url, runner)
            dec = _frac_to_decimal(frac) if frac else None
            best_frac.append(frac or "")
            best_dec.append(dec or "")

        df["best_price_frac"] = best_frac
        df["best_price_dec"] = best_dec

        # drop runners where no odds found (usually non-runners / missing market)
        df = df[df["best_price_dec"] != ""].copy()
        df["best_price_dec"] = df["best_price_dec"].astype(float)
        return df

    # -----------------------
    # Internals
    # -----------------------
    def _parse_race_ref(self, href: str) -> RaceRef:
        # /racecards/Wed-4th-Feb-2026/Newcastle/1900
        parts = href.strip("/").split("/")
        return RaceRef(date_label=parts[1], course=parts[2], race_id=parts[3], off_time="")

    def _fetch_racecard(self, url: str, rr: RaceRef) -> Tuple[pd.DataFrame, pd.DataFrame]:
        html = self.session.get(url, timeout=self.timeout).text
        soup = BeautifulSoup(html, "lxml")

        # meta headline block
        title = _clean(soup.select_one("h1").get_text(" ", strip=True)) if soup.select_one("h1") else ""
        # off time appears in breadcrumbs and in the race nav; we'll pull the first time-like token
        text = soup.get_text(" ", strip=True)
        m_time = re.search(r"\b(\d{1,2}\.\d{2})\b", text)
        off_time = m_time.group(1) if m_time else ""

        going = ""
        m_going = re.search(r"Going\s*-\s*([A-Za-z/\s\(\)]+?)\.", text)
        if m_going:
            going = _clean(m_going.group(1))

        # distance like '5f.' or '1m 5yds'
        distance = ""
        m_dist = re.search(r"(\d+m\s*\d+f\s*\d+yds|\d+m\s*\d+f|\d+m|\d+f\s*\d+yds|\d+f)\.", text)
        if m_dist:
            distance = _clean(m_dist.group(1))

        class_band = ""
        m_class = re.search(r"\(Class\s*(\d+)\)", text)
        if m_class:
            class_band = f"Class {m_class.group(1)}"

        # date from the URL label
        # 'Wed-4th-Feb-2026' -> 2026-02-04 best-effort
        date = self._label_to_date(rr.date_label)

        race_row = {
            "date": date,
            "date_label": rr.date_label,
            "course": rr.course,
            "race_id": rr.race_id,
            "off_time": off_time,
            "race_name": title.replace("#", "").strip(),
            "distance": distance,
            "going": going,
            "class_band": class_band,
            "race_url": url,
        }

        runners = self._parse_runners(soup, race_row)
        return pd.DataFrame([race_row]), runners

    def _label_to_date(self, label: str) -> str:
        # label like Wed-4th-Feb-2026
        try:
            parts = label.split("-")
            # parts: [Wed, 4th, Feb, 2026]
            day = re.sub(r"\D", "", parts[1])
            mon = parts[2]
            year = parts[3]
            dt = datetime.strptime(f"{day} {mon} {year}", "%d %b %Y")
            return dt.date().isoformat()
        except Exception:
            return datetime.utcnow().date().isoformat()

    def _parse_runners(self, soup: BeautifulSoup, race_row: dict) -> pd.DataFrame:
        """Best-effort runner parsing from racecard pages."""
        # Runner names appear as links to /horse/ or /runner/ profiles; in the extracted HTML they look like:
        # <a ...>Arnhem (IRE)</a> 10,b g 9-9 ...
        runners = []
        for a in soup.select("a[href^='/horse/'], a[href^='/runners/'], a[href^='/horse-racing/']"):
            name = _clean(a.get_text(" ", strip=True))
            # Filter out navigation items
            if not name or len(name) < 2:
                continue
            if name.lower() in ("view all races", "view all cards", "view card"):
                continue
            # We only keep names that look like horses (often include (IRE)/(GB) but not required)
            if re.search(r"\bhandicap\b|\bmaiden\b|\bnovice\b", name.lower()):
                continue
            # Deduplicate later
            runners.append(name)

        runners = list(dict.fromkeys(runners))

        # Narrow: runner blocks usually have a pattern "Rated XX" near them.
        page_text = soup.get_text("\n", strip=True)

        rows = []
        for runner in runners:
            # find a chunk around the runner name
            idx = page_text.find(runner)
            if idx == -1:
                continue
            chunk = page_text[idx: idx + 800]

            # rating
            rating = None
            m = re.search(r"Rated\s*(\d+)", chunk)
            if m:
                rating = int(m.group(1))

            # weight pattern like 9-7 or 11-02
            weight = ""
            m = re.search(r"\b(\d{1,2}-\d{1,2})\b", chunk)
            if m:
                weight = m.group(1)

            # age/sex pattern like '6,b g' or '10,b g'
            age = ""
            sex = ""
            m = re.search(r"\b(\d{1,2})\s*,\s*([a-z])\s*([a-z])\b", chunk, re.IGNORECASE)
            if m:
                age = m.group(1)
                sex = f"{m.group(2)} {m.group(3)}"

            # trainer / jockey appear as separate links; best-effort: find first two after runner in HTML structure
            trainer = ""
            jockey = ""
            # look for anchor tags following the runner anchor
            runner_anchor = soup.find("a", string=re.compile(re.escape(runner)))
            if runner_anchor:
                # collect next few anchors text
                next_as = []
                for nxt in runner_anchor.find_all_next("a", limit=15):
                    t = _clean(nxt.get_text(" ", strip=True))
                    if t and t != runner and len(t) <= 40:
                        next_as.append(t)
                # heuristic: trainer + jockey are often consecutive and look like names with spaces
                cand = [t for t in next_as if re.search(r"[A-Za-z]\s+[A-Za-z]", t)]
                if len(cand) >= 2:
                    trainer, jockey = cand[0], cand[1]

            # course/distance marker: cd, c, d
            course_distance = ""
            m = re.search(r"\b(cd\^\{\d+\}|cd\b|c\b|d\b)", chunk, re.IGNORECASE)
            if m:
                course_distance = m.group(1).lower()

            # days since last run
            days_since = None
            m = re.search(r"(\d{1,2})(st|nd|rd|th)\s+[A-Za-z]{3}\s+\d{2}", chunk)
            if m:
                # can't compute without full date reliably; keep blank
                days_since = ""

            row = {
                **race_row,
                "off_time": race_row.get("off_time", ""),
                "runner": runner,
                "rating": rating if rating is not None else "",
                "weight": weight,
                "age": age,
                "sex": sex,
                "trainer": trainer,
                "jockey": jockey,
                "course_distance": course_distance,
                "days_since": days_since if days_since is not None else 60,  # default
                "draw": "",
            }
            rows.append(row)

        df = pd.DataFrame(rows)
        # remove obvious non-runner duplicates / tiny lists
        if not df.empty:
            df = df.drop_duplicates(subset=["race_id", "runner"])
        return df

    def _best_price_for_runner(self, odds_url: str, runner_name: str) -> str | None:
        """Extract a single (best-ish) fractional price for a runner from odds-comparison page."""
        html = self.session.get(odds_url, timeout=self.timeout).text
        soup = BeautifulSoup(html, "lxml")
        text = soup.get_text("\n", strip=True)

        # Find the runner section then the first fractional odds after it.
        # Example patterns seen on the page:
        # 'Monks Dream 5 7/2 Rating: 78 ...'
        idx = text.lower().find(runner_name.lower())
        if idx == -1:
            return None
        chunk = text[idx: idx + 400]
        m = re.search(r"\b(\d+\s*/\s*\d+)\b", chunk)
        if m:
            return m.group(1).replace(" ", "")
        return None

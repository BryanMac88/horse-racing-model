from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional, Tuple, Union, Set

import pandas as pd
import requests
from bs4 import BeautifulSoup

UA = "Mozilla/5.0 (compatible; HorseRacingSheetsBot/1.2)"
BASE = "https://www.irishracing.com"

IRE_COURSES: Set[str] = {
    "curragh", "leopardstown", "fairyhouse", "punchestown", "navan", "cork",
    "galway", "killarney", "listowel", "tipperary", "dundalk", "gowran-park",
    "gowran", "naas", "roscommon", "sligo", "down-royal", "downroyal",
    "downpatrick", "clonmel", "thurles", "limerick", "ballinrobe", "tramore",
    "wexford", "kilbeggan", "bellewstown", "laytown",
}

OVERSEAS_COURSES: Set[str] = {
    "tokyo", "sha-tin", "kochi", "kanazawa", "ohi", "hanshin", "kyoto",
    "nakayama", "chongqing", "seoul", "busan", "singapore", "kembla-grange",
}


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


def _suffix(day: int) -> str:
    if 10 <= day % 100 <= 20:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")


def _date_label(d: date) -> str:
    day = d.day
    return f"{d.strftime('%a')}-{day}{_suffix(day)}-{d.strftime('%b')}-{d.strftime('%Y')}"


def _parse_date_input(d: Union[str, date]) -> date:
    if isinstance(d, date):
        return d
    return datetime.strptime(str(d), "%Y-%m-%d").date()


def _norm_course(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")


def _parse_form_date(s: str) -> Optional[date]:
    s = (s or "").strip()
    for fmt in ("%d%b%y", "%d%b%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except Exception:
            continue
    return None


def _pos_score(pos: Optional[int], field: Optional[int]) -> float:
    if pos is None or pos <= 0:
        return 0.0
    if field and field > 1:
        return max(0.0, 1.0 - (pos - 1) / max(field - 1, 1))
    return max(0.0, 1.0 - (pos - 1) / 12.0)


@dataclass(frozen=True)
class FormRun:
    run_date: Optional[date]
    course_code: str
    distance: str
    going: str
    position: Optional[int]
    field_size: Optional[int]
    sp_frac: str
    raw: str


class IrishRacingClient:
    def __init__(self, region: str = "all", timeout: int = 25) -> None:
        self.region = (region or "all").lower().strip()
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": UA})

    def fetch_today(self) -> Tuple[pd.DataFrame, pd.DataFrame]:
        return self.fetch_for_date(datetime.now().date())

    def fetch_tomorrow(self) -> Tuple[pd.DataFrame, pd.DataFrame]:
        return self.fetch_for_date(datetime.now().date() + timedelta(days=1))

    def fetch_for_date(self, d: Union[str, date]) -> Tuple[pd.DataFrame, pd.DataFrame]:
        target_date = _parse_date_input(d)
        date_label = _date_label(target_date)
        day_url = f"{BASE}/racecards/{date_label}"

        try:
            html = self.session.get(day_url, timeout=self.timeout).text
        except Exception as e:
            print(f"Failed to fetch day page {day_url}: {e}")
            return pd.DataFrame(), pd.DataFrame()

        soup = BeautifulSoup(html, "lxml")
        meeting_links = []
        for a in soup.select("a[href^='/racecards/']"):
            href = a.get("href", "")
            if re.fullmatch(rf"/racecards/{re.escape(date_label)}/[^/]+", href or ""):
                meeting_links.append(href)
        meeting_links = list(dict.fromkeys(meeting_links))

        races_all, runners_all = [], []
        for href in meeting_links:
            course = href.split("/")[-1]
            course_norm = _norm_course(course)

            if self.region == "ire":
                if course_norm not in IRE_COURSES:
                    continue
            elif self.region == "gb":
                if course_norm in IRE_COURSES or course_norm in OVERSEAS_COURSES:
                    continue
            else:
                if course_norm in OVERSEAS_COURSES:
                    continue

            course_url = BASE + href
            try:
                races_df, runners_df = self._parse_course_all_races(course_url, date_label, course)
            except Exception as e:
                print(f"Failed parsing {course_url}: {e}")
                continue

            if not races_df.empty:
                races_all.append(races_df)
            if not runners_df.empty:
                runners_all.append(runners_df)

        races = pd.concat(races_all, ignore_index=True) if races_all else pd.DataFrame()
        runners = pd.concat(runners_all, ignore_index=True) if runners_all else pd.DataFrame()
        return races, runners

    def enrich_with_best_prices(self, runners_df: pd.DataFrame) -> pd.DataFrame:
        df = runners_df.copy()
        df["best_price_dec"] = pd.to_numeric(df["best_price_dec"], errors="coerce")
        df = df.dropna(subset=["best_price_dec"]).copy()
        return df

    def enrich_with_form(
        self,
        runners_df: pd.DataFrame,
        max_horses: int = 80,
        max_workers: int = 8,
        max_runs: int = 8,
    ) -> pd.DataFrame:
        if runners_df is None or runners_df.empty:
            return runners_df

        df = runners_df.copy()
        if "horse_url" not in df.columns:
            df["horse_url"] = ""

        work = df[df["horse_url"].astype(str).str.len() > 5].copy()
        if work.empty:
            print("No horse_url values – form enrichment skipped.")
            return self._attach_empty_form_features(df)

        work["best_price_dec"] = pd.to_numeric(work["best_price_dec"], errors="coerce")
        work = work.sort_values("best_price_dec", ascending=True)
        unique = work.drop_duplicates(subset=["horse_url"]).head(max_horses)
        urls = unique["horse_url"].tolist()
        print(f"Fetching form for {len(urls)} horses (max_workers={max_workers})...")

        form_by_url: Dict[str, dict] = {}
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futs = {ex.submit(self._fetch_horse_form_features, u, max_runs): u for u in urls}
            for fut in as_completed(futs):
                u = futs[fut]
                try:
                    form_by_url[u] = fut.result()
                except Exception as e:
                    form_by_url[u] = self._empty_form_features()
                    print(f"  form fail {u}: {e}")

        feature_cols = list(self._empty_form_features().keys())
        for col in feature_cols:
            default = self._empty_form_features()[col]
            df[col] = df["horse_url"].map(
                lambda u, c=col, d=default: form_by_url.get(u, {}).get(c, d)
            )

        n_ok = sum(1 for v in form_by_url.values() if v.get("form_runs", 0) > 0)
        print(f"Form enrichment done: {n_ok}/{len(urls)} horses with runs.")
        return df

    def _parse_course_all_races(
        self, url: str, date_label: str, course: str
    ) -> Tuple[pd.DataFrame, pd.DataFrame]:
        html = self.session.get(url, timeout=self.timeout).text
        soup = BeautifulSoup(html, "lxml")
        text = soup.get_text("\n", strip=True)

        horse_url_map: Dict[str, str] = {}
        for a in soup.select("a[href*='/horse/']"):
            href = a.get("href", "")
            name = _clean(a.get_text(" ", strip=True)).lower()
            if name and href:
                if href.startswith("/"):
                    href = BASE + href
                horse_url_map[name] = href

        going = ""
        m_going = re.search(r"Going\s*-\s*(.+?)\.", text)
        if m_going:
            going = _clean(m_going.group(1))

        date_iso = self._label_to_date(date_label)
        blocks = re.split(r"\n(?=\d{1,2}\.\d{2}\n)", text)

        races_rows, runner_rows = [], []

        for block in blocks:
            m_time = re.match(r"(\d{1,2}\.\d{2})\n", block)
            if not m_time:
                continue
            off_time = m_time.group(1)
            lines = [ln.strip() for ln in block.split("\n") if ln.strip()]
            if len(lines) < 3:
                continue

            race_name = ""
            for ln in lines[1:10]:
                if ln.lower() in (
                    "no", "form", "horse age weight", "trainer", "jockey", "or", "horse"
                ):
                    continue
                if re.search(r"Probable SP", ln, re.I):
                    break
                if re.search(r"\b(of €|Race Conditions|Weights|Penalties)\b", ln, re.I):
                    continue
                race_name = ln
                break

            dist = ""
            m_dist = re.search(
                r"(\d+m\.\s*\d+f\.\s*\d+yds\.|\d+m\.\s*\d+f\.|\d+f\.)", block
            )
            if m_dist:
                dist = _clean(m_dist.group(1)).replace(" .", ".")

            class_band = ""
            m_class = re.search(r"\(Class\s*(\d+)\)", block)
            if m_class:
                class_band = f"Class {m_class.group(1)}"

            m_psp = re.search(r"Probable SP\s*-\s*(.+)", block)
            if not m_psp:
                continue
            psp = m_psp.group(1)

            race_key = (
                f"{course}_{off_time}_{re.sub(r'[^A-Za-z0-9]+', '_', race_name)[:40]}"
            )

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

            horse_to_odds: Dict[str, str] = {}
            for part in psp.split(","):
                part = part.strip().rstrip(".")
                m = re.match(r"(\d+/\d+)\s+(.+)$", part)
                if not m:
                    continue
                frac = m.group(1).strip()
                name = _clean(m.group(2))
                name = re.sub(r"\.\s*$", "", name)
                horse_to_odds[name.lower()] = frac

            for ln in lines:
                if len(ln) < 2 or len(ln) > 60:
                    continue
                if ln.lower().startswith("probable sp"):
                    break
                if re.search(
                    r"\b(Probable|Image:|Midnite|Handicap|Maiden|Novice|H'cap|Stakes|Conditions|Weights|Penalties)\b",
                    ln,
                    re.I,
                ):
                    continue

                key = ln.lower()
                if key not in horse_to_odds:
                    continue
                frac = horse_to_odds[key]
                dec = _frac_to_decimal(frac)
                if not dec:
                    continue

                horse_url = horse_url_map.get(key, "")
                if not horse_url:
                    for n, u in horse_url_map.items():
                        if n in key or key in n:
                            horse_url = u
                            break

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
                    "horse_url": horse_url,
                    "best_price_frac": frac,
                    "best_price_dec": float(dec),
                    "rating": "",
                    "days_since": 60,
                    "course_distance": "",
                    "trainer": "",
                    "jockey": "",

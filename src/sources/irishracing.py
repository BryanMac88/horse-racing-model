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
                    "weight": "",
                    "age": "",
                    "sex": "",
                    "draw": "",
                })

        races_df = pd.DataFrame(races_rows)
        runners_df = (
            pd.DataFrame(runner_rows).drop_duplicates(subset=["race_id", "runner"])
            if runner_rows
            else pd.DataFrame()
        )
        return races_df, runners_df

    def _fetch_horse_form_features(self, horse_url: str, max_runs: int = 8) -> dict:
        try:
            html = self.session.get(horse_url, timeout=self.timeout).text
        except Exception:
            return self._empty_form_features()
        soup = BeautifulSoup(html, "lxml")
        text = soup.get_text("\n", strip=True)
        runs = self._parse_form_runs(text, max_runs=max_runs)
        return self._features_from_runs(runs)

    def _parse_form_runs(self, text: str, max_runs: int = 8) -> List[FormRun]:
        runs: List[FormRun] = []
        start = text.find("Form Timeline")
        if start < 0:
            start = text.find("Form Figures")
        chunk = text[start : start + 8000] if start >= 0 else text

        date_line_re = re.compile(
            r"(?m)^(\d{1,2}[A-Za-z]{3}\d{2})\s+([A-Za-z]{2,6})\s+(\d+f\b[^\n]*)$"
        )
        matches = list(date_line_re.finditer(chunk))
        for i, m in enumerate(matches[:max_runs]):
            run_date = _parse_form_date(m.group(1))
            course_code = m.group(2).strip()
            rest = m.group(3).strip()

            dist_m = re.match(r"(\d+f(?:\s*\d+y)?)\s+(.+)", rest)
            distance = dist_m.group(1) if dist_m else ""
            going_raw = dist_m.group(2) if dist_m else rest
            going = re.split(
                r"\d+y|\(|NHF|Hcap|H'cap|Mdn|Maiden|Novice|Stakes|Chase|Hurdle",
                going_raw,
            )[0]
            going = _clean(going)

            end = (
                matches[i + 1].start()
                if i + 1 < len(matches)
                else min(len(chunk), m.end() + 400)
            )
            block = chunk[m.start() : end]

            position, field_size = None, None
            pm = re.search(r"(\d+)(?:st|nd|rd|th)/(\d+)", block, re.I)
            if pm:
                position = int(pm.group(1))
                field_size = int(pm.group(2))
            elif re.search(r"\b(pulled up|pu)\b", block, re.I):
                position = 99
            elif re.search(
                r"\b(fell|unseated|ur|brought down|bd|refused)\b", block, re.I
            ):
                position = 99

            sp_frac = ""
            sm = re.search(r"(\d+/\d+)(?:Fav|JFav)?", block)
            if sm:
                sp_frac = sm.group(1)

            runs.append(
                FormRun(
                    run_date=run_date,
                    course_code=course_code,
                    distance=distance,
                    going=going,
                    position=position,
                    field_size=field_size,
                    sp_frac=sp_frac,
                    raw=block[:200],
                )
            )
        return runs

    def _features_from_runs(self, runs: List[FormRun]) -> dict:
        feats = self._empty_form_features()
        if not runs:
            return feats

        today = datetime.utcnow().date()
        feats["form_runs"] = len(runs)

        last = runs[0]
        if last.run_date:
            feats["days_since"] = max(0, (today - last.run_date).days)

        recent = runs[:5]
        scores = [_pos_score(r.position, r.field_size) for r in recent]
        if scores:
            weights = [1.0, 0.85, 0.7, 0.55, 0.4][: len(scores)]
            wsum = sum(weights)
            feats["recent_form_score"] = (
                sum(s * w for s, w in zip(scores, weights)) / wsum
            )

        def is_win(r: FormRun) -> bool:
            return r.position == 1

        def is_place(r: FormRun) -> bool:
            if r.position is None:
                return False
            if r.field_size and r.field_size >= 8:
                return r.position <= 3
            return r.position <= 2

        feats["wins_last5"] = sum(1 for r in recent if is_win(r))
        feats["places_last5"] = sum(1 for r in recent if is_place(r))

        soft_keys = ("soft", "heavy", "yielding", "slow")
        good_keys = ("good", "firm", "standard", "fast")
        soft_runs = [r for r in runs if any(k in r.going.lower() for k in soft_keys)]
        good_runs = [r for r in runs if any(k in r.going.lower() for k in good_keys)]
        if soft_runs:
            feats["soft_win_rate"] = sum(1 for r in soft_runs if is_win(r)) / len(
                soft_runs
            )
            feats["soft_place_rate"] = sum(1 for r in soft_runs if is_place(r)) / len(
                soft_runs
            )
        if good_runs:
            feats["good_win_rate"] = sum(1 for r in good_runs if is_win(r)) / len(
                good_runs
            )
            feats["good_place_rate"] = sum(1 for r in good_runs if is_place(r)) / len(
                good_runs
            )

        feats["course_runs"] = len(runs)
        feats["course_wins"] = sum(1 for r in runs if is_win(r))

        last3 = [r for r in runs[:3] if r.position and r.position < 90]
        if last3:
            feats["avg_pos_last3"] = sum(r.position for r in last3) / len(last3)

        parts = []
        for r in runs[:6]:
            if r.position is None:
                parts.append("x")
            elif r.position >= 90:
                parts.append("P")
            elif r.position >= 10:
                parts.append("0")
            else:
                parts.append(str(r.position))
        feats["form_string"] = "".join(parts)
        return feats

    @staticmethod
    def _empty_form_features() -> dict:
        return {
            "form_runs": 0,
            "days_since": 60,
            "recent_form_score": 0.0,
            "wins_last5": 0,
            "places_last5": 0,
            "soft_win_rate": 0.0,
            "soft_place_rate": 0.0,
            "good_win_rate": 0.0,
            "good_place_rate": 0.0,
            "course_runs": 0,
            "course_wins": 0,
            "avg_pos_last3": 10.0,
            "form_string": "",
        }

    def _attach_empty_form_features(self, df: pd.DataFrame) -> pd.DataFrame:
        empty = self._empty_form_features()
        out = df.copy()
        for k, v in empty.items():
            out[k] = v
        return out

    def _label_to_date(self, label: str) -> str:
        try:
            parts = label.split("-")
            day = re.sub(r"\D", "", parts[1])
            mon = parts[2]
            year = parts[3]
            dt = datetime.strptime(f"{day} {mon} {year}", "%d %b %Y")
            return dt.date().isoformat()
        except Exception:
            return datetime.utcnow().date().isoformat()

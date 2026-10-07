#!/usr/bin/env python3
"""
Horse racing Google Sheet job (UK + Ireland), run on a schedule by GitHub Actions.
Source: irishracing.com (Probable SP before the race, official SP + finishing order after).

What it does each run
  1. Settles any PENDING bets in BET_TRACKER using results pages (position + SP).
  2. Scrapes ALL UK/IRE meetings for the target day (today, or tomorrow after 22:00 Dublin).
  3. Logs a price snapshot, then works out market movers against earlier snapshots.
  4. Scores runners, picks BETS_TO_PLACE across all meetings, logs them in BET_TRACKER.
  5. Picks the single best selection in EVERY race (BEST_PER_RACE), re-picking until the off,
     then tracks and settles those too (RACE_BEST_TRACKER / RACE_BEST_SUMMARY).
  6. Rewrites the summary tabs, including TRACKER_SUMMARY (win % / place % / ROI).

Environment variables
  GOOGLE_CREDS    full service-account JSON (or put credentials.json next to this file)
  SHEET_NAME      exact spreadsheet title, default "Horse Racing Model"
  REGION          all | ire | gb          (default all)
  MIN_VALUE_EDGE  default 0.02
  MAX_BETS        max bets in BETS_TO_PLACE, default 12
  MAX_PER_MEETING max bets from one meeting, default 3
  DEBUG_DUMP      set to 1 to save fetched pages into ./debug (uploaded by the workflow)
"""
import datetime as dt
import json
import os
import re
import sys
import time
from collections import defaultdict
from urllib.parse import unquote, urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
TZ = ZoneInfo("Europe/Dublin")
BASE = "https://www.irishracing.com"
SHEET_NAME = os.environ.get("SHEET_NAME") or "Horse Racing Model"
REGION = (os.environ.get("REGION") or "all").strip().lower()
MIN_EDGE = float(os.environ.get("MIN_VALUE_EDGE") or 0.02)
MAX_BETS = int(os.environ.get("MAX_BETS") or 12)
MAX_PER_MEETING = int(os.environ.get("MAX_PER_MEETING") or 3)
DEBUG = os.environ.get("DEBUG_DUMP") == "1"

MIN_DEC, MAX_DEC = 1.8, 13.0       # price window for selections (decimal odds)
MOVER_MIN_AGE_MIN = 20             # a snapshot must be at least this old to be a reference
MOVER_SHOW_PCT = 5.0               # show movers of at least this size
REQUEST_PAUSE = 0.4                # seconds between page requests

DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MON3 = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

IRISH = {
    "ballinrobe", "bellewstown", "clonmel", "cork", "curragh", "downroyal", "downpatrick",
    "dundalk", "fairyhouse", "galway", "gowranpark", "kilbeggan", "killarney", "laytown",
    "leopardstown", "limerick", "listowel", "naas", "navan", "punchestown", "roscommon",
    "sligo", "thurles", "tipperary", "tramore", "wexford", "newbridge", "kenmare",
}
GB = {
    "aintree", "ascot", "ayr", "bangor", "bath", "beverley", "brighton", "carlisle", "cartmel",
    "catterick", "chelmsford", "cheltenham", "chepstow", "chester", "doncaster", "epsom",
    "exeter", "fakenham", "ffoslas", "fontwell", "goodwood", "hamilton", "haydock", "hereford",
    "hexham", "huntingdon", "kelso", "kempton", "leicester", "lingfield", "ludlow",
    "marketrasen", "musselburgh", "newbury", "newcastle", "newmarket", "newtonabbot",
    "nottingham", "perth", "plumpton", "pontefract", "redcar", "ripon", "salisbury", "sandown",
    "sedgefield", "southwell", "stratford", "taunton", "thirsk", "towcester", "uttoxeter",
    "warwick", "wetherby", "wincanton", "windsor", "wolverhampton", "worcester", "yarmouth",
    "york", "greatyarmouth",
}


def log(*a):
    print(*a, flush=True)


# ----------------------------------------------------------------------------
# Small helpers (pure functions, easy to test)
# ----------------------------------------------------------------------------
def norm(s):
    """Lowercase letters/digits only, country tags like (IRE) removed."""
    s = re.sub(r"\((?:[A-Za-z]{2,3})\)", "", s or "")
    return re.sub(r"[^a-z0-9]", "", s.lower())


def clean(t):
    return " ".join((t or "").split())


def display_name(t):
    return re.sub(r"\s*\([A-Za-z]{2,3}\)\s*$", "", clean(t)).strip()


def ordinal(n):
    if 10 <= n % 100 <= 20:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


def fmt_date(d):
    """irishracing.com date slug, e.g. Wed-7th-Oct-2026."""
    return f"{DOW[d.weekday()]}-{d.day}{ordinal(d.day)}-{MON3[d.month - 1]}-{d.year}"


def region_of(course):
    n = norm(course)
    if n in IRISH:
        return "ire"
    if any(n == g or n.startswith(g) or g.startswith(n) for g in GB):
        return "gb"
    return "other"


def region_ok(region):
    if region == "other":
        return False
    return REGION in ("all", "") or REGION == region


FRAC_RE = re.compile(r"(?<![\d/])(\d{1,3})\s*/\s*(\d{1,3})(?![\d/])")
EVENS_RE = re.compile(r"\b(?:evens|evs)\b", re.I)


def parse_odds(text):
    """Return (odds_text, decimal) from the first fractional price found, else (None, None)."""
    text = text or ""
    m = FRAC_RE.search(text)
    e = EVENS_RE.search(text)
    if e and (not m or e.start() < m.start()):
        return "evens", 2.0
    if m:
        n, d = int(m.group(1)), int(m.group(2))
        if d == 0:
            return None, None
        return f"{n}/{d}", round(n / d + 1, 3)
    return None, None


def place_terms(field, handicap):
    """(places paid, fraction of win odds) using standard UK/IRE each-way terms."""
    if field <= 4:
        return 1, 1.0            # no place market, treat as winner only
    if field <= 7:
        return 2, 0.25
    if handicap and field >= 16:
        return 4, 0.25
    if handicap:
        return 3, 0.25
    return 3, 0.2


def to_float(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


# ----------------------------------------------------------------------------
# HTTP + parsing
# ----------------------------------------------------------------------------
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept-Language": "en-GB,en;q=0.9",
})


def dump(url, text):
    if not DEBUG:
        return
    os.makedirs("debug", exist_ok=True)
    fn = re.sub(r"[^A-Za-z0-9]+", "_", url)[-150:] + ".html"
    with open(os.path.join("debug", fn), "w", encoding="utf-8") as f:
        f.write(text[:400000])


def get(url, tries=3):
    for i in range(tries):
        try:
            r = SESSION.get(url, timeout=30)
            if r.status_code == 200:
                dump(url, r.text)
                time.sleep(REQUEST_PAUSE)
                return r.text
            log(f"  HTTP {r.status_code} for {url}")
            if r.status_code in (404, 410):
                return None
        except requests.RequestException as e:
            log(f"  request error for {url}: {e}")
        time.sleep(1.5 * (i + 1))
    return None


LINK_RE = re.compile(
    r"/(?:racecards|raceresults)/([A-Za-z]{3}-\d{1,2}(?:st|nd|rd|th)-[A-Za-z]{3}-\d{4})"
    r"/([^/\s?#\"']+)/(\d{4})", re.I)
HORSE_HREF = re.compile(r"/horse/", re.I)
SP_LABEL = re.compile(r"Probable\s*SP", re.I)
PRICE_PIECE = re.compile(r"^(\d{1,3}\s*/\s*\d{1,3}|evens|evs)\s+(.+)$", re.I)
ORD_RE = re.compile(r"^\s*(\d{1,2})(?:st|nd|rd|th)\b")
STATUS_RE = re.compile(r"^\s*(PU|UR|BD|F|RO|SU|DSQ|REF|CO|LFT|DNF|VOID)\b")
SP_RE = re.compile(r"\bSP\s+(\d{1,3}\s*/\s*\d{1,3}|evens|evs)", re.I)
HCAP_RE = re.compile(r"h['\u2019]?cap|handicap", re.I)


def discover(index_urls, day):
    """Find every race link for `day` on the given index pages -> {(course, hhmm): url}."""
    want = fmt_date(day).lower()
    found = {}
    for url in index_urls:
        html = get(url)
        if not html:
            continue
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            m = LINK_RE.search(a["href"])
            if not m or m.group(1).lower() != want:
                continue
            course = clean(unquote(m.group(2)).replace("-", " ").replace("_", " ")).title()
            found.setdefault((course, m.group(3)), urljoin(BASE, a["href"]))
    return found


def row_for(a):
    """Climb from a horse link to the largest ancestor that contains only that one horse."""
    row, node = None, a
    for _ in range(10):
        node = node.parent
        if node is None:
            break
        names = {clean(h.get_text()) for h in node.find_all("a", href=HORSE_HREF)}
        names.discard("")
        if len(names) > 1:
            break
        row = node
    return row


def horse_rows(soup):
    """[(name, norm, row_text)] for each distinct horse link, in page order."""
    for root in (soup.find("main"), soup):
        if root is None:
            continue
        out, seen = [], set()
        for a in root.find_all("a", href=HORSE_HREF):
            name = display_name(a.get_text(" ", strip=True))
            n = norm(name)
            if len(name) < 2 or n in seen:
                continue
            seen.add(n)
            row = row_for(a)
            out.append((name, n, clean(row.get_text(" ", strip=True)) if row else ""))
        if out:
            return out
    return []


def parse_probable_sp(soup):
    """'Probable SP 11/4 Silver Trumpet, 3/1 Caragio, 8/1 Lady Manzor, Timely Affair' -> {norm: odds}."""
    node = soup.find(string=SP_LABEL)
    if node is None:
        return {}
    el, after = node.parent, ""
    for _ in range(4):
        text = clean(el.get_text(" ", strip=True))
        parts = SP_LABEL.split(text, 1)
        after = parts[1] if len(parts) > 1 else ""
        if FRAC_RE.search(after) or EVENS_RE.search(after):
            break
        if el.parent is None:
            break
        el = el.parent
    after = re.split(r"Previous Years|Symbols Explained|First Time", after)[0].strip().rstrip(".")
    prices, cur = {}, None
    for piece in after.split(","):
        piece = clean(piece).strip(" .")
        if not piece:
            continue
        m = PRICE_PIECE.match(piece)
        if m:
            cur, name = m.group(1), m.group(2)
        else:
            name = piece
        if cur is not None:
            prices[norm(name)] = cur
    return prices


def parse_racecard(html):
    """Runners in card order, priced from the page's Probable SP line."""
    soup = BeautifulSoup(html, "html.parser")
    prices = parse_probable_sp(soup)
    entries = []
    for name, n, _ in horse_rows(soup):
        odds, dec = parse_odds(prices.get(n, ""))
        entries.append({"name": name, "norm": n, "odds": odds, "dec": dec,
                        "nr": False, "pos": None, "status": False})
    return entries


def parse_result(html):
    """Finishers with position and official SP. Returns (entries, is_handicap)."""
    soup = BeautifulSoup(html, "html.parser")
    entries = []
    for name, n, text in horse_rows(soup):
        m = ORD_RE.match(text)
        pos = int(m.group(1)) if m else (99 if STATUS_RE.match(text) else None)
        sp = SP_RE.search(text)
        odds, dec = parse_odds(sp.group(1)) if sp else (None, None)
        entries.append({"name": name, "norm": n, "odds": odds, "dec": dec,
                        "nr": False, "pos": pos, "status": pos == 99})
    title = clean(soup.title.get_text()) if soup.title else ""
    h1 = soup.find("h1")
    head = title + " " + (clean(h1.get_text()) if h1 else "")
    return entries, bool(HCAP_RE.search(head))


# ----------------------------------------------------------------------------
# Scrape the target day
# ----------------------------------------------------------------------------
def race_off(day, hhmm):
    return dt.datetime.combine(day, dt.time(int(hhmm[:2]), int(hhmm[2:])), tzinfo=TZ)


def scrape_day(day, now, warnings):
    today = now.date()
    idx = [f"{BASE}/racecards/{fmt_date(day)}"]
    if day == today:
        idx.append(f"{BASE}/racecards")
    elif day == today + dt.timedelta(days=1):
        idx.append(f"{BASE}/racecards/tomorrow")
    links = discover(idx, day)
    log(f"Found {len(links)} race links for {day}")
    races, runners, per_meeting = [], [], defaultdict(lambda: [0, 0])
    for (course, hhmm), url in sorted(links.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        region = region_of(course)
        if not region_ok(region):
            continue
        off = race_off(day, hhmm)
        if off < now + dt.timedelta(minutes=2):
            continue                       # already off, nothing to bet on or snapshot
        html = get(url)
        if not html:
            warnings.append(f"fetch failed {course} {hhmm}")
            continue
        entries = [e for e in parse_racecard(html) if not e["nr"]]
        priced = [e for e in entries if e["dec"]]
        per_meeting[course][0] += 1
        per_meeting[course][1] += len(priced)
        races.append({"date": day.isoformat(), "course": course, "region": region,
                      "time": hhmm, "url": url, "runners": len(entries), "priced": len(priced)})
        if len(priced) < 2:
            warnings.append(f"{course} {hhmm}: only {len(priced)} priced runners parsed")
            continue
        for e in priced:
            runners.append({"date": day.isoformat(), "course": course, "region": region,
                            "time": hhmm, "off": off, "horse": e["name"], "norm": e["norm"],
                            "odds": e["odds"], "dec": e["dec"]})
    return races, runners, per_meeting


# ----------------------------------------------------------------------------
# Market movers
# ----------------------------------------------------------------------------
def snap_key(date, course, hhmm, horse):
    return f"{date}|{norm(course)}|{hhmm}|{norm(horse)}"


def pick_ref(hist, now, target_min=120, lo=MOVER_MIN_AGE_MIN, hi=300):
    """Snapshot closest to `target_min` minutes ago, within [lo, hi] minutes old."""
    best = None
    for ts, dec in hist:
        age = (now - ts).total_seconds() / 60
        if lo <= age <= hi:
            d = abs(age - target_min)
            if best is None or d < best[0]:
                best = (d, dec)
    return best[1] if best else None


def pick_open(hist, now, lo=MOVER_MIN_AGE_MIN):
    """Earliest snapshot of the day that is at least `lo` minutes old."""
    old = [(ts, dec) for ts, dec in hist if (now - ts).total_seconds() / 60 >= lo]
    return min(old)[1] if old else None


def add_movers(runners, snaps, now):
    for r in runners:
        hist = snaps.get(snap_key(r["date"], r["course"], r["time"], r["horse"]), [])
        ref2, refo = pick_ref(hist, now), pick_open(hist, now)
        r["mover_2h_pct"] = round((r["dec"] - ref2) / ref2 * 100, 1) if ref2 else None
        r["mover_night_pct"] = round((r["dec"] - refo) / refo * 100, 1) if refo else None


# ----------------------------------------------------------------------------
# Model (simple, market based placeholder: swap in your own scoring here)
# ----------------------------------------------------------------------------
def add_model(runners):
    by_race = defaultdict(list)
    for r in runners:
        by_race[(r["date"], r["course"], r["time"])].append(r)
    for field in by_race.values():
        raw = [1.0 / r["dec"] for r in field]
        tot = sum(raw)
        fair = [p / tot for p in raw]
        adj = [p ** 1.1 for p in fair]              # favourite / longshot bias correction
        for r, f, a in zip(field, fair, adj):
            move = r.get("mover_night_pct")
            if move is None:
                move = r.get("mover_2h_pct")
            bonus = min(0.03, max(0.0, -(move or 0.0)) / 100 * 0.15)   # shortening = support
            r["fair_prob"], r["_adj"] = f, a + bonus
        s = sum(r["_adj"] for r in field)
        for r in field:
            r["model_prob"] = r.pop("_adj") / s
            r["value_edge"] = r["model_prob"] - r["fair_prob"]
            r["ev"] = r["model_prob"] * r["dec"] - 1
            r["shorten_score"] = round(max(0.0, -(r.get("mover_2h_pct") or 0.0))
                                       + 0.5 * max(0.0, -(r.get("mover_night_pct") or 0.0)), 1)


def choose_bets(runners):
    best = {}
    for r in runners:
        if r["value_edge"] >= MIN_EDGE and MIN_DEC <= r["dec"] <= MAX_DEC:
            k = (r["date"], r["course"], r["time"])
            if k not in best or r["value_edge"] > best[k]["value_edge"]:
                best[k] = r                             # one selection per race
    picks, per_meet = [], defaultdict(int)
    for r in sorted(best.values(), key=lambda x: -x["value_edge"]):
        if per_meet[r["course"]] >= MAX_PER_MEETING:
            continue
        picks.append(r)
        per_meet[r["course"]] += 1
        if len(picks) >= MAX_BETS:
            break
    return sorted(picks, key=lambda x: (x["time"], x["course"]))


def choose_best_per_race(runners):
    """Highest value_edge runner in every race (no threshold); needs 3+ priced runners."""
    races = defaultdict(list)
    for r in runners:
        races[(r["date"], r["course"], r["time"])].append(r)
    picks = []
    for field in races.values():
        if len(field) >= 3:
            picks.append(max(field, key=lambda x: (x["value_edge"], -x["dec"])))
    return sorted(picks, key=lambda x: (x["date"], x["time"], x["course"]))


def card_order(r):
    """Newest day first, then each course grouped together with its races in time order."""
    d = dt.date.fromisoformat(r["date"]).toordinal() if r.get("date") else 0
    return (-d, r["course"], r["time"])


def upsert_race_bests(rows, picks):
    """One row per race. A PENDING row is re-picked on each run until the race is off
    (the scrape only sees races still to run); settled or started races are left alone."""
    by_key = {r["key"]: r for r in rows}
    added = 0
    for b in picks:
        key = f"{b['date']}|{norm(b['course'])}|{b['time']}"
        vals = {"odds_at_pick": b["odds"], "dec_at_pick": b["dec"], "horse": b["horse"],
                "model_prob": round(b["model_prob"], 4), "value_edge": round(b["value_edge"], 4),
                "region": b["region"]}
        row = by_key.get(key)
        if row is None:
            row = {**{c: "" for c in TRACKER_COLS}, "key": key, "date": b["date"],
                   "course": b["course"], "time": b["time"], "status": "PENDING"}
            row.update(vals)
            rows.append(row)
            by_key[key] = row
            added += 1
        elif row.get("status") == "PENDING":
            row.update(vals)
    return added


# ----------------------------------------------------------------------------
# Bet tracker: settle from results, summarise
# ----------------------------------------------------------------------------
TRACKER_COLS = ["key", "date", "course", "time", "region", "horse", "odds_at_pick",
                "dec_at_pick", "model_prob", "value_edge", "status", "position",
                "field_size", "places_paid", "sp", "sp_dec", "won", "placed",
                "pnl_win_units", "pnl_place_units", "settled_at"]


def settle_row(row, finishers, handicap, now):
    """Fill result columns on a tracker row. finishers = parse_result() entries in page order."""
    runners = [e for e in finishers if not e.get("nr")]
    names = [e["norm"] for e in runners]
    target = norm(row["horse"])
    if target not in names:
        row["status"] = "UNMATCHED"       # horse not on the result page: check by hand (or NR)
        return
    i = names.index(target)
    e = runners[i]
    field = len(runners)
    pos = e["pos"] if e.get("pos") is not None else i + 1
    places, frac = place_terms(field, handicap)
    sp_dec = e["dec"] or to_float(row.get("dec_at_pick"), 2.0)
    won = pos == 1
    placed = pos <= places
    place_odds = (sp_dec - 1) * frac + 1
    row.update({
        "status": "SETTLED", "position": pos if pos != 99 else "DNF", "field_size": field,
        "places_paid": places, "sp": e["odds"] or "", "sp_dec": round(sp_dec, 3),
        "won": 1 if won else 0, "placed": 1 if placed else 0,
        "pnl_win_units": round(sp_dec - 1 if won else -1, 3),
        "pnl_place_units": round(place_odds - 1 if placed else -1, 3),
        "settled_at": now.isoformat(timespec="minutes"),
    })


_RES_CACHE = {}


def fetch_results_for(day, needed):
    """needed = {(norm course, hhmm): course}. Returns {(norm course, hhmm): (entries, handicap)}."""
    out = {}
    for (ncourse, hhmm), course in needed.items():
        ck = (day.isoformat(), ncourse, hhmm)
        if ck not in _RES_CACHE:
            url = f"{BASE}/raceresults/{fmt_date(day)}/{course.replace(' ', '-')}/{hhmm}"
            html = get(url)
            _RES_CACHE[ck] = parse_result(html) if html else None
        if _RES_CACHE[ck] is not None:
            out[(ncourse, hhmm)] = _RES_CACHE[ck]
    return out


def settle_all(rows, now, warnings):
    pending = defaultdict(list)
    for r in rows:
        if r.get("status") == "PENDING":
            pending[r["date"]].append(r)
    for date_s, group in pending.items():
        try:
            day = dt.date.fromisoformat(date_s)
        except ValueError:
            continue
        due = [r for r in group if now >= race_off(day, r["time"]) + dt.timedelta(minutes=25)]
        if not due:
            continue
        needed = {(norm(r["course"]), r["time"]): r["course"] for r in due}
        results = fetch_results_for(day, needed)
        for r in due:
            res = results.get((norm(r["course"]), r["time"]))
            if res is None:
                if now > race_off(day, r["time"]) + dt.timedelta(hours=48):
                    r["status"] = "NO_RESULT"
                continue
            entries, handicap = res
            if len(entries) < 2 or not any(e["pos"] is not None or e["dec"] for e in entries):
                warnings.append(f"no result yet / not parsed: {r['course']} {r['time']}")
                continue
            settle_row(r, entries, handicap, now)


def band(x, cuts, labels):
    for c, lab in zip(cuts, labels):
        if x < c:
            return lab
    return labels[-1]


def summarise(rows):
    settled = [r for r in rows if r.get("status") == "SETTLED"]
    groups = {"ALL BETS": settled}
    for r in settled:
        edge = to_float(r.get("value_edge"), 0)
        dec = to_float(r.get("sp_dec"), 0)
        groups.setdefault("Region: " + str(r["region"]), []).append(r)
        groups.setdefault("Edge " + band(edge, [0.03, 0.05], ["2-3%", "3-5%", "5%+"]), []).append(r)
        groups.setdefault("Odds " + band(dec, [2.5, 5, 10], ["<1.5/1", "1.5/1-4/1", "4/1-9/1", "9/1+"]), []).append(r)
        fs = to_float(r.get("field_size"), 0)
        groups.setdefault("Field " + band(fs, [8, 13], ["<8", "8-12", "13+"]), []).append(r)
    header = ["Segment", "Bets", "Wins", "Win %", "Places", "Place %",
              "Win P&L (units)", "Win ROI %", "Place P&L (units)", "Place ROI %"]
    out = []
    for name in sorted(groups, key=lambda s: (s != "ALL BETS", s)):
        g = groups[name]
        n = len(g)
        if n == 0:
            out.append([name, 0, 0, "", 0, "", 0, "", 0, ""])
            continue
        w = sum(int(to_float(r["won"], 0)) for r in g)
        p = sum(int(to_float(r["placed"], 0)) for r in g)
        wp = sum(to_float(r["pnl_win_units"], 0) for r in g)
        pp = sum(to_float(r["pnl_place_units"], 0) for r in g)
        out.append([name, n, w, round(w / n * 100, 1), p, round(p / n * 100, 1),
                    round(wp, 2), round(wp / n * 100, 1), round(pp, 2), round(pp / n * 100, 1)])
    counts = defaultdict(int)
    for r in rows:
        counts[r.get("status", "")] += 1
    out.append([])
    out.append(["Status counts: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))])
    return header, out


# ----------------------------------------------------------------------------
# Google Sheets helpers
# ----------------------------------------------------------------------------
def open_sheet():
    import gspread
    from google.oauth2.service_account import Credentials
    scopes = ["https://www.googleapis.com/auth/spreadsheets",
              "https://www.googleapis.com/auth/drive"]
    raw = os.environ.get("GOOGLE_CREDS")
    if raw:
        creds = Credentials.from_service_account_info(json.loads(raw), scopes=scopes)
    else:
        creds = Credentials.from_service_account_file("credentials.json", scopes=scopes)
    return gspread.authorize(creds).open(SHEET_NAME)


def get_tab(sh, name):
    import gspread
    try:
        return sh.worksheet(name)
    except gspread.WorksheetNotFound:
        return sh.add_worksheet(title=name, rows=1000, cols=26)


def cell(v):
    if v is None:
        return ""
    if isinstance(v, float):
        return round(v, 4)
    if isinstance(v, (dt.datetime, dt.date)):
        return v.isoformat()
    return v


def write_tab(sh, name, header, rows):
    ws = get_tab(sh, name)
    values = [[cell(c) for c in header]] + [[cell(c) for c in r] for r in rows]
    ws.clear()
    ws.resize(rows=max(len(values) + 20, 50), cols=max(len(header), 12))
    ws.update(values=values, range_name="A1")


def append_rows(sh, name, header, rows):
    if not rows:
        return
    ws = get_tab(sh, name)
    if not ws.get_all_values()[:1]:
        ws.append_row(header)
    ws.append_rows([[cell(c) for c in r] for r in rows])


def load_tracker(sh, tab="BET_TRACKER"):
    ws = get_tab(sh, tab)
    vals = ws.get_all_values()
    if not vals:
        return []
    head = vals[0]
    rows = []
    for v in vals[1:]:
        d = {h: (v[i] if i < len(v) else "") for i, h in enumerate(head)}
        for c in TRACKER_COLS:
            d.setdefault(c, "")
        if d["key"]:
            rows.append(d)
    return rows


def save_tracker(sh, rows, tab="BET_TRACKER"):
    write_tab(sh, tab, TRACKER_COLS, [[r.get(c, "") for c in TRACKER_COLS] for r in rows])


def load_snapshots(sh, day):
    """{key: [(ts, dec)]} for the day, pruning the log when it grows large."""
    ws = get_tab(sh, "MARKET_SNAPSHOTS_LOG")
    vals = ws.get_all_values()
    snaps = defaultdict(list)
    keep = []
    cutoff = (day - dt.timedelta(days=3)).isoformat()
    for v in vals[1:]:
        if len(v) < 6:
            continue
        if v[1] >= cutoff:
            keep.append(v)
        if v[1] != day.isoformat():
            continue
        try:
            snaps[snap_key(v[1], v[2], v[3], v[4])].append(
                (dt.datetime.fromisoformat(v[0]), float(v[5])))
        except ValueError:
            continue
    if len(vals) > 30000:
        write_tab(sh, "MARKET_SNAPSHOTS_LOG", SNAP_HEAD, keep)
    return snaps, max(0, len(vals) - 1)


SNAP_HEAD = ["ts", "date", "course", "time", "horse", "dec_price"]


VISIBLE_TABS = ["BEST_PER_RACE", "BETS_TO_PLACE", "RACE_BEST_SUMMARY", "RACE_BEST_TRACKER"]


def tidy_tabs(sh):
    """Show only VISIBLE_TABS (in that order, first); hide every other tab. Hidden tabs keep updating."""
    for name in VISIBLE_TABS:
        get_tab(sh, name)                       # make sure they exist before anything is hidden
    sheets = sh.worksheets()
    by_name = {w.title: w for w in sheets}
    for name in VISIBLE_TABS:                   # unhide first: Google needs one visible tab
        ws = by_name[name]
        if ws._properties.get("hidden"):
            ws.show()
    for ws in sheets:
        if ws.title not in VISIBLE_TABS and not ws._properties.get("hidden"):
            ws.hide()
    wanted = [by_name[n] for n in VISIBLE_TABS] + [w for w in sheets if w.title not in VISIBLE_TABS]
    if [w.title for w in sheets] != [w.title for w in wanted]:
        sh.reorder_worksheets(wanted)


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main():
    now = dt.datetime.now(TZ)
    day = now.date() + (dt.timedelta(days=1) if now.hour >= 22 else dt.timedelta(0))
    warnings, status = [], "OK"
    log(f"Run at {now:%Y-%m-%d %H:%M} Dublin, target day {day}, region={REGION}")
    sh = open_sheet()

    # 1. settle old bets first so results are never lost
    tracker = load_tracker(sh)
    settle_all(tracker, now, warnings)
    race_best = load_tracker(sh, "RACE_BEST_TRACKER")
    settle_all(race_best, now, warnings)

    # 2. scrape target day
    races, runners, per_meeting = scrape_day(day, now, warnings)

    # 3. snapshots + movers
    snaps, snap_rows = load_snapshots(sh, day)
    add_movers(runners, snaps, now)
    stamp = now.isoformat(timespec="seconds")
    append_rows(sh, "MARKET_SNAPSHOTS_LOG", SNAP_HEAD,
                [[stamp, r["date"], r["course"], r["time"], r["horse"], r["dec"]] for r in runners])

    # 4. model + bets
    add_model(runners)
    runners.sort(key=lambda r: (r["time"], r["course"], r["dec"]))
    bets = choose_bets(runners)
    known = {r["key"] for r in tracker}
    new_bets = 0
    for b in bets:
        key = f"{b['date']}|{norm(b['course'])}|{b['time']}|{norm(b['horse'])}"
        if key in known:
            continue
        tracker.append({**{c: "" for c in TRACKER_COLS}, "key": key, "date": b["date"],
                        "course": b["course"], "time": b["time"], "region": b["region"],
                        "horse": b["horse"], "odds_at_pick": b["odds"], "dec_at_pick": b["dec"],
                        "model_prob": round(b["model_prob"], 4),
                        "value_edge": round(b["value_edge"], 4), "status": "PENDING"})
        new_bets += 1
    save_tracker(sh, tracker)

    picks = choose_best_per_race(runners)
    new_race_bests = upsert_race_bests(race_best, picks)
    save_tracker(sh, race_best, "RACE_BEST_TRACKER")

    # 5. write tabs
    r_head = ["date", "course", "region", "time", "horse", "odds", "dec", "fair_prob", "model_prob",
              "value_edge", "ev", "mover_2h_pct", "mover_night_pct"]

    def rr(r):
        return [r["date"], r["course"], r["region"], r["time"], r["horse"], r["odds"], r["dec"],
                r["fair_prob"], r["model_prob"], r["value_edge"], r["ev"],
                r["mover_2h_pct"], r["mover_night_pct"]]

    if runners:
        write_tab(sh, "RUNNERS_TARGET", r_head, [rr(r) for r in runners])
        write_tab(sh, "SIGNALS", r_head + ["shorten_score"],
                  [rr(r) + [r["shorten_score"]] for r in sorted(runners, key=lambda x: -x["value_edge"])])
        write_tab(sh, "VALUE_BETS_TARGET", r_head,
                  [rr(r) for r in sorted(runners, key=lambda x: -x["value_edge"])
                   if r["value_edge"] >= MIN_EDGE])
        write_tab(sh, "BETS_TO_PLACE", r_head, [rr(r) for r in bets])
    write_tab(sh, "RACES_TARGET", ["date", "course", "region", "time", "url", "runners", "priced"],
              [[x["date"], x["course"], x["region"], x["time"], x["url"], x["runners"], x["priced"]]
               for x in races])

    m_head = ["course", "time", "horse", "odds", "dec", "ref_move_pct", "direction"]

    def movers(field):
        rows = [r for r in runners if r.get(field) is not None and abs(r[field]) >= MOVER_SHOW_PCT]
        rows.sort(key=lambda r: r[field])
        return [[r["course"], r["time"], r["horse"], r["odds"], r["dec"], r[field],
                 "shortening" if r[field] < 0 else "drifting"] for r in rows]

    n2h = sum(1 for r in runners if r.get("mover_2h_pct") is not None)
    nnight = sum(1 for r in runners if r.get("mover_night_pct") is not None)
    for tab, field, have in (("MARKET_MOVERS_2H", "mover_2h_pct", n2h),
                             ("MARKET_MOVERS", "mover_night_pct", nnight)):
        rows = movers(field)
        if not rows:
            msg = ("No reference snapshot yet: builds after ~20+ minutes of runs"
                   if have == 0 else f"No moves of {MOVER_SHOW_PCT}%+ ({have} runners compared)")
            rows = [[msg, "", "", "", "", "", ""]]
        write_tab(sh, tab, m_head, rows)

    write_tab(sh, "TARGET_DAY", ["target_day", "generated"], [[day.isoformat(), stamp]])
    head, srows = summarise(tracker)
    write_tab(sh, "TRACKER_SUMMARY", head, srows)

    recent = (day - dt.timedelta(days=1)).isoformat()
    card = sorted((r for r in race_best if r["date"] >= recent), key=card_order)
    write_tab(sh, "BEST_PER_RACE",
              ["date", "time", "course", "region", "horse", "odds", "model_prob", "value_edge",
               "status", "position", "field_size", "sp", "won", "placed",
               "pnl_win_units", "pnl_place_units"],
              [[r["date"], r["time"], r["course"], r["region"], r["horse"], r["odds_at_pick"],
                r["model_prob"], r["value_edge"], r["status"], r["position"], r["field_size"],
                r["sp"], r["won"], r["placed"], r["pnl_win_units"], r["pnl_place_units"]]
               for r in card])
    rhead, rrows = summarise(race_best)
    write_tab(sh, "RACE_BEST_SUMMARY", rhead, rrows)

    # 6. dashboard + log
    if not runners:
        status = "NO RUNNERS PARSED"
    elif warnings:
        status = "OK WITH WARNINGS"
    dash = [["status", status], ["run_time", stamp], ["target_day", day.isoformat()],
            ["region_filter", REGION], ["meetings", len(per_meeting)], ["races", len(races)],
            ["runners_priced", len(runners)], ["bets_today", len(bets)],
            ["new_bets_logged", new_bets], ["snapshot_rows", snap_rows],
            ["runners_with_2h_ref", n2h], ["runners_with_night_ref", nnight],
            ["pending_bets", sum(1 for r in tracker if r["status"] == "PENDING")],
            ["settled_bets", sum(1 for r in tracker if r["status"] == "SETTLED")],
            ["unmatched_bets", sum(1 for r in tracker if r["status"] == "UNMATCHED")],
            ["race_best_picks_today", len(picks)], ["race_best_new", new_race_bests],
            ["race_best_pending", sum(1 for r in race_best if r["status"] == "PENDING")],
            ["race_best_settled", sum(1 for r in race_best if r["status"] == "SETTLED")],
            ["race_best_unmatched", sum(1 for r in race_best if r["status"] == "UNMATCHED")], []]
    for course, (nr, nrun) in sorted(per_meeting.items()):
        dash.append([f"meeting: {course}", f"{nr} races, {nrun} priced runners"])
    for w in warnings[:40]:
        dash.append(["warning", w])
    write_tab(sh, "DASHBOARD", ["item", "value"], dash)
    append_rows(sh, "RUN_LOG", ["run_time", "target_day", "races", "runners", "bets", "status"],
                [[stamp, day.isoformat(), len(races), len(runners), len(bets), status]])
    try:
        tidy_tabs(sh)
    except Exception as ex:                     # cosmetic only, never fail the run for it
        log(f"  could not tidy tabs: {ex}")
    log(f"Done: {status}, {len(races)} races, {len(runners)} runners, {len(bets)} bets, "
        f"{new_bets} new")
    if not runners:
        sys.exit(1)       # make the Actions run go red so a broken scrape is obvious


if __name__ == "__main__":
    main()

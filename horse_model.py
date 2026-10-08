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
  MAX_FIELD_HCAP  handicaps with this many runners or more are flagged / skipped, default 16
  STRICT_FILTERS  1 (default) = BETS_TO_PLACE skips flagged races and horses with <2 prior runs
  DEBUG_DUMP      set to 1 to save fetched pages into ./debug (uploaded by the workflow)
"""
import datetime as dt
import json
import math
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
BIG_FIELD = int(os.environ.get("MAX_FIELD_HCAP") or 16)
STRICT = (os.environ.get("STRICT_FILTERS") or "1") != "0"
DEBUG = os.environ.get("DEBUG_DUMP") == "1"

MIN_DEC, MAX_DEC = 1.8, 13.0       # price window for selections (decimal odds)
# Starting weights for the form model (logit units). Guesses, to be tuned from the tracker.
W = {"market": 1.1, "form": 0.7, "rating": 0.20, "trainer": 0.12, "jockey": 0.08,
     "cd": 0.15, "layoff": 0.15, "move": 0.8}
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
RUN_DATE_RE = re.compile(r"(\d{1,2})(?:st|nd|rd|th)\s+([A-Z][a-z]{2})\s+(\d{2})\b")
RUN_POS_RE = re.compile(r"\b([A-Za-z0-9]{1,4})\s+of\s+(\d{1,2})\s+runners")
RATED_RE = re.compile(r"Rated\s+(\d{2,3})")
DECLARED_RE = re.compile(r"(\d{1,2})\s+Declared", re.I)
SYMBOLS_RE = re.compile(r"\((\d{1,3})\)\s*((?:(?:cd|c|d|bf)(?=[\d\s(]|$)\d*\s*)*)")
MON_IDX = {m: i + 1 for i, m in enumerate(MON3)}


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


def horse_blocks(soup):
    """[{name, norm, text, row}] for each distinct horse link, in page order."""
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
            out.append({"name": name, "norm": n, "row": row,
                        "text": clean(row.get_text(" ", strip=True)) if row else ""})
        if out:
            return out
    return []


def horse_rows(soup):
    """[(name, norm, row_text)] for each distinct horse link, in page order."""
    return [(b["name"], b["norm"], b["text"]) for b in horse_blocks(soup)]


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


def person_key(s):
    return norm(re.sub(r"\(\d+\)", "", s or ""))


def smooth_rate(wins, runs, prior=0.10, k=20):
    return (wins + prior * k) / (runs + k)


def parse_in_form(lines, heading, end):
    """Read a 'Trainers/Jockeys In Form last 21 days' table from page text lines
    -> {person_key: (wins, runs)}."""
    out, name, nums, on = {}, None, [], False
    for ln in lines:
        if not on:
            on = bool(re.match(heading, ln, re.I))
            continue
        if re.search(end, ln, re.I):
            break
        if re.fullmatch(r"\d+", ln):
            nums.append(int(ln))
            if len(nums) == 3 and name:
                out[person_key(name)] = (nums[0], nums[1])
                name, nums = None, []
        elif ln not in ("Trainer", "Jockey", "Wins", "Runs", "%", "Trainer/Jockey"):
            name, nums = ln, []
    return out


def layoff_term(days):
    if days is None:
        return 0.0
    return -1.0 if days > 90 else (-0.5 if days > 45 else 0.0)


def runner_features(text, race_day):
    """Rating, recent-form score, days since last run and course/distance record
    from one runner's block of racecard text."""
    pre, _, rest = text.partition("Last Three Runs")
    section = rest.split("Head to Head", 1)[0]
    f = {"rating": None, "form_score": None, "runs_n": 0, "days_off": None, "cd": 0.0}
    m = RATED_RE.search(pre)
    if m:
        f["rating"] = int(m.group(1))
    sm = SYMBOLS_RE.search(pre)
    if sm:
        toks = [re.sub(r"\d", "", t) for t in sm.group(2).split()]
        if "cd" in toks:
            f["cd"] = 1.0
        elif "c" in toks or "d" in toks:
            f["cd"] = 0.5
    anchors = list(RUN_DATE_RE.finditer(section))
    f["runs_n"] = len(anchors)
    pcts = []
    for i, a in enumerate(anchors):
        if i == 0 and race_day is not None and a.group(2) in MON_IDX:
            try:
                last = dt.date(2000 + int(a.group(3)), MON_IDX[a.group(2)], int(a.group(1)))
                d = (race_day - last).days
                f["days_off"] = d if d >= 0 else None
            except ValueError:
                pass
        end = anchors[i + 1].start() if i + 1 < len(anchors) else len(section)
        pm = RUN_POS_RE.search(section[a.end():end])
        if not pm:
            continue
        om = re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)", pm.group(1))
        if om:
            field = int(pm.group(2))
            pcts.append(1.0 - (int(om.group(1)) - 1) / max(field - 1, 1))
        else:
            pcts.append(0.1)                      # pulled up / fell / unseated etc.
    if pcts:
        w = [0.5, 0.3, 0.2][:len(pcts)]
        f["form_score"] = sum(x * y for x, y in zip(pcts, w)) / sum(w)
    return f


def parse_racecard(html, race_day=None):
    """Runners in card order, priced from the Probable SP line, with form features.
    Returns (entries, race_info)."""
    soup = BeautifulSoup(html, "html.parser")
    prices = parse_probable_sp(soup)
    lines = [clean(x) for x in soup.get_text("\n", strip=True).split("\n")]
    trainers = parse_in_form(lines, r"Trainers In Form", r"For Trainers in this race")
    jockeys = parse_in_form(lines, r"Jockeys In Form", r"For Jockeys in this race")
    entries = []
    for b in horse_blocks(soup):
        odds, dec = parse_odds(prices.get(b["norm"], ""))
        f = runner_features(b["text"], race_day)
        row = b["row"]
        ta = row.find("a", href=re.compile(r"/trainer/", re.I)) if row else None
        ja = row.find("a", href=re.compile(r"/jockey/", re.I)) if row else None
        tname = clean(ta.get_text(" ", strip=True)) if ta else ""
        jname = clean(ja.get_text(" ", strip=True)) if ja else ""
        tw = trainers.get(person_key(tname))
        jw = jockeys.get(person_key(jname))
        entries.append({
            "name": b["name"], "norm": b["norm"], "odds": odds, "dec": dec,
            "nr": False, "pos": None, "status": False,
            "trainer": tname, "jockey": jname,
            "trainer_rate": smooth_rate(*tw) if tw else None,
            "jockey_rate": smooth_rate(*jw) if jw else None, **f})
    title = clean(soup.title.get_text()) if soup.title else ""
    h1 = soup.find("h1")
    head = title + " " + (clean(h1.get_text()) if h1 else "")
    dm = DECLARED_RE.search(soup.get_text(" ", strip=True))
    declared = int(dm.group(1)) if dm else len(entries)
    return entries, {"handicap": bool(HCAP_RE.search(head)), "declared": declared}


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
    info = {"links": len(links), "upcoming": 0}
    for (course, hhmm), url in sorted(links.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        region = region_of(course)
        if not region_ok(region):
            continue
        off = race_off(day, hhmm)
        if off < now + dt.timedelta(minutes=2):
            continue                       # already off, nothing to bet on or snapshot
        info["upcoming"] += 1
        html = get(url)
        if not html:
            warnings.append(f"fetch failed {course} {hhmm}")
            continue
        entries, rinfo = parse_racecard(html, day)
        entries = [e for e in entries if not e["nr"]]
        priced = [e for e in entries if e["dec"]]
        per_meeting[course][0] += 1
        per_meeting[course][1] += len(priced)
        declared = rinfo["declared"] or len(entries)
        conf = (sum(1 for e in entries if e["runs_n"] >= 2) / len(entries)) if entries else 0.0
        flags = []
        if rinfo["handicap"] and declared >= BIG_FIELD:
            flags.append("BIG-FIELD HCAP")
        if entries and conf < 0.7:
            flags.append("MANY UNRACED")
        flag = " + ".join(flags)
        races.append({"date": day.isoformat(), "course": course, "region": region,
                      "time": hhmm, "url": url, "runners": len(entries), "priced": len(priced),
                      "handicap": rinfo["handicap"], "declared": declared,
                      "confidence": round(conf, 2), "flag": flag})
        if len(priced) < 2:
            warnings.append(f"{course} {hhmm}: only {len(priced)} priced runners parsed")
            continue
        for e in priced:
            runners.append({"date": day.isoformat(), "course": course, "region": region,
                            "time": hhmm, "off": off, "horse": e["name"], "norm": e["norm"],
                            "odds": e["odds"], "dec": e["dec"], "flag": flag,
                            "confidence": round(conf, 2), "rating": e["rating"],
                            "form_score": e["form_score"], "runs_n": e["runs_n"],
                            "days_off": e["days_off"], "cd": e["cd"],
                            "trainer": e["trainer"], "jockey": e["jockey"],
                            "trainer_rate": e["trainer_rate"], "jockey_rate": e["jockey_rate"]})
    return races, runners, per_meeting, info


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
def centered(vals):
    """Subtract the race average (of runners that have data); missing data counts as average."""
    known = [v for v in vals if v is not None]
    if len(known) < 2:
        return [0.0] * len(vals)
    m = sum(known) / len(known)
    return [(v - m) if v is not None else 0.0 for v in vals]


def clip(x, lo, hi):
    return max(lo, min(hi, x))


def add_model(runners):
    """Market probability (bias corrected) nudged by form, rating, trainer/jockey form,
    course/distance record, time off and price moves, then renormalised per race."""
    by_race = defaultdict(list)
    for r in runners:
        by_race[(r["date"], r["course"], r["time"])].append(r)
    for field in by_race.values():
        raw = [1.0 / r["dec"] for r in field]
        tot = sum(raw)
        fair = [p / tot for p in raw]
        forms = centered([r.get("form_score") for r in field])
        ratings = centered([r.get("rating") for r in field])
        trates = centered([r.get("trainer_rate") for r in field])
        jrates = centered([r.get("jockey_rate") for r in field])
        logits = []
        for r, f, fc, rc, tc, jc in zip(field, fair, forms, ratings, trates, jrates):
            move = r.get("mover_night_pct")
            if move is None:
                move = r.get("mover_2h_pct")
            short = min(0.3, max(0.0, -(move or 0.0)) / 100)
            terms = {
                "form": W["form"] * fc,
                "rating": W["rating"] * clip(rc / 10, -3, 3),
                "trainer": W["trainer"] * clip(tc / 0.10, -2, 2),
                "jockey": W["jockey"] * clip(jc / 0.10, -2, 2),
                "c&d": W["cd"] * (r.get("cd") or 0.0),
                "layoff": W["layoff"] * layoff_term(r.get("days_off")),
                "move": W["move"] * short,
            }
            r["fair_prob"], r["_terms"] = f, terms
            logits.append(W["market"] * math.log(f) + sum(terms.values()))
        mx = max(logits)
        ex = [math.exp(x - mx) for x in logits]
        s = sum(ex)
        for r, e in zip(field, ex):
            r["model_prob"] = e / s
            r["value_edge"] = r["model_prob"] - r["fair_prob"]
            r["ev"] = r["model_prob"] * r["dec"] - 1
            r["shorten_score"] = round(max(0.0, -(r.get("mover_2h_pct") or 0.0))
                                       + 0.5 * max(0.0, -(r.get("mover_night_pct") or 0.0)), 1)
            top = sorted(r.pop("_terms").items(), key=lambda kv: -kv[1])[:2]
            r["reason"] = ", ".join(f"{k} {v:+.2f}" for k, v in top if v >= 0.03)


def choose_bets(runners):
    best = {}
    for r in runners:
        if r["value_edge"] >= MIN_EDGE and MIN_DEC <= r["dec"] <= MAX_DEC:
            if STRICT and (r.get("flag") or (r.get("runs_n") or 0) < 2):
                continue                                # big-field handicaps, unraced fields, debutants
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


def _by_race(runners):
    races = defaultdict(list)
    for r in runners:
        races[(r["date"], r["course"], r["time"])].append(r)
    return races


def choose_best_per_race(runners):
    """Highest value_edge runner in every race (no threshold); needs 3+ priced runners."""
    picks = [max(f, key=lambda x: (x["value_edge"], -x["dec"]))
             for f in _by_race(runners).values() if len(f) >= 3]
    return sorted(picks, key=lambda x: (x["date"], x["time"], x["course"]))


def choose_favourite_per_race(runners):
    """Shortest-priced runner in every race: the baseline the picks have to beat."""
    picks = [min(f, key=lambda x: (x["dec"], x["horse"]))
             for f in _by_race(runners).values() if len(f) >= 3]
    return sorted(picks, key=lambda x: (x["date"], x["time"], x["course"]))


def card_order(r):
    """Newest day first, then each course grouped together with its races in time order."""
    d = dt.date.fromisoformat(r["date"]).toordinal() if r.get("date") else 0
    return (-d, r["course"], r["time"])


def pick_fields(b):
    fs = b.get("form_score")
    return {"odds_at_pick": b["odds"], "dec_at_pick": b["dec"], "horse": b["horse"],
            "model_prob": round(b["model_prob"], 4), "value_edge": round(b["value_edge"], 4),
            "region": b["region"], "form_score": round(fs, 2) if fs is not None else "",
            "confidence": b.get("confidence", ""), "reason": b.get("reason", ""),
            "race_flag": b.get("flag", "")}


def upsert_race_bests(rows, picks):
    """One row per race. A PENDING row is re-picked on each run until the race is off
    (the scrape only sees races still to run); settled or started races are left alone."""
    by_key = {r["key"]: r for r in rows}
    added = 0
    for b in picks:
        key = f"{b['date']}|{norm(b['course'])}|{b['time']}"
        vals = pick_fields(b)
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
                "pnl_win_units", "pnl_place_units", "settled_at",
                "form_score", "confidence", "reason", "race_flag", "clv_pct"]


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
    pick_dec = to_float(row.get("dec_at_pick"))
    clv = round((pick_dec / sp_dec - 1) * 100, 1) if pick_dec and sp_dec else ""
    row.update({"clv_pct": clv,
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


def race_key(r):
    return f"{r['date']}|{norm(r['course'])}|{r['time']}"


SUM_HEAD = ["Segment", "Bets", "Wins", "Win %", "Places", "Place %",
            "Win P&L (units)", "Win ROI %", "Place P&L (units)", "Place ROI %", "Avg CLV %"]


def stats_row(name, g):
    n = len(g)
    if n == 0:
        return [name, 0, 0, "", 0, "", 0, "", 0, "", ""]
    w = sum(int(to_float(r["won"], 0)) for r in g)
    p = sum(int(to_float(r["placed"], 0)) for r in g)
    wp = sum(to_float(r["pnl_win_units"], 0) for r in g)
    pp = sum(to_float(r["pnl_place_units"], 0) for r in g)
    clvs = [c for c in (to_float(r.get("clv_pct")) for r in g) if c is not None]
    return [name, n, w, round(w / n * 100, 1), p, round(p / n * 100, 1),
            round(wp, 2), round(wp / n * 100, 1), round(pp, 2), round(pp / n * 100, 1),
            round(sum(clvs) / len(clvs), 1) if clvs else ""]


def summarise(rows, fav_rows=None):
    """Win/place/ROI/CLV by segment. With fav_rows, adds a 'favourite in the same races' baseline."""
    settled = [r for r in rows if r.get("status") == "SETTLED"]
    groups = {"ALL BETS": settled}
    for r in settled:
        edge = to_float(r.get("value_edge"), 0)
        dec = to_float(r.get("sp_dec"), 0)
        groups.setdefault("Region: " + str(r["region"]), []).append(r)
        groups.setdefault("Edge " + band(edge, [0.03, 0.05], ["<3%", "3-5%", "5%+"]), []).append(r)
        groups.setdefault("Odds " + band(dec, [2.5, 5, 10], ["<1.5/1", "1.5/1-4/1", "4/1-9/1", "9/1+"]), []).append(r)
        fs = to_float(r.get("field_size"), 0)
        groups.setdefault("Field " + band(fs, [8, 13], ["<8", "8-12", "13+"]), []).append(r)
        groups.setdefault("Race " + ("flagged" if r.get("race_flag") else "OK"), []).append(r)
        clv = to_float(r.get("clv_pct"))
        if clv is not None:
            groups.setdefault("Price " + ("shortened (CLV+)" if clv > 0 else "drifted (CLV-)"), []).append(r)
    out = [stats_row(name, groups[name])
           for name in sorted(groups, key=lambda s: (s != "ALL BETS", s))]
    if fav_rows is not None:
        keys = {race_key(r) for r in settled}
        base = [f for f in fav_rows if f.get("status") == "SETTLED" and race_key(f) in keys]
        out.insert(1, stats_row("BASELINE: favourite, same races", base))
    counts = defaultdict(int)
    for r in rows:
        counts[r.get("status", "")] += 1
    out.append([])
    out.append(["Status counts: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))])
    return SUM_HEAD, out


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
    fav_best = load_tracker(sh, "FAV_BASELINE_TRACKER")
    settle_all(fav_best, now, warnings)

    # 2. scrape target day
    races, runners, per_meeting, info = scrape_day(day, now, warnings)
    rolled = False
    if day == now.date() and info["links"] > 0 and info["upcoming"] == 0:
        rolled = True
        day = day + dt.timedelta(days=1)          # today's racing is all over: do tomorrow
        log(f"No races left today, moving on to {day}")
        races, runners, per_meeting, info = scrape_day(day, now, warnings)

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
                        "course": b["course"], "time": b["time"], "status": "PENDING",
                        **pick_fields(b)})
        new_bets += 1
    save_tracker(sh, tracker)

    picks = choose_best_per_race(runners)
    new_race_bests = upsert_race_bests(race_best, picks)
    save_tracker(sh, race_best, "RACE_BEST_TRACKER")
    upsert_race_bests(fav_best, choose_favourite_per_race(runners))
    save_tracker(sh, fav_best, "FAV_BASELINE_TRACKER")

    # 5. write tabs
    r_head = ["date", "course", "region", "time", "horse", "odds", "dec", "fair_prob", "model_prob",
              "value_edge", "ev", "mover_2h_pct", "mover_night_pct", "form_score", "rating",
              "days_off", "trainer_rate", "jockey_rate", "confidence", "race_flag", "reason"]

    def rr(r):
        return [r["date"], r["course"], r["region"], r["time"], r["horse"], r["odds"], r["dec"],
                r["fair_prob"], r["model_prob"], r["value_edge"], r["ev"],
                r["mover_2h_pct"], r["mover_night_pct"], r.get("form_score"), r.get("rating"),
                r.get("days_off"), r.get("trainer_rate"), r.get("jockey_rate"),
                r.get("confidence"), r.get("flag"), r.get("reason")]

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
    head, srows = summarise(tracker, fav_best)
    write_tab(sh, "TRACKER_SUMMARY", head, srows)

    recent = (day - dt.timedelta(days=1)).isoformat()
    card = sorted((r for r in race_best if r["date"] >= recent), key=card_order)
    write_tab(sh, "BEST_PER_RACE",
              ["date", "time", "course", "region", "horse", "odds", "model_prob", "value_edge",
               "form_score", "confidence", "reason", "race_flag", "status", "position",
               "field_size", "sp", "clv_pct", "won", "placed", "pnl_win_units", "pnl_place_units"],
              [[r["date"], r["time"], r["course"], r["region"], r["horse"], r["odds_at_pick"],
                r["model_prob"], r["value_edge"], r["form_score"], r["confidence"], r["reason"],
                r["race_flag"], r["status"], r["position"], r["field_size"], r["sp"],
                r["clv_pct"], r["won"], r["placed"], r["pnl_win_units"], r["pnl_place_units"]]
               for r in card])
    rhead, rrows = summarise(race_best, fav_best)
    write_tab(sh, "RACE_BEST_SUMMARY", rhead, rrows)

    # 6. dashboard + log
    entries_seen = sum(x["runners"] for x in races)
    fatal = True
    if info["links"] == 0 and rolled:
        status, fatal = "TOMORROW'S CARD NOT PUBLISHED YET", False
    elif info["links"] == 0:
        status = "NO RACE LINKS FOUND (site blocked or layout changed)"
    elif info["upcoming"] == 0:
        status, fatal = "NO RACES LEFT", False
    elif not races:
        status = "RACE PAGES FAILED TO LOAD"
    elif entries_seen == 0:
        status = "NO RUNNERS PARSED"
    elif not runners:
        status, fatal = "NO PRICES YET (Probable SP not published)", False
    else:
        fatal = False
        if warnings:
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
            ["race_best_unmatched", sum(1 for r in race_best if r["status"] == "UNMATCHED")],
            ["runners_with_form_pct", round(100 * sum(1 for r in runners if (r.get("runs_n") or 0) >= 2)
                                            / len(runners)) if runners else 0],
            ["runners_with_rating_pct", round(100 * sum(1 for r in runners if r.get("rating"))
                                              / len(runners)) if runners else 0],
            ["runners_with_trainer_form_pct", round(100 * sum(1 for r in runners if r.get("trainer_rate") is not None)
                                                    / len(runners)) if runners else 0],
            ["races_flagged", sum(1 for x in races if x.get("flag"))],
            ["baseline_settled", sum(1 for r in fav_best if r["status"] == "SETTLED")], []]
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
    if fatal:
        sys.exit(1)       # make the Actions run go red so a broken scrape is obvious


if __name__ == "__main__":
    main()

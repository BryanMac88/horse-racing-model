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
  MIN_EV          minimum expected value at the best bookmaker price for BETS_TO_PLACE, default 0
  AUTO_FILTER     1 (default) = skip segments the tracker shows are losing (needs 40+ settled bets)
  USE_FITTED_WEIGHTS  1 = use weights fitted from your own results once 300+ races are logged
  ANALYSE         1 = run the calibration / weight-fitting step on every run (default: late run only)
  LOG_WINDOW_MIN  minutes before the off to log a race's inputs, default 35 (use 65 if you run hourly)
  WIN_FOCUS       1 (default) = BETS_TO_PLACE backs only the model's top-rated horse in a race, aiming
                  for winners rather than value; 0 = the older value-edge selection
  MIN_WIN_PROB    win-focus: minimum model win probability, default 0.25
  MAX_WIN_DEC     win-focus: maximum decimal price, default 5.0 (4/1)
  MAX_FIELD       win-focus: skip races with more declared runners than this, default 12
  BEST_RULE       prob (default) = BEST_PER_RACE picks the horse most likely to win; edge = biggest value edge
  DEBUG_DUMP      set to 1 to save fetched pages into ./debug (uploaded by the workflow)
"""
import datetime as dt
import difflib
import json
import math
import os
import re
import sys
import time
from collections import defaultdict
from fractions import Fraction
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
MIN_EV = float(os.environ.get("MIN_EV") or 0.0)
MIN_EV_SET = bool(os.environ.get("MIN_EV"))
WIN_FOCUS = (os.environ.get("WIN_FOCUS") or "1") != "0"
MIN_WIN_PROB = float(os.environ.get("MIN_WIN_PROB") or 0.25)
MAX_WIN_DEC = float(os.environ.get("MAX_WIN_DEC") or 5.0)
MAX_FIELD = int(os.environ.get("MAX_FIELD") or 12)
BEST_RULE = (os.environ.get("BEST_RULE") or "prob").strip().lower()
PLACE_RULES_V = "2"                # bumped when place settlement changed; older rows are re-checked
AUTO_FILTER = (os.environ.get("AUTO_FILTER") or "1") != "0"
USE_FITTED = (os.environ.get("USE_FITTED_WEIGHTS") or "0") == "1"
ANALYSE = (os.environ.get("ANALYSE") or "0") == "1"
MIN_SEG_N = 40                     # settled bets needed before a segment can be switched off
MIN_FIT_RACES = 300                # logged, settled races needed before fitting weights
LOG_WINDOW_MIN = int(os.environ.get("LOG_WINDOW_MIN") or 35)   # log a race's inputs once this close to the off
DEBUG = os.environ.get("DEBUG_DUMP") == "1"

MIN_DEC, MAX_DEC = 1.8, 13.0       # price window for selections (decimal odds)
# Starting weights for the form model (logit units). Guesses, to be tuned from the tracker.
W = {"market": 1.1, "form": 0.7, "rating": 0.20, "trainer": 0.12, "jockey": 0.08,
     "cd": 0.15, "layoff": 0.15, "move": 0.8, "move1h": 0.6, "wwr": 0.08, "course": 0.06, "bf": 0.0}
FEATURES = ["form", "rating", "trainer", "jockey", "cd", "layoff", "move", "move1h",
            "wwr", "course", "bf"]
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


def frac_text(dec):
    """Decimal odds -> readable fractional text, e.g. 3.5 -> 5/2."""
    if not dec or dec <= 1:
        return ""
    f = Fraction(dec - 1).limit_denominator(20)
    return "evens" if f == 1 else f"{f.numerator}/{f.denominator}"


def place_terms(field, handicap):
    """(places paid, fraction of win odds), standard UK/IRE each-way terms:
    2-4 runners: win only; 5-7 runners: 2 places; 8+ runners: 3 places;
    handicaps with 16+ runners: 4 places. Fraction is 1/4 for 5-7 runners and big handicaps, else 1/5."""
    if field <= 4:
        return 1, 1.0            # no place market, treat as winner only
    if field <= 7:
        return 2, 0.25
    if handicap and field >= 16:
        return 4, 0.25
    if handicap and field >= 12:
        return 3, 0.25
    return 3, 0.2


def to_float(x, default=None):
    try:
        if isinstance(x, str) and x.strip().endswith("%"):
            return float(x.strip()[:-1]) / 100        # a cell someone formatted as a percentage
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
WWR_RE = re.compile(r"Won When Rated\s+([\d\-]+)")
WEIGHT_RE = re.compile(r"\b\d{1,2},[a-z]+(?: [a-z]+)?\s+(\d{1,2})-(\d{1,2})\b")
PRICE_ONLY_RE = re.compile(r"^(?:\d{1,3}\s*/\s*\d{1,3}|evens|evs)$", re.I)
FORM_WORDS = re.compile(r"^[0-9PFURBDSCO\-/]+$")


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


def parse_course_form(lines):
    """'Course Form for X' table -> {norm(horse): (wins, runs)}."""
    out, name, nums, on = {}, None, [], False
    for ln in lines:
        if not on:
            on = bool(re.match(r"Course Form for", ln, re.I))
            continue
        if re.match(r"(Subscribe|Latest News|Collateral|\[?Top\b)", ln):
            break
        if re.fullmatch(r"\d+", ln):
            nums.append(int(ln))
            if len(nums) == 2 and name:
                out[norm(display_name(name))] = (nums[0], nums[1])
                name, nums = None, []
        elif RUN_DATE_RE.match(ln) or ln in ("Horse", "Wins", "Runs"):
            continue
        else:
            name, nums = ln, []
    return out


def parse_odds_compare(html, known=()):
    """Odds Compare page -> {norm(horse): {"best": dec, "median": dec, "books": n}}.
    Uses the bookmaker columns only (tote and any wild outlier prices are dropped)."""
    soup = BeautifulSoup(html, "html.parser")
    out = {}
    for table in soup.find_all("table"):
        header, rows = None, []
        for tr in table.find_all("tr"):
            cells = [clean(c.get_text(" ", strip=True)) for c in tr.find_all(["td", "th"])]
            if not cells:
                continue
            if header is None and any(c.lower() == "bet365" for c in cells):
                header = [c.lower() for c in cells]
            else:
                rows.append(cells)
        if header is None:
            continue
        for cells in rows:
            j = next((i for i, c in enumerate(cells) if i >= 1 and norm(display_name(c)) in known), None)
            if j is None:
                j = next((i for i, c in enumerate(cells)
                          if i >= 1 and re.search(r"[A-Za-z]{3}", c) and not FORM_WORDS.match(c)
                          and not PRICE_ONLY_RE.match(c)), None)
            if j is None:
                continue
            decs = []
            for i in range(j + 2, len(cells)):          # j+1 is the opening/summary cell
                if i < len(header) and header[i] in ("tote", ""):
                    continue
                if PRICE_ONLY_RE.match(cells[i]):
                    decs.append(parse_odds(cells[i])[1])
            decs = sorted(d for d in decs if d)
            if len(decs) >= 4:
                med = decs[len(decs) // 2]
                decs = [d for d in decs if med / 1.6 <= d <= med * 1.6]
            if len(decs) >= 3:
                med = decs[len(decs) // 2]
                out[norm(display_name(cells[j]))] = {"best": max(decs), "median": med, "books": len(decs)}
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
    f = {"rating": None, "form_score": None, "runs_n": 0, "days_off": None, "cd": 0.0,
         "bf": 0.0, "wwr_gap": None, "weight_lb": None}
    m = RATED_RE.search(pre)
    if m:
        f["rating"] = int(m.group(1))
    wm = WWR_RE.search(pre)
    if wm and f["rating"]:
        won = [int(x) for x in wm.group(1).split("-") if x.isdigit()]
        if won:
            f["wwr_gap"] = (max(won) - f["rating"]) / 10.0   # >0: rated below its best winning mark
    wt = WEIGHT_RE.search(pre)
    if wt:
        f["weight_lb"] = int(wt.group(1)) * 14 + int(wt.group(2))
    sm = SYMBOLS_RE.search(pre)
    if sm:
        toks = [re.sub(r"\d", "", t) for t in sm.group(2).split()]
        if "bf" in toks:
            f["bf"] = 1.0
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
    course_form = parse_course_form(lines)
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
            "jockey_rate": smooth_rate(*jw) if jw else None,
            "course_wins": course_form.get(b["norm"], (None, None))[0],
            "course_runs": course_form.get(b["norm"], (None, None))[1], **f})
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
        books = {}
        ohtml = get(url.replace("/racecards/", "/odds-comparison/"))
        if ohtml:
            books = parse_odds_compare(ohtml, {e["norm"] for e in entries})
            if sum(1 for e in entries if e["norm"] in books) < 0.8 * max(len(entries), 1):
                books = {}                 # patchy coverage would mix two price sources in one race
        for e in entries:
            b = books.get(e["norm"])
            e["dec_sp"] = e["dec"]
            e["dec_best"], e["books"], e["src"] = None, 0, "p"
            if b:
                e["dec"], e["dec_best"], e["books"], e["src"] = b["median"], b["best"], b["books"], "b"
                e["odds"] = frac_text(b["median"])
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
                      "confidence": round(conf, 2), "flag": flag,
                      "price_src": "bookmakers" if books else "probable SP"})
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
                            "trainer_rate": e["trainer_rate"], "jockey_rate": e["jockey_rate"],
                            "dec_sp": e["dec_sp"], "dec_best": e["dec_best"], "books": e["books"],
                            "src": e["src"], "declared": declared, "handicap": rinfo["handicap"],
                            "wwr_gap": e["wwr_gap"], "course_wins": e["course_wins"],
                            "course_runs": e["course_runs"], "bf": e["bf"],
                            "weight_lb": e["weight_lb"]})
    return races, runners, per_meeting, info


# ----------------------------------------------------------------------------
# Market movers
# ----------------------------------------------------------------------------
def snap_key(date, course, hhmm, horse):
    return f"{date}|{norm(course)}|{hhmm}|{norm(horse)}"


def _same_src(h, src):
    return src is None or (h[2] if len(h) > 2 else "p") == src


def pick_ref(hist, now, target_min=120, lo=MOVER_MIN_AGE_MIN, hi=300, src=None):
    """Snapshot closest to `target_min` minutes ago, within [lo, hi] minutes old."""
    best = None
    for h in hist:
        if not _same_src(h, src):
            continue
        age = (now - h[0]).total_seconds() / 60
        if lo <= age <= hi:
            d = abs(age - target_min)
            if best is None or d < best[0]:
                best = (d, h[1])
    return best[1] if best else None


def pick_open(hist, now, lo=MOVER_MIN_AGE_MIN, src=None):
    """Earliest snapshot of the day that is at least `lo` minutes old."""
    old = [(h[0], h[1]) for h in hist
           if _same_src(h, src) and (now - h[0]).total_seconds() / 60 >= lo]
    return min(old)[1] if old else None


def pick_night_best(hist, race_day):
    """Best bookmaker price from the last snapshot taken the evening before the race, else None."""
    cand = [h for h in hist if len(h) > 3 and h[3] and h[2] == "b" and h[0].date() < race_day]
    return max(cand, key=lambda h: h[0])[3] if cand else None


def add_movers(runners, snaps, now):
    for r in runners:
        hist = snaps.get(snap_key(r["date"], r["course"], r["time"], r["horse"]), [])
        r["night_best"] = pick_night_best(hist, dt.date.fromisoformat(r["date"]))
        src = r.get("src", "p")
        ref1 = pick_ref(hist, now, 60, MOVER_MIN_AGE_MIN, 150, src)
        ref2, refo = pick_ref(hist, now, src=src), pick_open(hist, now, src=src)
        r["mover_1h_pct"] = round((r["dec"] - ref1) / ref1 * 100, 1) if ref1 else None
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


ODDS_LABELS = ["<1.5/1", "1.5/1-4/1", "4/1-9/1", "9/1+"]
FIELD_LABELS = ["<8", "8-12", "13+"]
LABEL = {"cd": "c&d"}


def race_features(field):
    """Per-runner feature values for one race (centred on the race average where that
    makes sense, clipped). The model is: log(market) + sum(weight * feature), then softmax."""
    forms = centered([r.get("form_score") for r in field])
    ratings = centered([r.get("rating") for r in field])
    trates = centered([r.get("trainer_rate") for r in field])
    jrates = centered([r.get("jockey_rate") for r in field])
    out = []
    for r, fc, rc, tc, jc in zip(field, forms, ratings, trates, jrates):
        move = r.get("mover_night_pct")
        if move is None:
            move = r.get("mover_2h_pct")
        m1 = r.get("mover_1h_pct")
        wg = r.get("wwr_gap")
        cr, cw = r.get("course_runs"), r.get("course_wins")
        out.append({
            "form": fc,
            "rating": clip(rc / 10, -3, 3),
            "trainer": clip(tc / 0.10, -2, 2),
            "jockey": clip(jc / 0.10, -2, 2),
            "cd": r.get("cd") or 0.0,
            "layoff": layoff_term(r.get("days_off")),
            "move": min(0.3, max(0.0, -(move or 0.0)) / 100),
            "move1h": min(0.25, max(0.0, -(m1 or 0.0)) / 100),
            "wwr": clip(wg, -2, 2) if wg is not None else 0.0,
            "course": clip(((cw + 0.24) / (cr + 2) - 0.12) / 0.10, -2, 2) if cr else 0.0,
            "bf": r.get("bf") or 0.0,
        })
    return out


def add_model(runners):
    """Market probability (bias corrected) nudged by form, rating, trainer/jockey form,
    course/distance record, time off, price moves and more, renormalised per race."""
    by_race = defaultdict(list)
    for r in runners:
        by_race[(r["date"], r["course"], r["time"])].append(r)
    for field in by_race.values():
        raw = [1.0 / r["dec"] for r in field]
        tot = sum(raw)
        fair = [p / tot for p in raw]
        feats = race_features(field)
        logits = []
        for r, f, ft in zip(field, fair, feats):
            terms = {k: W[k] * ft[k] for k in FEATURES}
            r["fair_prob"], r["_terms"] = f, terms
            logits.append(W["market"] * math.log(f) + sum(terms.values()))
        mx = max(logits)
        ex = [math.exp(x - mx) for x in logits]
        s = sum(ex)
        for r, e in zip(field, ex):
            r["model_prob"] = e / s
            r["value_edge"] = r["model_prob"] - r["fair_prob"]
            price = r.get("dec_best") or r["dec"]                 # best bookmaker price when we have it
            r["ev"] = r["model_prob"] * price - 1
            r["shorten_score"] = round(max(0.0, -(r.get("mover_2h_pct") or 0.0))
                                       + 0.5 * max(0.0, -(r.get("mover_night_pct") or 0.0)), 1)
            top = sorted(r.pop("_terms").items(), key=lambda kv: -kv[1])[:2]
            r["reason"] = ", ".join(f"{LABEL.get(k, k)} {v:+.2f}" for k, v in top if v >= 0.03)
        for i, r in enumerate(sorted(field, key=lambda x: -x["model_prob"])):
            r["model_rank"], r["field_n"] = i + 1, len(field)


def runner_segments(r):
    return ["Region: " + str(r["region"]),
            "Odds " + band(r["dec"], [2.5, 5, 10], ODDS_LABELS),
            "Field " + band(r.get("declared") or 0, [8, 13], FIELD_LABELS),
            "Race " + ("flagged" if r.get("flag") else "OK")]


def row_segments(row):
    """Segment labels for a settled tracker row (the same families as runner_segments)."""
    return ["Region: " + str(row.get("region")),
            "Odds " + band(to_float(row.get("sp_dec"), 0), [2.5, 5, 10], ODDS_LABELS),
            "Field " + band(to_float(row.get("field_size"), 0), [8, 13], FIELD_LABELS),
            "Race " + ("flagged" if row.get("race_flag") else "OK")]


def blocked_segments(rows, min_n=MIN_SEG_N):
    """Segments the tracker says lose: 40+ settled bets, win ROI below -10% AND average CLV below -2%."""
    groups = defaultdict(list)
    for r in rows:
        if r.get("status") == "SETTLED":
            for lab in row_segments(r):
                groups[lab].append(r)
    out = {}
    for lab, g in groups.items():
        if len(g) < min_n:
            continue
        roi = sum(to_float(r["pnl_win_units"], 0) for r in g) / len(g) * 100
        clvs = [c for c in (to_float(r.get("clv_pct")) for r in g) if c is not None]
        if clvs and roi < -10 and sum(clvs) / len(clvs) < -2:
            out[lab] = (len(g), round(roi, 1), round(sum(clvs) / len(clvs), 1))
    return out


def choose_bets(runners, blocked=()):
    """BETS_TO_PLACE. WIN_FOCUS (default): only the model's top-rated horse in a race, with a real win
    chance (>= MIN_WIN_PROB), a short enough price, no poor recent form and no long layoff. Otherwise the
    older rule: biggest value edge above MIN_EDGE inside the price window."""
    best = {}
    for r in runners:
        if WIN_FOCUS:
            ok = (r.get("model_rank") == 1 and r["model_prob"] >= MIN_WIN_PROB
                  and 1.5 <= r["dec"] <= MAX_WIN_DEC and r["value_edge"] >= 0
                  and (not r.get("declared") or r["declared"] <= MAX_FIELD)
                  and (r.get("form_score") is None or r["form_score"] >= 0.40)
                  and (r.get("days_off") is None or r["days_off"] <= 90))
            if ok and MIN_EV_SET and r.get("dec_best") and r["ev"] < MIN_EV:
                ok = False
            rank_key = lambda x: x["model_prob"]
        else:
            ok = r["value_edge"] >= MIN_EDGE and MIN_DEC <= r["dec"] <= MAX_DEC
            if ok and r.get("dec_best") and r["ev"] < MIN_EV:
                ok = False                              # no value even at the best bookmaker price
            rank_key = lambda x: x["value_edge"]
        if not ok:
            continue
        if STRICT and (r.get("flag") or (r.get("runs_n") or 0) < 2):
            continue                                    # big-field handicaps, unraced fields, debutants
        if blocked and set(runner_segments(r)) & set(blocked):
            continue                                    # segment the tracker shows is losing
        k = (r["date"], r["course"], r["time"])
        if k not in best or rank_key(r) > rank_key(best[k]):
            best[k] = r                                 # one selection per race
    score = (lambda x: -x["model_prob"]) if WIN_FOCUS else (lambda x: -x["value_edge"])
    picks, per_meet = [], defaultdict(int)
    for r in sorted(best.values(), key=score):
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


def choose_best_per_race(runners, rule=None):
    """One pick in every race (needs 3+ priced runners). rule 'prob' (default): the horse most likely to win
    on the model, which gives the most winners; rule 'edge': the biggest value edge over the market."""
    rule = rule or BEST_RULE
    if rule == "edge":
        key = lambda x: (x["value_edge"], -x["dec"])
    else:
        key = lambda x: (x["model_prob"], -x["dec"])
    picks = [max(f, key=key) for f in _by_race(runners).values() if len(f) >= 3]
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


def pick_fields(b, rule=""):
    fs = b.get("form_score")
    best = b.get("dec_best")
    price = best or b["dec"]
    return {"odds_at_pick": frac_text(price) if best else b["odds"], "dec_at_pick": price,
            "horse": b["horse"], "model_prob": round(b["model_prob"], 4),
            "value_edge": round(b["value_edge"], 4), "region": b["region"],
            "form_score": round(fs, 2) if fs is not None else "",
            "confidence": b.get("confidence", ""), "reason": b.get("reason", ""),
            "race_flag": b.get("flag", ""),
            "price_src": f"best of {b.get('books')} books" if best else "probable SP",
            "declared": b.get("declared", ""), "night_dec": b.get("night_best") or "",
            "pick_rule": rule}


def upsert_race_bests(rows, picks, rule=""):
    """One row per race. A PENDING row is re-picked on each run until the race is off
    (the scrape only sees races still to run); settled or started races are left alone."""
    by_key = {r["key"]: r for r in rows}
    added = 0
    for b in picks:
        key = f"{b['date']}|{norm(b['course'])}|{b['time']}"
        vals = pick_fields(b, rule)
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
                "form_score", "confidence", "reason", "race_flag", "clv_pct", "price_src",
                "declared", "rules_v", "night_dec", "night_clv_pct", "pick_rule", "nr_note"]


def field_count(entries, declared=None):
    """How many horses actually ran. Only rows that carry a result position (1st, 2nd, PU, F ...) count,
    so other horse links on the page cannot inflate the field, and it can never exceed the declared runners."""
    runners = [e for e in entries if not e.get("nr")]
    with_pos = [e for e in runners if e.get("pos") is not None]
    n = len(with_pos) if len(with_pos) >= 2 else len(runners)
    d = to_float(declared)
    if d and d >= 2:
        n = min(n, int(d))
    return n


def settle_row(row, finishers, handicap, now):
    """Fill result columns on a tracker row. finishers = parse_result() entries in page order.
    A horse that is not on a result page that clearly lists the finishers is a NON-RUNNER (stake returned)."""
    runners = [e for e in finishers if not e.get("nr")]
    names = [e["norm"] for e in runners]
    target = norm(row["horse"])
    if target not in names:
        if sum(1 for e in runners if e.get("pos") is not None) < 3:
            return                              # page not clear enough to say: try again next run
        close = difflib.get_close_matches(target, names, n=1, cutoff=0.88)
        if close:
            target = close[0]                   # same horse, spelled slightly differently
        else:
            best, ratio = "", 0.0
            for e in runners:
                q = difflib.SequenceMatcher(None, target, e["norm"]).ratio()
                if q > ratio:
                    best, ratio = e["name"], q
            row.update({"status": "NON-RUNNER", "pnl_win_units": 0, "pnl_place_units": 0,
                        "won": "", "placed": "", "rules_v": PLACE_RULES_V,
                        "nr_note": f"not on the result page; closest name there: {best} ({ratio:.2f})",
                        "settled_at": now.isoformat(timespec="minutes")})
            return
    i = names.index(target)
    e = runners[i]
    field = field_count(finishers, row.get("declared"))
    pos = e["pos"] if e.get("pos") is not None else i + 1
    places, frac = place_terms(field, handicap)
    sp_dec = e["dec"] or to_float(row.get("dec_at_pick"), 2.0)
    won = pos == 1
    placed = pos <= places
    place_odds = (sp_dec - 1) * frac + 1
    pick_dec = to_float(row.get("dec_at_pick"))
    night_dec = to_float(row.get("night_dec"))
    clv = round((pick_dec / sp_dec - 1) * 100, 1) if pick_dec and sp_dec else ""
    nclv = round((night_dec / sp_dec - 1) * 100, 1) if night_dec and sp_dec else ""
    row.update({"clv_pct": clv, "night_clv_pct": nclv, "rules_v": PLACE_RULES_V,
        "status": "SETTLED", "position": pos if pos != 99 else "DNF", "field_size": field,
        "places_paid": places, "sp": e["odds"] or "", "sp_dec": round(sp_dec, 3),
        "won": 1 if won else 0, "placed": 1 if placed else 0,
        "pnl_win_units": round(sp_dec - 1 if won else -1, 3),
        "pnl_place_units": round(place_odds - 1 if placed else -1, 3),
        "settled_at": now.isoformat(timespec="minutes"),
    })


def recheck_places(rows, now, limit=150):
    """One-off repair: rows settled before PLACE_RULES_V that finished 2nd-4th are re-settled from the
    result page with the corrected field size / place terms. A row is only changed if the re-settle works."""
    todo, done = [], 0
    for r in rows:
        if r.get("status") == "NON-RUNNER" and not r.get("nr_note"):
            todo.append(r)                      # older non-runners: re-test with the name fallback, add a note
            continue
        if r.get("status") != "SETTLED" or str(r.get("rules_v")) == PLACE_RULES_V:
            continue
        pos = to_float(r.get("position"))
        if pos in (2.0, 3.0, 4.0):
            todo.append(r)
        else:
            r["rules_v"] = PLACE_RULES_V       # a win or an unplaced finish cannot change
    by_day = defaultdict(list)
    for r in todo[:limit]:
        by_day[r["date"]].append(r)
    for date_s, group in by_day.items():
        try:
            day = dt.date.fromisoformat(date_s)
        except ValueError:
            continue
        needed = {(norm(r["course"]), r["time"]): r["course"] for r in group}
        results = fetch_results_for(day, needed)
        for r in group:
            res = results.get((norm(r["course"]), r["time"]))
            if res is None:
                if now > race_off(day, r["time"]) + dt.timedelta(days=21):
                    r["rules_v"] = PLACE_RULES_V          # page long gone: keep what we had
                    if r.get("status") == "NON-RUNNER" and not r.get("nr_note"):
                        r["nr_note"] = "result page no longer available"
                continue
            trial = dict(r)
            settle_row(trial, res[0], res[1], now)
            if trial.get("status") == "SETTLED":
                r.update(trial)
                done += 1
            elif trial.get("status") == "NON-RUNNER":
                r.update(trial)
            elif r.get("status") == "NON-RUNNER":
                r["nr_note"] = "result page not clear enough to check"
    return done


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
            "Win P&L (units)", "Win ROI %", "Place P&L (units)", "Place ROI %", "Avg CLV %",
            "Avg night CLV %"]


PCT_SUMMARY_COLS = ["Win %", "Place %", "Win ROI %", "Place ROI %", "Avg CLV %", "Avg night CLV %"]


def stats_row(name, g):
    """Rates are stored as fractions (0.25 = 25%); the sheet shows them with a % format."""
    n = len(g)
    if n == 0:
        return [name, 0, 0, "", 0, "", 0, "", 0, "", "", ""]
    w = sum(int(to_float(r["won"], 0)) for r in g)
    p = sum(int(to_float(r["placed"], 0)) for r in g)
    wp = sum(to_float(r["pnl_win_units"], 0) for r in g)
    pp = sum(to_float(r["pnl_place_units"], 0) for r in g)
    clvs = [c for c in (to_float(r.get("clv_pct")) for r in g) if c is not None]
    nclvs = [c for c in (to_float(r.get("night_clv_pct")) for r in g) if c is not None]
    return [name, n, w, round(w / n, 4), p, round(p / n, 4),
            round(wp, 2), round(wp / n, 4), round(pp, 2), round(pp / n, 4),
            round(sum(clvs) / len(clvs) / 100, 4) if clvs else "",
            round(sum(nclvs) / len(nclvs) / 100, 4) if nclvs else ""]


def summarise(rows, fav_rows=None):
    """Win/place/ROI/CLV by segment. With fav_rows, adds a 'favourite in the same races' baseline."""
    settled = [r for r in rows if r.get("status") == "SETTLED"]
    groups = {"ALL BETS": settled}
    for r in settled:
        edge = to_float(r.get("value_edge"), 0)
        dec = to_float(r.get("sp_dec"), 0)
        groups.setdefault("Region: " + str(r["region"]), []).append(r)
        groups.setdefault("Edge " + band(edge, [0.03, 0.05], ["<3%", "3-5%", "5%+"]), []).append(r)
        groups.setdefault("Odds " + band(dec, [2.5, 5, 10], ODDS_LABELS), []).append(r)
        fs = to_float(r.get("field_size"), 0)
        groups.setdefault("Field " + band(fs, [8, 13], FIELD_LABELS), []).append(r)
        groups.setdefault("Race " + ("flagged" if r.get("race_flag") else "OK"), []).append(r)
        groups.setdefault("Pick rule: " + str(r.get("pick_rule") or "edge"), []).append(r)
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
        if d["status"] == "UNMATCHED":
            d["status"] = "NON-RUNNER"          # horses missing from the result are non-runners
        if d["key"]:
            rows.append(d)
    return rows


def save_tracker(sh, rows, tab="BET_TRACKER"):
    write_tab(sh, tab, TRACKER_COLS, [[r.get(c, "") for c in TRACKER_COLS] for r in rows])


def load_snapshots(sh, day):
    """{key: [(ts, dec, src)]} for the day, pruning the log when it grows large."""
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
                (dt.datetime.fromisoformat(v[0]), float(v[5]), v[6] if len(v) > 6 and v[6] else "p",
                 to_float(v[7]) if len(v) > 7 else None))
        except ValueError:
            continue
    if len(vals) > 30000:
        write_tab(sh, "MARKET_SNAPSHOTS_LOG", SNAP_HEAD, keep)
    return snaps, max(0, len(vals) - 1)


SNAP_HEAD = ["ts", "date", "course", "time", "horse", "dec_price", "src", "dec_best"]


R_HEAD = ["date", "course", "region", "time", "horse", "odds", "dec", "fair_prob", "model_prob",
          "value_edge", "ev", "mover_2h_pct", "mover_night_pct", "form_score", "rating",
          "days_off", "trainer_rate", "jockey_rate", "confidence", "race_flag", "reason",
          "best_price", "books", "mover_1h_pct", "wwr_gap", "course_wins", "course_runs",
          "night_price"]
BEST_HEAD = ["date", "time", "course", "region", "horse", "odds", "night_price", "model_prob",
             "value_edge", "form_score", "confidence", "reason", "race_flag", "status", "position",
             "field_size", "places_paid", "sp", "clv_pct", "night_clv_pct", "won", "placed",
             "pnl_win_units", "pnl_place_units", "pick_rule"]

HIGHLIGHT_EDGE = 0.05                     # rows with an edge of 5% or more are highlighted green
HIGHLIGHT_RGB = {"red": 0.78, "green": 0.94, "blue": 0.81}
RUNNER_PCT = ["fair_prob", "model_prob", "value_edge", "ev", "trainer_rate", "jockey_rate", "confidence"]
# tab -> (header, column holding the edge (or (model, market) columns), columns shown as %)
# The trackers are read back by this script, so they are highlighted but their numbers are left as they are.
def format_specs():
    return {
        "RUNNERS_TARGET": (R_HEAD, "value_edge", RUNNER_PCT),
        "SIGNALS": (R_HEAD + ["shorten_score"], "value_edge", RUNNER_PCT),
        "VALUE_BETS_TARGET": (R_HEAD, "value_edge", RUNNER_PCT),
        "BETS_TO_PLACE": (R_HEAD, "value_edge", RUNNER_PCT),
        "BEST_PER_RACE": (BEST_HEAD, "value_edge", ["model_prob", "value_edge", "confidence"]),
        "BET_TRACKER": (TRACKER_COLS, "value_edge", []),
        "RACE_BEST_TRACKER": (TRACKER_COLS, "value_edge", []),
        "FAV_BASELINE_TRACKER": (TRACKER_COLS, "value_edge", []),
        "RUNNER_LOG": (RL_COLS, ("model_prob", "fair_prob"), []),
        "RACE_BEST_SUMMARY": (SUM_HEAD, None, PCT_SUMMARY_COLS),
        "TRACKER_SUMMARY": (SUM_HEAD, None, PCT_SUMMARY_COLS),
    }


def col_letter(i):
    out = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        out = chr(65 + r) + out
    return out


def edge_formula(head, edge):
    if isinstance(edge, tuple):
        a, b = (col_letter(head.index(x)) for x in edge)
        return f"=AND(ISNUMBER(${a}2),ISNUMBER(${b}2),${a}2-${b}2>={HIGHLIGHT_EDGE})"
    c = col_letter(head.index(edge))
    return f"=AND(ISNUMBER(${c}2),${c}2>={HIGHLIGHT_EDGE})"


def formatting_requests(meta_sheets, specs=None):
    """Batch-update requests that (a) highlight rows with edge >= 5% and (b) show rate columns as %.
    Safe to run every time: an existing identical rule is left alone and stale ones are replaced."""
    specs = format_specs() if specs is None else specs
    dele, add = [], []
    for sh_meta in meta_sheets:
        props = sh_meta.get("properties", {})
        title, sid = props.get("title"), props.get("sheetId")
        if title not in specs:
            continue
        head, edge, pct_cols = specs[title]
        existing = sh_meta.get("conditionalFormats", []) or []
        if edge is not None:
            want = edge_formula(head, edge)
            have = False
            for idx, cf in enumerate(existing):
                vals = ((cf.get("booleanRule") or {}).get("condition") or {}).get("values") or [{}]
                f = vals[0].get("userEnteredValue", "")
                if f == want:
                    have = True
                elif f.startswith("=AND(ISNUMBER(") and f.endswith(f">={HIGHLIGHT_EDGE})"):
                    dele.append({"deleteConditionalFormatRule": {"sheetId": sid, "index": idx}})
            if not have:
                add.append({"addConditionalFormatRule": {"index": 0, "rule": {
                    "ranges": [{"sheetId": sid, "startRowIndex": 1, "startColumnIndex": 0,
                                "endColumnIndex": len(head)}],
                    "booleanRule": {"condition": {"type": "CUSTOM_FORMULA",
                                                  "values": [{"userEnteredValue": want}]},
                                    "format": {"backgroundColor": HIGHLIGHT_RGB}}}}})
        for name in pct_cols:
            c = head.index(name)
            add.append({"repeatCell": {
                "range": {"sheetId": sid, "startRowIndex": 1, "startColumnIndex": c, "endColumnIndex": c + 1},
                "cell": {"userEnteredFormat": {"numberFormat": {"type": "PERCENT", "pattern": "0.0%"}}},
                "fields": "userEnteredFormat.numberFormat"}})
    # delete highest index first so earlier deletions do not shift the later ones
    dele.sort(key=lambda r: (r["deleteConditionalFormatRule"]["sheetId"], -r["deleteConditionalFormatRule"]["index"]))
    return dele + add


def apply_formatting(sh):
    meta = sh.fetch_sheet_metadata()
    reqs = formatting_requests(meta.get("sheets", []))
    if reqs:
        sh.batch_update({"requests": reqs})


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
# Runner log: every runner's inputs and result, for calibration and weight fitting
# ----------------------------------------------------------------------------
RL_COLS = ["key", "date", "course", "time", "region", "horse", "logged_at", "dec_market", "dec_best",
           "books", "src", "fair_prob", "model_prob", "form_score", "rating", "days_off",
           "trainer_rate", "jockey_rate", "cd", "runs_n", "wwr_gap", "course_wins", "course_runs",
           "bf", "weight_lb", "mover_1h_pct", "mover_2h_pct", "mover_night_pct", "flag",
           "handicap", "declared"]
RR_COLS = ["key", "position", "field_size", "places_paid", "sp", "sp_dec", "won", "placed",
           "status", "settled_at"]
LOG_NUM = ["dec_market", "dec_best", "fair_prob", "model_prob", "form_score", "rating", "days_off",
           "trainer_rate", "jockey_rate", "cd", "runs_n", "wwr_gap", "course_wins", "course_runs",
           "bf", "mover_1h_pct", "mover_2h_pct", "mover_night_pct", "declared"]


def runner_key(r):
    return f"{r['date']}|{norm(r['course'])}|{r['time']}|{r['norm']}"


def runner_log_rows(runners, now, logged):
    """Rows for races that are close to the off and not logged yet (one row per runner)."""
    rows = []
    for r in runners:
        if (r["off"] - now).total_seconds() / 60 > LOG_WINDOW_MIN:
            continue
        key = runner_key(r)
        if key in logged:
            continue
        logged.add(key)
        rows.append([key, r["date"], r["course"], r["time"], r["region"], r["horse"],
                     now.isoformat(timespec="minutes"), r["dec"], r.get("dec_best"), r.get("books"),
                     r.get("src"), r["fair_prob"], r["model_prob"], r.get("form_score"),
                     r.get("rating"), r.get("days_off"), r.get("trainer_rate"), r.get("jockey_rate"),
                     r.get("cd"), r.get("runs_n"), r.get("wwr_gap"), r.get("course_wins"),
                     r.get("course_runs"), r.get("bf"), r.get("weight_lb"), r.get("mover_1h_pct"),
                     r.get("mover_2h_pct"), r.get("mover_night_pct"), r.get("flag"),
                     r.get("handicap"), r.get("declared")])
    return rows


def settle_log_races(races, now):
    """races = {(date, course, time): [{key, horse}]} -> RUNNER_RESULTS rows for finished races."""
    iso = now.isoformat(timespec="minutes")
    by_day = defaultdict(dict)
    for (date, course, hhmm) in races:
        if now >= race_off(dt.date.fromisoformat(date), hhmm) + dt.timedelta(minutes=25):
            by_day[date][(norm(course), hhmm)] = course
    results = {}
    for date, needed in by_day.items():
        for k, v in fetch_results_for(dt.date.fromisoformat(date), needed).items():
            results[(date,) + k] = v
    out = []
    for (date, course, hhmm), items in races.items():
        off = race_off(dt.date.fromisoformat(date), hhmm)
        if now < off + dt.timedelta(minutes=25):
            continue
        res = results.get((date, norm(course), hhmm))
        if res is None or len(res[0]) < 2 or not any(e["pos"] is not None or e["dec"] for e in res[0]):
            if now > off + dt.timedelta(hours=48):
                out += [[it["key"], "", "", "", "", "", "", "", "NO_RESULT", iso] for it in items]
            continue
        entries, handicap = res
        ran = [e for e in entries if not e.get("nr")]
        field = field_count(entries)
        places, _ = place_terms(field, handicap)
        found = {e["norm"]: (i, e) for i, e in enumerate(ran)}
        for it in items:
            hit = found.get(norm(it["horse"]))
            if not hit:
                out.append([it["key"], "NR", field, places, "", "", 0, 0, "NR", iso])
                continue
            i, e = hit
            pos = e["pos"] if e.get("pos") is not None else i + 1
            out.append([it["key"], "DNF" if pos == 99 else pos, field, places, e["odds"] or "",
                        e["dec"] or "", 1 if pos == 1 else 0, 1 if pos <= places else 0,
                        "SETTLED", iso])
    return out


def read_tab_dicts(sh, tab):
    vals = get_tab(sh, tab).get_all_values()
    if len(vals) < 2:
        return []
    head = vals[0]
    return [{h: (v[i] if i < len(v) else "") for i, h in enumerate(head)} for v in vals[1:]]


def settle_runner_log(sh, now):
    done = set(get_tab(sh, "RUNNER_RESULTS").col_values(1)[1:])
    cutoff = (now.date() - dt.timedelta(days=4)).isoformat()
    races = defaultdict(list)
    for r in read_tab_dicts(sh, "RUNNER_LOG"):
        if r["date"] >= cutoff and r["key"] not in done:
            races[(r["date"], r["course"], r["time"])].append({"key": r["key"], "horse": r["horse"]})
    rows = settle_log_races(races, now)
    append_rows(sh, "RUNNER_RESULTS", RR_COLS, rows)
    return len(rows)


# ----------------------------------------------------------------------------
# Calibration + weight fitting (uses the runner log once it has results)
# ----------------------------------------------------------------------------
def join_log_results(log_rows, results):
    """Settled runners with numeric inputs; the latest log row per key wins."""
    latest = {r["key"]: r for r in log_rows}
    out = []
    for key, r in latest.items():
        res = results.get(key)
        if not res or res.get("status") != "SETTLED":
            continue
        d = {c: to_float(r.get(c)) for c in LOG_NUM}
        d.update({"key": key, "date": r["date"], "course": r["course"], "time": r["time"],
                  "horse": r["horse"], "region": r.get("region"), "flag": r.get("flag"),
                  "won": int(to_float(res.get("won"), 0)), "placed": int(to_float(res.get("placed"), 0)),
                  "sp_dec": to_float(res.get("sp_dec"))})
        d["dec"] = d["dec_market"]
        if d["dec"] and d["fair_prob"]:
            out.append(d)
    return out


def usable_races(rows):
    """Races with 3+ runners and exactly one winner: [(field, winner_index)], oldest first."""
    groups = defaultdict(list)
    for r in rows:
        groups[(r["date"], r["time"], r["course"])].append(r)
    races = []
    for k in sorted(groups):
        f = groups[k]
        wins = [i for i, r in enumerate(f) if r["won"] == 1]
        if len(f) >= 3 and len(wins) == 1:
            races.append((f, wins[0]))
    return races


def race_losses(races):
    """Mean -log(probability given to the winner): model vs market. Lower is better."""
    lm = lk = 0.0
    for f, w in races:
        mk = [1 / r["dec"] for r in f]
        md = [r["model_prob"] for r in f]
        lk += -math.log((mk[w]) / sum(mk))
        lm += -math.log(max(md[w], 1e-9) / sum(md))
    n = max(len(races), 1)
    return lm / n, lk / n


def calibration_rows(rows, races):
    bins = [0, 0.05, 0.10, 0.15, 0.20, 0.30, 0.50, 1.01]
    out = [["Calibration: did horses we rated X% win X% of the time?", "", "", "", "", ""],
           ["Model probability", "Runners", "Avg model %", "Avg market %", "Actual win %", "Gap (actual - model)"]]
    for lo, hi in zip(bins, bins[1:]):
        g = [r for r in rows if lo <= r["model_prob"] < hi]
        if not g:
            continue
        am = sum(r["model_prob"] for r in g) / len(g) * 100
        ak = sum(r["fair_prob"] for r in g) / len(g) * 100
        aw = sum(r["won"] for r in g) / len(g) * 100
        out.append([f"{lo*100:.0f}% to {min(hi, 1)*100:.0f}%", len(g), round(am, 1), round(ak, 1),
                    round(aw, 1), round(aw - am, 1)])
    lm, lk = race_losses(races) if races else (0, 0)
    out += [[], ["Does the model beat the market?", "", "", "", "", ""],
            ["Races used", len(races), "", "", "", ""],
            ["Model log loss (lower is better)", round(lm, 4), "", "", "", ""],
            ["Market log loss", round(lk, 4), "", "", "", ""],
            ["Verdict", "MODEL BEATS MARKET" if races and lm < lk else "NOT YET (or no data)", "", "", "", ""]]
    out += [[], ["Return from backing every runner with model edge >= 2%", "Bets", "Wins", "Win %", "ROI at SP %",
                 "ROI at best price %"]]
    bets = [r for r in rows if r["model_prob"] - r["fair_prob"] >= 0.02]
    if bets:
        sp = [(r["sp_dec"] or r["dec"]) for r in bets]
        best = [(r.get("dec_best") or r["dec"]) for r in bets]
        w = sum(r["won"] for r in bets)
        out.append(["Edge >= 2%", len(bets), w, round(w / len(bets) * 100, 1),
                    round(sum((p - 1) if r["won"] else -1 for p, r in zip(sp, bets)) / len(bets) * 100, 1),
                    round(sum((p - 1) if r["won"] else -1 for p, r in zip(best, bets)) / len(bets) * 100, 1)])
    favs = []
    for f, w in races:
        r = min(f, key=lambda x: x["dec"])
        favs.append(r)
    if favs:
        w = sum(r["won"] for r in favs)
        out.append(["Favourite in every race (baseline)", len(favs), w, round(w / len(favs) * 100, 1),
                    round(sum(((r["sp_dec"] or r["dec"]) - 1) if r["won"] else -1 for r in favs) / len(favs) * 100, 1), ""])
    return out


def fit_weights(races, w0, lam=10.0, iters=400, lr=1.0):
    """Conditional-logit fit of the model weights, pulled towards the current ones (ridge)
    so a small sample cannot throw them far. Returns (fitted dict, names)."""
    import numpy as np
    names = ["market"] + FEATURES
    R, M, K = len(races), max(len(f) for f, _ in races), len(names)
    X = np.zeros((R, M, K))
    mask = np.zeros((R, M), dtype=bool)
    y = np.zeros((R, M))
    for ri, (field, win) in enumerate(races):
        inv = [1 / r["dec"] for r in field]
        tot = sum(inv)
        for j, (r, ft) in enumerate(zip(field, race_features(field))):
            X[ri, j, 0] = math.log(inv[j] / tot)
            for k, n in enumerate(FEATURES):
                X[ri, j, k + 1] = ft[n]
            mask[ri, j] = True
        y[ri, win] = 1.0
    b0 = np.array([w0[n] for n in names], dtype=float)

    def probs(beta):
        z = np.where(mask, X @ beta, -1e9)
        z = z - z.max(axis=1, keepdims=True)
        e = np.exp(z) * mask
        return e / e.sum(axis=1, keepdims=True)

    def loss(beta, sel=slice(None)):
        p = probs(beta)[sel]
        return float(-np.log(np.maximum((p * y[sel]).sum(axis=1), 1e-12)).mean())

    def fit(sel):
        beta = b0.copy()
        for _ in range(iters):
            p = probs(beta)[sel]
            grad = np.einsum("rm,rmk->k", (y[sel] - p), X[sel]) / len(p) - 2 * lam * (beta - b0) / len(p)
            beta += lr * grad
        return beta

    # 5-fold cross-validation: every race is scored by a fit that never saw it, so the
    # comparison with the current weights is not at the mercy of one small hold-out set.
    folds = 5
    cv_loss = 0.0
    for k in range(folds):
        test_idx = np.arange(k, R, folds)
        train_idx = np.setdiff1d(np.arange(R), test_idx)
        beta = fit(train_idx)
        cv_loss += loss(beta, test_idx) * len(test_idx)
    cv_loss /= R
    full = fit(slice(0, R))
    lo = {n: (0.8 if n == "market" else -1.0) for n in names}
    hi = {n: (1.4 if n == "market" else 2.0) for n in names}
    fitted = {n: float(min(hi[n], max(lo[n], v))) for n, v in zip(names, full)}
    return {"fitted": fitted, "names": names, "test_loss_current": loss(b0),
            "test_loss_fitted": cv_loss, "races": R}


def run_analysis(sh, now):
    """Writes MODEL_CALIBRATION and WEIGHTS. Returns a one-line status."""
    results = {r["key"]: r for r in read_tab_dicts(sh, "RUNNER_RESULTS")}
    rows = join_log_results(read_tab_dicts(sh, "RUNNER_LOG"), results)
    races = usable_races(rows)
    if not races:
        write_tab(sh, "MODEL_CALIBRATION", ["Section", "", "", "", "", ""],
                  [["No settled runners yet: this fills in after the first races are logged and settled."]])
        return "no settled races yet"
    write_tab(sh, "MODEL_CALIBRATION", ["Section", "", "", "", "", ""], calibration_rows(rows, races))
    if len(races) < MIN_FIT_RACES:
        write_tab(sh, "WEIGHTS", ["feature", "current", "fitted", "apply", "races", "needed"],
                  [[n, W[n], "", 0, len(races), MIN_FIT_RACES] for n in ["market"] + FEATURES])
        return f"{len(races)}/{MIN_FIT_RACES} races logged for weight fitting"
    res = fit_weights(races, W)
    apply = res["test_loss_fitted"] < res["test_loss_current"] - 0.002
    head = ["feature", "current", "fitted", "apply", "races", "cv_loss_current", "cv_loss_fitted", "updated"]
    out = [[n, W[n], round(res["fitted"][n], 3), 1 if apply else 0, res["races"],
            round(res["test_loss_current"], 4), round(res["test_loss_fitted"], 4),
            now.isoformat(timespec="minutes")] for n in res["names"]]
    write_tab(sh, "WEIGHTS", head, out)
    return (f"fitted on {res['races']} races, cross-validated loss {res['test_loss_current']:.4f} -> "
            f"{res['test_loss_fitted']:.4f} ({'apply' if apply else 'not better, keeping current'})")


def load_weights(sh):
    """With USE_FITTED_WEIGHTS=1, use the weights the last analysis found to be better."""
    if not USE_FITTED:
        return "off"
    rows = read_tab_dicts(sh, "WEIGHTS")
    if not rows or str(rows[0].get("apply")) != "1":
        return "on, nothing to apply yet"
    n = 0
    for r in rows:
        v = to_float(r.get("fitted"))
        if r["feature"] in W and v is not None:
            W[r["feature"]] = v
            n += 1
    return f"applied {n} fitted weights"


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main():
    now = dt.datetime.now(TZ)
    day = now.date() + (dt.timedelta(days=1) if now.hour >= 22 else dt.timedelta(0))
    warnings, status = [], "OK"
    log(f"Run at {now:%Y-%m-%d %H:%M} Dublin, target day {day}, region={REGION}")
    sh = open_sheet()
    weights_status = load_weights(sh)

    # 1. settle old bets first so results are never lost
    tracker = load_tracker(sh)
    settle_all(tracker, now, warnings)
    race_best = load_tracker(sh, "RACE_BEST_TRACKER")
    settle_all(race_best, now, warnings)
    fav_best = load_tracker(sh, "FAV_BASELINE_TRACKER")
    settle_all(fav_best, now, warnings)
    try:                                         # one-off repair of old place results (see recheck_places)
        rechecked = sum(recheck_places(t, now) for t in (tracker, race_best, fav_best))
    except Exception as ex:
        rechecked = 0
        warnings.append(f"place re-check failed: {ex}")
    try:
        runner_results_added = settle_runner_log(sh, now)
    except Exception as ex:                     # the research log must never break the main job
        runner_results_added = 0
        warnings.append(f"runner results step failed: {ex}")

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
                [[stamp, r["date"], r["course"], r["time"], r["horse"], r["dec"], r.get("src", "p"),
                  r.get("dec_best")] for r in runners])

    # 4. model + bets
    add_model(runners)
    runners.sort(key=lambda r: (r["time"], r["course"], r["dec"]))
    blocked = blocked_segments(race_best) if AUTO_FILTER else {}
    bets = choose_bets(runners, blocked)
    try:
        logged = set(get_tab(sh, "RUNNER_LOG").col_values(1)[1:])
        new_log = runner_log_rows(runners, now, logged)
        append_rows(sh, "RUNNER_LOG", RL_COLS, new_log)
    except Exception as ex:
        new_log = []
        warnings.append(f"runner log step failed: {ex}")
    known = {r["key"] for r in tracker}
    new_bets = 0
    for b in bets:
        key = f"{b['date']}|{norm(b['course'])}|{b['time']}|{norm(b['horse'])}"
        if key in known:
            continue
        tracker.append({**{c: "" for c in TRACKER_COLS}, "key": key, "date": b["date"],
                        "course": b["course"], "time": b["time"], "status": "PENDING",
                        **pick_fields(b, "win-focus" if WIN_FOCUS else "edge")})
        new_bets += 1
    save_tracker(sh, tracker)

    picks = choose_best_per_race(runners)
    new_race_bests = upsert_race_bests(race_best, picks, BEST_RULE)
    save_tracker(sh, race_best, "RACE_BEST_TRACKER")
    upsert_race_bests(fav_best, choose_favourite_per_race(runners), "fav")
    save_tracker(sh, fav_best, "FAV_BASELINE_TRACKER")

    # 5. write tabs
    r_head = R_HEAD

    def rr(r):
        return [r["date"], r["course"], r["region"], r["time"], r["horse"], r["odds"], r["dec"],
                r["fair_prob"], r["model_prob"], r["value_edge"], r["ev"],
                r["mover_2h_pct"], r["mover_night_pct"], r.get("form_score"), r.get("rating"),
                r.get("days_off"), r.get("trainer_rate"), r.get("jockey_rate"),
                r.get("confidence"), r.get("flag"), r.get("reason"),
                frac_text(r.get("dec_best")), r.get("books"), r.get("mover_1h_pct"),
                r.get("wwr_gap"), r.get("course_wins"), r.get("course_runs"),
                frac_text(r.get("night_best"))]

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
    n1h = sum(1 for r in runners if r.get("mover_1h_pct") is not None)
    for tab, field, have in (("MARKET_MOVERS_1H", "mover_1h_pct", n1h),
                             ("MARKET_MOVERS_2H", "mover_2h_pct", n2h),
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
    write_tab(sh, "BEST_PER_RACE", BEST_HEAD,
              [[r["date"], r["time"], r["course"], r["region"], r["horse"], r["odds_at_pick"],
                frac_text(to_float(r.get("night_dec"))), r["model_prob"], r["value_edge"],
                r["form_score"], r["confidence"], r["reason"], r["race_flag"], r["status"],
                r["position"], r["field_size"], r["places_paid"], r["sp"], r["clv_pct"],
                r["night_clv_pct"], r["won"], r["placed"], r["pnl_win_units"],
                r["pnl_place_units"], r["pick_rule"] or "edge"]
               for r in card])
    rhead, rrows = summarise(race_best, fav_best)
    write_tab(sh, "RACE_BEST_SUMMARY", rhead, rrows)

    analysis_note = "skipped (runs on the late run)"
    if ANALYSE or now.hour >= 22:
        try:
            analysis_note = run_analysis(sh, now)
        except Exception as ex:
            analysis_note = f"failed: {ex}"
            warnings.append(f"analysis step failed: {ex}")

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
            ["non_runners", sum(1 for r in tracker if r["status"] == "NON-RUNNER")],
            ["race_best_picks_today", len(picks)], ["race_best_new", new_race_bests],
            ["race_best_pending", sum(1 for r in race_best if r["status"] == "PENDING")],
            ["race_best_settled", sum(1 for r in race_best if r["status"] == "SETTLED")],
            ["race_best_non_runners", sum(1 for r in race_best if r["status"] == "NON-RUNNER")],
            ["place_results_rechecked", rechecked],
            ["bets_to_place_rule", f"win-focus (top model pick, win prob >= {MIN_WIN_PROB:.0%}, price <= {MAX_WIN_DEC})"
             if WIN_FOCUS else "value edge"], ["best_per_race_rule", BEST_RULE],
            ["runners_with_night_price_pct", round(100 * sum(1 for r in runners if r.get("night_best"))
                                                   / len(runners)) if runners else 0],
            ["runners_with_form_pct", round(100 * sum(1 for r in runners if (r.get("runs_n") or 0) >= 2)
                                            / len(runners)) if runners else 0],
            ["runners_with_rating_pct", round(100 * sum(1 for r in runners if r.get("rating"))
                                              / len(runners)) if runners else 0],
            ["runners_with_trainer_form_pct", round(100 * sum(1 for r in runners if r.get("trainer_rate") is not None)
                                                    / len(runners)) if runners else 0],
            ["races_flagged", sum(1 for x in races if x.get("flag"))],
            ["runners_with_bookmaker_prices_pct", round(100 * sum(1 for r in runners if r.get("src") == "b")
                                                        / len(runners)) if runners else 0],
            ["runner_log_rows_added", len(new_log)], ["runner_results_added", runner_results_added],
            ["analysis", analysis_note], ["fitted_weights", weights_status],
            ["blocked_segments", "; ".join(f"{k} (n={v[0]}, ROI {v[1]}%, CLV {v[2]}%)"
                                           for k, v in blocked.items()) or "none"],
            ["baseline_settled", sum(1 for r in fav_best if r["status"] == "SETTLED")], []]
    for course, (nr, nrun) in sorted(per_meeting.items()):
        dash.append([f"meeting: {course}", f"{nr} races, {nrun} priced runners"])
    for w in warnings[:40]:
        dash.append(["warning", w])
    write_tab(sh, "DASHBOARD", ["item", "value"], dash)
    append_rows(sh, "RUN_LOG", ["run_time", "target_day", "races", "runners", "bets", "status"],
                [[stamp, day.isoformat(), len(races), len(runners), len(bets), status]])
    try:
        apply_formatting(sh)
    except Exception as ex:                     # cosmetic only, never fail the run for it
        log(f"  could not apply highlighting / % formats: {ex}")
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

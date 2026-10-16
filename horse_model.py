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
  7. Compares each race's field with the first field seen (FIELD_BASELINE) and highlights
     horses ADDED (yellow) or REMOVED (red, struck through).
  8. Adjusts the night-before price and the price moves for withdrawn horses (Rule 4 style),
     so a horse shortening only because another one was removed is not counted as a move.

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
  BOOKMAKERS      the bookmakers you bet with, default "PaddyPower,Boylesports". Your price (and the night-before
                  price) is the best of THESE only. Use "all" for the best of every bookmaker on the page.

Staking used for the euro P&L and ROI columns (how the bets are actually placed):
  price under 9/2  -> 5 euro win
  9/2 or bigger    -> 2.50 each way (2.50 win + 2.50 place), place part paid at 1/5 of the odds,
                      number of places by field size (2 for 5-7 runners, 3 for 8+, 4 for 16+ handicaps)
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
STAKE_WIN = 5.0                    # euro on a straight win bet
STAKE_EW = 2.5                     # euro each way (win part and place part)
EW_FROM = 5.5                      # decimal price at which win bets become each way (9/2)
EW_PLACE_FRACTION = 0.2            # each-way place part pays 1/5 of the odds
def parse_bookmakers(text):
    """'PaddyPower, Boylesports' -> {'paddypower', 'boylesports'}; 'all' -> None (every bookmaker)."""
    t = (text or "").strip()
    if not t:
        t = "PaddyPower,Boylesports"
    if t.lower() in ("all", "*", "any"):
        return None
    return {re.sub(r"[^a-z0-9]", "", x.lower()) for x in t.split(",") if x.strip()}


_BOOK_TEXT = (os.environ.get("BOOKMAKERS") or "").strip() or "PaddyPower,Boylesports"
PREFERRED_BOOKS = parse_bookmakers(_BOOK_TEXT)                                    # None = every bookmaker
BOOK_LABEL = "all bookmakers" if PREFERRED_BOOKS is None else " + ".join(x.strip() for x in _BOOK_TEXT.split(",") if x.strip())
PLACE_RULES_V = "2"                # bumped when place settlement changed; older rows are re-checked
AUTO_FILTER = (os.environ.get("AUTO_FILTER") or "1") != "0"
USE_FITTED = (os.environ.get("USE_FITTED_WEIGHTS") or "0") == "1"
ANALYSE = (os.environ.get("ANALYSE") or "0") == "1"
MIN_SEG_N = 40                     # settled bets needed before a segment can be switched off
MIN_FIT_RACES = 300                # logged, settled races needed before fitting weights
LOG_WINDOW_MIN = int(os.environ.get("LOG_WINDOW_MIN") or 35)   # log a race's inputs once this close to the off
DEBUG = os.environ.get("DEBUG_DUMP") == "1"

FIELDS = {}      # race key -> field seen on this run (filled by scrape_day)
BOOK_ONLY = []   # snapshot rows for horses with a bookmaker price but no priced runner row

MIN_DEC, MAX_DEC = 1.8, 13.0       # price window for selections (decimal odds)
# Starting weights for the form model (logit units). Guesses, to be tuned from the tracker.
W = {"market": 1.1, "form": 0.7, "rating": 0.20, "trainer": 0.12, "jockey": 0.08,
     "cd": 0.15, "layoff": 0.15, "move": 0.8, "move1h": 0.6, "wwr": 0.08, "course": 0.06, "bf": 0.0}
FEATURES = ["form", "rating", "trainer", "jockey", "cd", "layoff", "move", "move1h",
            "wwr", "course", "bf"]
MOVER_MIN_AGE_MIN = 20             # a snapshot must be at least this old to be a reference
MOVER_SHOW_PCT = 5.0               # show movers of at least this size
REQUEST_PAUSE = 0.4                # seconds between page requests
BOOK_COVER_MIN = 0.6               # bookmaker prices are used for a race when they cover this share of runners

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


def parse_odds_compare(html, known=(), only=None):
    """Odds Compare page -> {norm(horse): {...}} using the bookmaker columns (tote and wild outliers dropped).
    market price: "median", "mean", "best" and "books" come from EVERY bookmaker (a steady view of the market).
    your price: "price_taken" / "taken_book" are the best price from the bookmakers named in `only`
    (a set of lowercase names without spaces, e.g. {"paddypower", "boylesports"}); None means every bookmaker."""
    soup = BeautifulSoup(html, "html.parser")
    out = {}
    for table in soup.find_all("table"):
        header, rows = None, []
        for tr in table.find_all("tr"):
            cells = [clean(c.get_text(" ", strip=True)) for c in tr.find_all(["td", "th"])]
            if not cells:
                continue
            if header is None and any(c.lower() == "bet365" for c in cells):
                header = cells
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
            pairs = []                                   # (bookmaker, decimal price)
            for i in range(j + 2, len(cells)):          # j+1 is the opening/summary cell
                hname = header[i] if i < len(header) else ""
                if hname.lower() in ("tote", ""):
                    continue
                if PRICE_ONLY_RE.match(cells[i]):
                    d = parse_odds(cells[i])[1]
                    if d:
                        pairs.append((hname, d))
            decs = sorted(d for _, d in pairs)
            if len(decs) >= 4:
                med = decs[len(decs) // 2]
                pairs = [(b, d) for b, d in pairs if med / 1.6 <= d <= med * 1.6]
                decs = sorted(d for _, d in pairs)
            if len(decs) >= 3:
                med = decs[len(decs) // 2]
                mine = [(b, d) for b, d in pairs
                        if only is None or re.sub(r"[^a-z0-9]", "", b.lower()) in only]
                taken = max(mine, key=lambda x: x[1]) if mine else (None, None)
                out[norm(display_name(cells[j]))] = {
                    "best": max(decs), "median": med, "books": len(decs), "mean": sum(decs) / len(decs),
                    "price_taken": taken[1], "taken_book": taken[0]}
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
        FIELDS[f"{day.isoformat()}|{norm(course)}|{hhmm}"] = {
            "date": day.isoformat(), "course": course, "time": hhmm,
            "horses": [(e["name"], e["norm"]) for e in entries]}
        books_all = {}
        ohtml = get(url.replace("/racecards/", "/odds-comparison/"))
        if ohtml:
            books_all = parse_odds_compare(ohtml, {e["norm"] for e in entries}, PREFERRED_BOOKS)
        covered = sum(1 for e in entries if e["norm"] in books_all)
        # bookmaker prices run the race when they cover most of the field (the rest keep the Probable SP);
        # with thin coverage the whole race stays on the Probable SP, but the prices we did see are still
        # remembered for the night-before record.
        books = books_all if covered >= BOOK_COVER_MIN * max(len(entries), 1) else {}
        for e in entries:
            b = books.get(e["norm"])
            ba = books_all.get(e["norm"])
            e["dec_sp"] = e["dec"]
            e["dec_best"], e["books"], e["src"], e["dec_mv"] = None, 0, "p", e["dec"]
            e["book_best_any"] = ba["price_taken"] if ba else None        # remembered for the night-before record
            e["best_book"] = ""
            if b:
                e["dec"], e["books"], e["src"], e["dec_mv"] = b["median"], b["books"], "b", b["mean"]
                e["dec_best"], e["best_book"] = b["price_taken"], b["taken_book"] or ""    # YOUR bookmakers' best price
                e["odds"] = frac_text(b["median"])
        priced = [e for e in entries if e["dec"]]
        for e in entries:
            ba = books_all.get(e["norm"])
            if ba and ba.get("price_taken") and (len(priced) < 2 or not e["dec"]):
                BOOK_ONLY.append([day.isoformat(), course, hhmm, e["name"], ba["median"], "n",
                                  ba["price_taken"], ba["mean"]])
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
                      "price_src": "bookmakers" if books else "probable SP",
                      "books_cover": f"{covered} of {len(entries)}"})
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
                            "weight_lb": e["weight_lb"], "dec_mv": e["dec_mv"],
                            "book_best_any": e["book_best_any"], "best_book": e["best_book"]})
    return races, runners, per_meeting, info


# ----------------------------------------------------------------------------
# Market movers
# ----------------------------------------------------------------------------
def snap_key(date, course, hhmm, horse):
    return f"{date}|{norm(course)}|{hhmm}|{norm(horse)}"


def _val(h, src):
    """The comparable price in a snapshot: for bookmaker snapshots the average bookmaker price (older
    snapshots that lack it cannot be compared); for Probable SP snapshots the price itself.
    None if the snapshot is from a different price source."""
    hs = h[2] if len(h) > 2 else "p"
    if src is not None and hs != src:
        return None
    if hs == "b":
        return (h[4] if len(h) > 4 else None) or None
    return h[1]


def pick_ref(hist, now, target_min=120, lo=MOVER_MIN_AGE_MIN, hi=300, src=None, with_ts=False):
    """Price from the snapshot closest to `target_min` minutes ago, within [lo, hi] minutes old.
    With with_ts=True returns (snapshot time, price) instead of just the price."""
    best = None
    for h in hist:
        v = _val(h, src)
        if v is None:
            continue
        age = (now - h[0]).total_seconds() / 60
        if lo <= age <= hi:
            d = abs(age - target_min)
            if best is None or d < best[0]:
                best = (d, v, h[0])
    if not best:
        return None
    return (best[2], best[1]) if with_ts else best[1]


def pick_open(hist, now, lo=MOVER_MIN_AGE_MIN, src=None, with_ts=False):
    """Price from the earliest snapshot of the day that is at least `lo` minutes old."""
    old = [(h[0], _val(h, src)) for h in hist
           if _val(h, src) is not None and (now - h[0]).total_seconds() / 60 >= lo]
    if not old:
        return None
    first = min(old)
    return first if with_ts else first[1]


def pick_night_best(hist, race_day):
    """Best bookmaker price from the last snapshot taken the evening before the race, else None."""
    cand = [h for h in hist if len(h) > 3 and h[3] and h[0].date() < race_day]
    return max(cand, key=lambda h: h[0])[3] if cand else None


def pick_night_snap(hist, race_day):
    """The last snapshot taken the evening before the race that has a bookmaker price, else None."""
    cand = [h for h in hist if len(h) > 3 and h[3] and h[0].date() < race_day]
    return max(cand, key=lambda h: h[0]) if cand else None


def nr_adjustments(removed, snaps):
    """{race_key: [(last_seen_ts, fair_prob, name)]} for each removed horse that has price history today.
    fair_prob is the horse's share of the market (margin removed) at the last run where it still had a price."""
    out = {}
    for rk, items in removed.items():
        prefix = rk + "|"
        by_ts = defaultdict(float)
        for key, hist in snaps.items():
            if key.startswith(prefix):
                for h in hist:
                    if h[1]:
                        by_ts[h[0]] += 1.0 / h[1]
        for name, n in items:
            hist = snaps.get(prefix + n)
            if not hist:
                continue
            last = max(hist, key=lambda h: h[0])
            total = by_ts.get(last[0], 0.0)
            if not last[1] or total <= 0:
                continue
            out.setdefault(rk, []).append((last[0], (1.0 / last[1]) / total, name))
    return out


def deduction_since(nr, rk, ts):
    """Rule 4 style deduction (a fraction, capped at 0.9) for horses withdrawn after the snapshot at `ts`.
    A horse counts if it still had a price in that snapshot or later."""
    return min(0.9, sum(p for last, p, _ in nr.get(rk, []) if last >= ts))


def rule4_price(dec, ded):
    """Decimal price after a deduction: the winnings part is cut by `ded` (0.2 = 20p in the pound)."""
    if not dec or not ded:
        return dec
    return 1 + (dec - 1) * (1 - ded)


def add_movers(runners, snaps, now, nr=None):
    """Price moves in %, negative = shortened. Only real bookmaker prices are compared (a Probable SP
    forecast does not move, so those horses get blanks, not a misleading 0). When a horse has been
    withdrawn since a reference price was taken, that reference price is first cut Rule 4 style, so the
    other runners shortening only because of the withdrawal is not counted as a move."""
    nr = nr or {}
    for r in runners:
        hist = snaps.get(snap_key(r["date"], r["course"], r["time"], r["horse"]), [])
        rk = f"{r['date']}|{norm(r['course'])}|{r['time']}"
        nsnap = pick_night_snap(hist, dt.date.fromisoformat(r["date"]))
        ded_n = deduction_since(nr, rk, nsnap[0]) if nsnap else 0.0
        r["night_best"] = rule4_price(nsnap[3], ded_n) if nsnap else None
        r["nr_deduction"] = round(ded_n, 4) if ded_n else ""
        src = r.get("src", "p")
        r["mover_1h_pct"] = r["mover_2h_pct"] = r["mover_night_pct"] = None
        if src != "b":
            continue
        cur = r.get("dec_mv") or r["dec"]

        def adj(t):
            return rule4_price(t[1], deduction_since(nr, rk, t[0])) if t else None

        ref1 = adj(pick_ref(hist, now, 60, MOVER_MIN_AGE_MIN, 150, src, with_ts=True))
        ref2 = adj(pick_ref(hist, now, src=src, with_ts=True))
        refo = adj(pick_open(hist, now, src=src, with_ts=True))
        if ref1:
            r["mover_1h_pct"] = round((cur - ref1) / ref1 * 100, 1)
        if ref2:
            r["mover_2h_pct"] = round((cur - ref2) / ref2 * 100, 1)
        if refo:
            r["mover_night_pct"] = round((cur - refo) / refo * 100, 1)


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
            "price_src": (f"{b.get('best_book') or 'best'} (best of {BOOK_LABEL})" if best else
                          "market price (no price from your bookmakers)" if b.get("src") == "b" else "probable SP"),
            "declared": b.get("declared", ""), "night_dec": b.get("night_best") or "",
            "pick_rule": rule, "field_change": b.get("field_change", "")}


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
                "declared", "rules_v", "night_dec", "night_clv_pct", "pick_rule", "nr_note",
                "bet_type", "stake_eur", "pnl_eur", "pnl_eur_sp", "field_change"]


def staking(dec, won, placed, field):
    """(bet type, euro staked, euro profit) for one bet at decimal price `dec`.
    Under 9/2: 5 euro win. 9/2 or bigger: 2.50 each way with the place part at 1/5 of the odds.
    In a field of 4 or fewer there is no place market, so the place half is refunded."""
    if not dec or dec <= 1:
        return "", 0.0, None
    if dec < EW_FROM:
        return "WIN 5", STAKE_WIN, round(STAKE_WIN * (dec - 1) if won else -STAKE_WIN, 2)
    win_part = STAKE_EW * (dec - 1) if won else -STAKE_EW
    if field and field <= 4:
        return "EW 2.50 (no place market)", STAKE_EW, round(win_part, 2)
    place_dec = (dec - 1) * EW_PLACE_FRACTION + 1
    place_part = STAKE_EW * (place_dec - 1) if placed else -STAKE_EW
    return "EW 2.50", 2 * STAKE_EW, round(win_part + place_part, 2)


def row_staking(row):
    """Staking result for a settled tracker row at the price taken and at the SP.
    Returns {"pick": (type, stake, pnl), "sp": (type, stake, pnl)} or None if not a settled bet."""
    if row.get("status") != "SETTLED":
        return None
    won = int(to_float(row.get("won"), 0)) == 1
    placed = int(to_float(row.get("placed"), 0)) == 1
    field = to_float(row.get("field_size"), 0) or 0
    sp = to_float(row.get("sp_dec"))
    pick = to_float(row.get("dec_at_pick")) or sp
    return {"pick": staking(pick, won, placed, field), "sp": staking(sp or pick, won, placed, field)}


def apply_staking(rows):
    """Fill bet_type / stake_eur / pnl_eur (at the price taken) / pnl_eur_sp on every tracker row."""
    for r in rows:
        st = row_staking(r)
        if st:
            r["bet_type"], r["stake_eur"], r["pnl_eur"] = st["pick"]
            r["pnl_eur_sp"] = st["sp"][2]
        elif r.get("status") == "NON-RUNNER":
            r["bet_type"], r["stake_eur"], r["pnl_eur"], r["pnl_eur_sp"] = "NON-RUNNER", 0, 0, 0


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
            "Avg night CLV %", "Staked €", "P&L €", "ROI % (my staking)", "ROI at SP % (my staking)"]


PCT_SUMMARY_COLS = ["Win %", "Place %", "Win ROI %", "Place ROI %", "Avg CLV %", "Avg night CLV %",
                    "ROI % (my staking)", "ROI at SP % (my staking)"]
EUR_SUMMARY_COLS = ["Staked €", "P&L €"]


def _staking_cols(g):
    """Euro staked, euro profit and ROI using the 5 win / 2.50 each-way staking."""
    sts = [row_staking(r) for r in g]
    sts = [x for x in sts if x]
    staked = sum(x["pick"][1] for x in sts)
    pnl = sum(x["pick"][2] or 0 for x in sts)
    staked_sp = sum(x["sp"][1] for x in sts)
    pnl_sp = sum(x["sp"][2] or 0 for x in sts)
    return [round(staked, 2), round(pnl, 2),
            round(pnl / staked, 4) if staked else "", round(pnl_sp / staked_sp, 4) if staked_sp else ""]


def stats_row(name, g):
    """Rates are stored as fractions (0.25 = 25%); the sheet shows them with a % format."""
    n = len(g)
    if n == 0:
        return [name, 0, 0, "", 0, "", 0, "", 0, "", "", "", 0, 0, "", ""]
    w = sum(int(to_float(r["won"], 0)) for r in g)
    p = sum(int(to_float(r["placed"], 0)) for r in g)
    wp = sum(to_float(r["pnl_win_units"], 0) for r in g)
    pp = sum(to_float(r["pnl_place_units"], 0) for r in g)
    clvs = [c for c in (to_float(r.get("clv_pct")) for r in g) if c is not None]
    nclvs = [c for c in (to_float(r.get("night_clv_pct")) for r in g) if c is not None]
    return [name, n, w, round(w / n, 4), p, round(p / n, 4),
            round(wp, 2), round(wp / n, 4), round(pp, 2), round(pp / n, 4),
            round(sum(clvs) / len(clvs) / 100, 4) if clvs else "",
            round(sum(nclvs) / len(nclvs) / 100, 4) if nclvs else ""] + _staking_cols(g)


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
    apply_staking(rows)
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
                 to_float(v[7]) if len(v) > 7 else None, to_float(v[8]) if len(v) > 8 else None))
        except ValueError:
            continue
    if len(vals) > 30000:
        write_tab(sh, "MARKET_SNAPSHOTS_LOG", SNAP_HEAD, keep)
    return snaps, max(0, len(vals) - 1)


SNAP_HEAD = ["ts", "date", "course", "time", "horse", "dec_price", "src", "dec_best", "dec_mv"]


R_HEAD = ["date", "course", "region", "time", "horse", "odds", "dec", "fair_prob", "model_prob",
          "value_edge", "ev", "mover_2h_pct", "mover_night_pct", "form_score", "rating",
          "days_off", "trainer_rate", "jockey_rate", "confidence", "race_flag", "reason",
          "best_price", "books", "mover_1h_pct", "wwr_gap", "course_wins", "course_runs",
          "night_price", "best_book", "field_change", "nr_deduction"]
BEST_HEAD = ["date", "time", "course", "region", "horse", "odds", "night_price", "model_prob",
             "value_edge", "form_score", "confidence", "reason", "race_flag", "status", "position",
             "field_size", "places_paid", "sp", "clv_pct", "night_clv_pct", "won", "placed",
             "pnl_win_units", "pnl_place_units", "bet_type", "stake_eur", "pnl_eur", "pnl_eur_sp",
             "pick_rule", "field_change"]

HIGHLIGHT_EDGE = 0.05                     # rows with an edge of 5% or more are highlighted green
HIGHLIGHT_RGB = {"red": 0.78, "green": 0.94, "blue": 0.81}
RUNNER_PCT = ["fair_prob", "model_prob", "value_edge", "ev", "trainer_rate", "jockey_rate", "confidence",
              "nr_deduction"]
# tab -> (header, column holding the edge (or (model, market) columns), columns shown as %)
# The trackers are read back by this script, so they are highlighted but their numbers are left as they are.
def format_specs():
    return {
        "RUNNERS_TARGET": (R_HEAD, "value_edge", RUNNER_PCT),
        "SIGNALS": (R_HEAD + ["shorten_score"], "value_edge", RUNNER_PCT),
        "VALUE_BETS_TARGET": (R_HEAD, "value_edge", RUNNER_PCT),
        "BETS_TO_PLACE": (R_HEAD, "value_edge", RUNNER_PCT),
        "BEST_PER_RACE": (BEST_HEAD, "value_edge", ["model_prob", "value_edge", "confidence"],
                      ["stake_eur", "pnl_eur", "pnl_eur_sp"]),
        "BET_TRACKER": (TRACKER_COLS, "value_edge", []),
        "RACE_BEST_TRACKER": (TRACKER_COLS, "value_edge", []),
        "FAV_BASELINE_TRACKER": (TRACKER_COLS, "value_edge", []),
        "RUNNER_LOG": (RL_COLS, ("model_prob", "fair_prob"), []),
        "RACE_BEST_SUMMARY": (SUM_HEAD, None, PCT_SUMMARY_COLS, EUR_SUMMARY_COLS),
        "TRACKER_SUMMARY": (SUM_HEAD, None, PCT_SUMMARY_COLS, EUR_SUMMARY_COLS),
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
    """Batch-update requests that (a) highlight rows with edge >= 5% and (b) show rate columns as %,
    (c) colour ADDED horses yellow and REMOVED horses red with a strike-through.
    Safe to run every time: an existing identical rule is left alone and stale ones are replaced."""
    specs = format_specs() if specs is None else specs
    dele, add = [], []
    for sh_meta in meta_sheets:
        props = sh_meta.get("properties", {})
        title, sid = props.get("title"), props.get("sheetId")
        if title not in specs:
            continue
        head, edge, pct_cols, *rest = specs[title]
        eur_cols = rest[0] if rest else []
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
        if "field_change" in head:
            fc = col_letter(head.index("field_change"))
            have_f = {(((cf.get("booleanRule") or {}).get("condition") or {}).get("values") or [{}])[0]
                      .get("userEnteredValue", "") for cf in existing}
            for formula, fmt in ((f'=${fc}2="ADDED"', ADDED_FMT), (f'=${fc}2="REMOVED"', REMOVED_FMT)):
                if formula not in have_f:
                    add.append({"addConditionalFormatRule": {"index": 0, "rule": {
                        "ranges": [{"sheetId": sid, "startRowIndex": 1, "startColumnIndex": 0,
                                    "endColumnIndex": len(head)}],
                        "booleanRule": {"condition": {"type": "CUSTOM_FORMULA",
                                                      "values": [{"userEnteredValue": formula}]},
                                        "format": fmt}}}})
        for name in pct_cols:
            c = head.index(name)
            add.append({"repeatCell": {
                "range": {"sheetId": sid, "startRowIndex": 1, "startColumnIndex": c, "endColumnIndex": c + 1},
                "cell": {"userEnteredFormat": {"numberFormat": {"type": "PERCENT", "pattern": "0.0%"}}},
                "fields": "userEnteredFormat.numberFormat"}})
        for name in eur_cols:
            c = head.index(name)
            add.append({"repeatCell": {
                "range": {"sheetId": sid, "startRowIndex": 1, "startColumnIndex": c, "endColumnIndex": c + 1},
                "cell": {"userEnteredFormat": {"numberFormat": {"type": "CURRENCY", "pattern": "€#,##0.00"}}},
                "fields": "userEnteredFormat.numberFormat"}})
    # delete highest index first so earlier deletions do not shift the later ones
    dele.sort(key=lambda r: (r["deleteConditionalFormatRule"]["sheetId"], -r["deleteConditionalFormatRule"]["index"]))
    return dele + add


def apply_formatting(sh):
    meta = sh.fetch_sheet_metadata()
    reqs = formatting_requests(meta.get("sheets", [])) + guide_requests(meta.get("sheets", []))
    if reqs:
        sh.batch_update({"requests": reqs})


VISIBLE_TABS = ["BEST_PER_RACE", "BETS_TO_PLACE", "RACE_BEST_SUMMARY", "RACE_BEST_TRACKER", "GUIDE"]


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
# GUIDE tab: plain-English explanation of every tab and every column
# ----------------------------------------------------------------------------
GUIDE_HEAD = ["Section", "Name", "What it means (plain English)", "Why it matters / how to use it"]


def guide_rows():
    """Returns (header, rows, heading_row_indexes) for the GUIDE tab. Heading rows are shaded."""
    rows, heads = [], []

    def H(title):
        heads.append(len(rows))
        rows.append([title, "", "", ""])

    def R(name, meaning, why=""):
        rows.append(["", name, meaning, why])

    H("1. START HERE: HOW THIS SHEET WORKS")
    R("What this sheet does",
      "A program runs about every 30 minutes during racing hours. It reads the day's racecards and bookmaker prices, rates every runner, and picks horses. After each race it reads the result and works out how every pick did using YOUR staking.",
      "You get a live list of what to back, plus a running record of whether the picks really make money.")
    R("The two pick lists",
      "BEST_PER_RACE gives one pick in every race: the horse the model thinks is most likely to win. BETS_TO_PLACE is a much shorter list of stricter picks meant for real betting.",
      "BEST_PER_RACE shows how good the model is across the board. BETS_TO_PLACE is where you would actually put money.")
    R("Your staking",
      "Price under 9/2: 5 euro to win. Price 9/2 or bigger: 2.50 euro each way (2.50 win + 2.50 place = 5 euro in total), with the place part paid at 1/5 of the odds. Every euro column in the sheet uses these rules.",
      "This is how you really bet, so the euro columns are the most realistic measure of results.")
    R("Which bookmakers",
      "Your price, and the night-before price, are the best price from YOUR bookmakers only: Paddy Power and Boylesports by default (change them with the BOOKMAKERS setting). The overall market view (the middle price, fair_prob and the price moves) still uses every bookmaker on the page, because that is a steadier picture of the market.",
      "So the euro results show what you could really have taken. best_book tells you which of your bookmakers has the best price.")
    R("Green rows",
      "Any row with a value_edge of 5% or more is shaded green.",
      "Green means the model rates that horse at least 5 percentage points better than the market does. These are the strongest opinions.")
    R("Yellow and red rows (field changes)",
      "The first time the program sees a race it remembers the field. If a horse appears later that was not in that first field, the row turns yellow and says ADDED. If a horse from the first field has gone, a red row with a line through it says REMOVED.",
      "Late additions and removals change the race. Re-check your pick if a horse in its race was added or removed.")
    R("Withdrawn horses and prices",
      "When a horse is withdrawn, the other horses' prices shorten automatically (the bookmakers re-balance the market). So the program cuts the earlier price (the night-before price and the earlier prices used for the 1h, 2h and overnight moves) by the withdrawn horse's share of the market, in the same way as a Rule 4 deduction, before comparing. This is an approximation, not the official Rule 4 table.",
      "Without this, a horse would look like it was being backed just because another horse was taken out. nr_deduction shows how much was taken off.")
    R("Picks can change until the off",
      "While a race is still to run, its pick can change as prices and information change. Once the race is off, the pick is locked, and that locked pick is the one that is judged.",
      "So a pick you saw early in the day may differ from the final one. Look at the latest run before betting.")
    R("Hidden tabs",
      "Most tabs are hidden to keep things tidy, but they all keep updating. To see one, use the View menu, then Hidden sheets, and pick the tab.",
      "DASHBOARD is the one to unhide if something looks wrong.")
    R("Small samples",
      "A few hundred bets is a small sample. Horse racing is very random, so ROI can swing by 10 percentage points or more purely through luck.",
      "Don't change your approach after a few bad days. Judge on 500+ bets, and also look at CLV, which settles down faster than ROI.")
    R("No guarantees",
      "The model is only an estimate. Bookmakers build a margin into every price, so most people lose over time.",
      "Only bet what you can afford to lose.")

    H("2. THE TABS YOU SEE")
    R("BEST_PER_RACE", "Yesterday's and today's pick in every race, grouped by course with the races in time order. Shows the price you could take, the price the night before, the SP, the result and the euro profit. Horses removed from a race appear as red struck-through rows.", "Your daily card. Green rows are the strongest edges.")
    R("BETS_TO_PLACE", "The short list of the strongest picks for real betting. By default only the model's top-rated horse in a race, at 4/1 or shorter, in a field of 12 or fewer, with decent recent form, no long break and at least a 25% chance on the model. A pending bet on a horse that has been removed from the race shows as a red struck-through row.", "Fewer bets, but aimed at winners rather than long shots.")
    R("RACE_BEST_SUMMARY", "The scoreboard for all the picks in RACE_BEST_TRACKER: wins, places, ROI, CLV and euro profit, for everything together and split into groups (price, field size and so on).", "Start here to see how the picks are doing and where they win or lose.")
    R("RACE_BEST_TRACKER", "The permanent log of every BEST_PER_RACE pick, one row per race, from the pick through to the result and the money won or lost.", "The raw record behind the summary. Check individual bets here.")
    R("GUIDE", "This tab: what every tab and every column means, in plain English.", "Keep it handy. Anything not explained here is not part of the sheet.")

    H("3. THE TABS THAT ARE HIDDEN (View > Hidden sheets)")
    R("BET_TRACKER", "Same as RACE_BEST_TRACKER but for the BETS_TO_PLACE list.", "The record of your stricter real-betting picks.")
    R("TRACKER_SUMMARY", "Same as RACE_BEST_SUMMARY but for BET_TRACKER.", "The scoreboard for the BETS_TO_PLACE list.")
    R("FAV_BASELINE_TRACKER", "A log of 'just back the favourite in every race', staked the same way as your bets.", "The yardstick. If the picks can't beat simply backing favourites, the model isn't adding anything.")
    R("FIELD_BASELINE", "The field of runners the program saw the first time it found each race. Used to spot horses added or removed later. Rows older than 3 days are deleted automatically.", "Don't edit it. If you clear it, the next run simply takes a fresh baseline.")
    R("RUNNERS_TARGET", "Every runner in every race still to come, with its price and all the model numbers.", "The full list behind the picks.")
    R("SIGNALS", "The same runners sorted with the biggest edge at the top, plus shorten_score.", "Quick way to see which horses the model likes most.")
    R("VALUE_BETS_TARGET", "Only runners whose edge is at least the minimum (2% by default), biggest first.", "Horses the model rates clearly above the market.")
    R("RACES_TARGET", "The races found for the day and how many runners had prices.", "A quick check that every meeting was picked up.")
    R("MARKET_MOVERS_1H", "Horses whose average bookmaker price has moved by 5% or more compared with about an hour ago. Horses without real bookmaker prices are not listed.", "Late money is often the most informative.")
    R("MARKET_MOVERS_2H", "Horses whose price has moved by 5% or more compared with about two hours ago.", "Same idea over a longer window.")
    R("MARKET_MOVERS", "Horses whose price has moved by 5% or more compared with the first price of the day (the evening before when available).", "Shows overnight and all-day support or doubt.")
    R("MARKET_SNAPSHOTS_LOG", "A price snapshot of every runner each time the program runs.", "Needed to work out the movers. Grows over time.")
    R("RUNNER_LOG", "Every runner's information (price, form, rating and so on) recorded shortly before the off.", "Research data. Lets the program test whether its ratings are accurate.")
    R("RUNNER_RESULTS", "How each logged runner finished, with the starting price.", "Joined to RUNNER_LOG to check the model against real results.")
    R("MODEL_CALIBRATION", "A report card for the model's percentages: did horses rated at 20% win about 20% of the time? Does the model beat the market?", "Fills in as results arrive. Updated on the late run each night.")
    R("WEIGHTS", "How much each factor counts in the model, and (after 300+ races) what the data suggests instead.", "Shows whether the model's settings should change.")
    R("TARGET_DAY", "Which day the latest run was working on, and when.", "After the last race of the day it moves on to tomorrow.")
    R("DASHBOARD", "A health check of the latest run: how many races and runners were found, how many results were settled, warnings and the euro profit so far.", "Unhide this first if something looks wrong.")
    R("RUN_LOG", "One row for every run of the program, with a status.", "Shows whether runs are happening and whether any failed.")

    H("4. THE SCOREBOARD COLUMNS (RACE_BEST_SUMMARY and TRACKER_SUMMARY)")
    R("Segment", "Which group of bets the row covers, for example ALL BETS or Odds 4/1-9/1 or Field 13+.", "Compare groups to see where the picks do well or badly.")
    R("Bets", "How many finished bets are in the group. Non-runners and bets still to run are not counted.", "Small groups (under about 40 bets) are mostly luck, so don't read much into them.")
    R("Wins", "How many of those bets won.", "")
    R("Win %", "The share of bets that won, often called the strike rate. 25% means 1 winner in every 4 bets.", "Higher is nicer, but a high strike rate on short prices can still lose money.")
    R("Places", "How many bets finished in the paid places, including winners. Places depend on the field: 5 to 7 runners pay 2 places, 8 or more pay 3, big handicaps (16+) pay 4, and 4 or fewer runners have no place betting.", "")
    R("Place %", "The share of bets that placed.", "Shows how often a horse is at least in the money.")
    R("Win P&L (units)", "Profit or loss if you had put 1 unit to win on every bet at the starting price (SP). A unit is just a measuring stick, not euro.", "Older style measure kept for comparison. The euro columns are closer to real life.")
    R("Win ROI %", "ROI for those 1-unit win bets. ROI means Return On Investment: total profit divided by total staked. For example, staking 100 in total and ending 7.80 down is an ROI of -7.8%.", "Above 0% means money made, below 0% means money lost. It is the single most important number, but it needs lots of bets to be trusted.")
    R("Place P&L (units)", "Profit or loss if you had put 1 unit on every bet to place only, at the SP, using normal place terms.", "")
    R("Place ROI %", "ROI for those place-only bets.", "Place-only bets on short prices pay very little, so this is often negative even when a horse places a lot.")
    R("Avg CLV %", "Average closing line value. For each bet: the price you could take when the pick was made divided by the SP, minus 1. Positive means you got a bigger price than the horse went off at.", "Beating the SP regularly is a good sign the picks are finding horses the market later agrees with. It settles down faster than ROI.")
    R("Avg night CLV %", "The same idea, but using the best bookmaker price from the evening before instead of the price at pick time.", "Shows whether betting the night before would have beaten the SP. Only filled in once night prices are being recorded.")
    R("Staked €", "Total euro put on, using your staking: 5 euro win under 9/2, or 2.50 each way (5 in total) from 9/2 up.", "The 'money in'.")
    R("P&L €", "Total euro profit or loss using your staking and the price you took.", "The 'money out' minus the 'money in'.")
    R("ROI % (my staking)", "P&L € divided by Staked €. Total profit as a percentage of the money you put on, using how you really bet.", "This is the most realistic ROI. 10% means 10 euro profit for every 100 euro staked.")
    R("ROI at SP % (my staking)", "The same, but as if every bet had been taken at the starting price instead of the price at pick time.", "Compare with the line above. If this is much lower, you rely on beating the SP to make money.")

    H("5. THE ROWS IN THE SCOREBOARD (the groups)")
    R("ALL BETS", "Every finished bet together.", "The headline result.")
    R("BASELINE: favourite, same races", "What you would have got backing the favourite in the same races, staked the same way. Only races that have a settled favourite record are included.", "The yardstick the picks need to beat.")
    R("Edge <3%, 3-5%, 5%+", "Bets grouped by how much the model liked the horse above the market when the pick was made.", "If bigger edges do better, the model is working. If not, edge isn't telling us much.")
    R("Odds groups", "Bets grouped by the starting price: under 3/2, 3/2 to 4/1, 4/1 to 9/1, and 9/1 or more.", "Shows which price range is making or losing money. Note that CLV in these groups is skewed, because the groups are chosen using the SP.")
    R("Field groups", "Bets grouped by number of runners: under 8, 8 to 12, 13 or more.", "Big fields are harder to predict.")
    R("Race OK / Race flagged", "Races with no warning flag, versus races flagged as big-field handicaps or ones with many lightly-raced horses.", "Shows whether the warning flags are really warning of anything.")
    R("Region: gb / ire", "Britain versus Ireland.", "")
    R("Price shortened / drifted", "Bets where the SP was shorter than the price taken (shortened, CLV positive), versus longer (drifted, CLV negative).", "Horses that drift tend to do worse, which is the market telling us something.")
    R("Pick rule groups", "Bets grouped by the method that chose them: edge (older picks, biggest value), prob (most likely winner), win-focus (BETS_TO_PLACE shortlist), fav (favourite).", "Lets you compare picking methods fairly on the same sheet.")
    R("Status counts", "The last line counts rows by status: PENDING (race not finished), SETTLED, NON-RUNNER and NO_RESULT.", "")

    H("6. COLUMNS IN BEST_PER_RACE AND THE TRACKERS")
    R("key", "A unique ID for the bet (date, course, time and horse).", "Stops the same bet being logged twice.")
    R("date", "The race date.", "")
    R("course", "The racecourse.", "")
    R("time", "The off time as four digits, for example 1530 is 3:30pm.", "")
    R("region", "gb for Britain or ire for Ireland.", "")
    R("horse", "The horse picked.", "")
    R("odds", "The price when the pick was made, as fractions (for example 9/2). It is the best price from your bookmakers (Paddy Power and Boylesports by default). If they had no price it is the middle price of all bookmakers, and if there were no bookmaker prices at all it is the Probable SP.", "The price you would have taken. price_src says which one it was.")
    R("odds_at_pick", "Same as odds, as stored in the tracker.", "")
    R("dec_at_pick", "The price at pick time in decimal form (9/2 is 5.5, evens is 2.0).", "Used for the maths. Decimal price = stake returned per 1 staked including the stake.")
    R("night_price", "The best price from your bookmakers (Paddy Power and Boylesports by default) from the evening before the race, as fractions.", "Shows how the price moved overnight (cut for any horses withdrawn since, see nr_deduction). Blank if no bookmaker price was seen for that horse the evening before (the late run needs the odds page to show a price for it). Check probable_sp_race rows on DASHBOARD.")
    R("night_dec", "The night-before price in decimal form.", "Used to work out night CLV.")
    R("model_prob", "The model's chance that the horse wins, as a percentage. 30% is roughly 2/1.", "Compare with fair_prob. The gap is the edge.")
    R("value_edge", "model_prob minus fair_prob, in percentage points. 5% means the model rates the horse 5 points better than the market does.", "Bigger means the model disagrees with the market more. 5% or more turns the row green.")
    R("field_change", "ADDED means this horse was not in the first field the program saw for the race. REMOVED means it was in the first field but has gone. Blank means no change.", "Yellow rows are added horses. Red struck-through rows are removed horses.")
    R("form_score", "A 0 to 1 score for the horse's last three runs, counting the latest most (50%, then 30%, then 20%). 1 means it won, 0.5 means mid-field, 0.1 means pulled up or fell.", "Higher is better recent form.")
    R("confidence", "The share of runners in the race that have at least 2 previous runs.", "Low confidence means many unraced or lightly raced horses, so the form is less reliable.")
    R("reason", "The two things pushing the model's rating of this horse up the most, for example 'form +0.23, c&d +0.07'.", "A quick 'why'. The numbers are the size of the push in the model's own units.")
    R("race_flag", "A warning: BIG-FIELD HCAP (a handicap with 16 or more runners) or MANY UNRACED (fewer than 70% of runners have 2 or more runs). Blank means no warning.", "These races are harder to predict. BETS_TO_PLACE skips them.")
    R("status", "On bets: PENDING means the race isn't finished, SETTLED means the result is in, NON-RUNNER means the horse wasn't in the race, NO_RESULT means no result could be found after 2 days. In RUN_LOG and DASHBOARD it describes the run itself (OK, OK WITH WARNINGS, NO RACES LEFT and so on).", "Only SETTLED bets count in the scoreboard.")
    R("position", "Where the horse finished. DNF means it didn't finish (pulled up, fell and so on).", "")
    R("field_size", "How many horses actually ran.", "Decides how many places are paid.")
    R("places_paid", "How many places count for the place part of an each-way bet in that race.", "5 to 7 runners pay 2 places, so a third there does not place.")
    R("sp", "The starting price, the official price at the off, as fractions.", "The final market price. It is the yardstick for CLV.")
    R("sp_dec", "The starting price in decimal form.", "")
    R("won", "1 if the horse won, otherwise 0.", "")
    R("placed", "1 if the horse finished in the paid places, otherwise 0. Winners count as placed.", "")
    R("pnl_win_units", "Profit or loss in units for a 1-unit win bet at the SP.", "Older measure, kept for comparison.")
    R("pnl_place_units", "Profit or loss in units for a 1-unit place-only bet at the SP.", "")
    R("clv_pct", "Closing line value for this bet: price at pick time divided by SP, minus 1, as a percentage.", "Positive means the price shortened after the pick (good).")
    R("night_clv_pct", "Same, but using the night-before price.", "Positive means the price shortened after the night before.")
    R("bet_type", "WIN 5 means a 5 euro win bet. EW 2.50 means 2.50 each way. 'No place market' means the race had 4 or fewer runners, so only the win half is staked.", "Chosen from the price taken: under 9/2 is a win bet, 9/2 or bigger is each way.")
    R("stake_eur", "Euro staked on the bet (5, or 2.50 in a field of 4 or fewer). Non-runners are 0.", "")
    R("pnl_eur", "Euro profit or loss at the price you could take when the pick was made. Each-way bets pay the win part at full odds and the place part at 1/5 of the odds.", "Your realistic result for the bet.")
    R("pnl_eur_sp", "Euro profit or loss if the same bet had been placed at the starting price.", "Shows how much depends on getting the earlier price.")
    R("pick_rule", "The method that chose the pick: prob, edge, win-focus or fav. Blank on very old rows means edge.", "Lets you compare methods.")
    R("price_src", "Where the pick price came from, for example 'PaddyPower (best of PaddyPower + Boylesports)', 'market price (no price from your bookmakers)' or 'probable SP'.", "The first kind is a price you could really have taken. The other two are only guides.")
    R("declared", "How many runners were declared when the pick was made.", "Used to avoid counting too many runners when working out places.")
    R("nr_note", "For a non-runner: a note saying the horse wasn't on the result page, and which name there was closest.", "Check it if you think a horse did run. A near match means a spelling problem, not a non-runner.")
    R("rules_v", "An internal version number for how places were worked out.", "Ignore. It lets the program re-check older results once.")
    R("settled_at", "When the result was recorded.", "")

    H("7. EXTRA COLUMNS IN THE RUNNER TABS (RUNNERS_TARGET, SIGNALS, VALUE_BETS_TARGET, BETS_TO_PLACE)")
    R("dec", "The market price in decimal form: the middle (median) bookmaker price, or the Probable SP if bookmaker prices weren't available.", "Used for the market's view.")
    R("fair_prob", "The market's chance for the horse, as a percentage, after removing the bookmakers' margin so all the runners add up to 100%.", "Compare with model_prob.")
    R("ev", "Expected value: model_prob times the best price, minus 1. 5% means the model expects 5 cents profit per euro staked, if it is right.", "Positive means value at the best price available. The model can be wrong, so treat it as a guide.")
    R("mover_1h_pct", "How far the average bookmaker price has moved in the last hour, as a percentage. Negative means shortened (more support), positive means drifted. A blank means there was no earlier bookmaker price to compare with (for example the race is on the Probable SP forecast, which never moves). A 0 means it was compared and had not moved.", "-20% means the price got a fifth shorter.")
    R("mover_2h_pct", "The same over about two hours.", "")
    R("mover_night_pct", "The same since the first price of the day (the evening before when available).", "")
    R("shorten_score", "A single number for how much the price has shortened recently (larger means more support).", "Handy for sorting in SIGNALS.")
    R("nr_deduction", "How much was taken off the earlier prices because of withdrawn horses, as a percentage (20% means the winnings part of the night-before price was cut by a fifth). Blank means no withdrawal affected this horse.", "Already applied to night_price and the mover columns. It is the combined market share of the withdrawn horses, capped at 90%.")
    R("rating", "The horse's official rating (higher means better).", "The model compares it with the rest of the field.")
    R("days_off", "Days since the horse last ran.", "Over 90 days off counts against a horse.")
    R("trainer_rate", "The trainer's win rate over the last 21 days, smoothed so small samples don't mislead.", "A hot trainer is a small positive.")
    R("jockey_rate", "The jockey's win rate over the last 21 days, smoothed in the same way.", "")
    R("best_price", "The biggest price your bookmakers (Paddy Power and Boylesports by default) are offering for the horse. Blank if neither has a price.", "The price to take.")
    R("best_book", "Which of your bookmakers has that best price.", "Where to place the bet.")
    R("books", "How many bookmakers' prices the market price was based on.", "More bookmakers means a more reliable price.")
    R("wwr_gap", "The horse's best winning mark minus today's rating, in 10lb units. Positive means it is rated below the mark it has won off.", "Positive is a small plus.")
    R("course_wins", "How many times the horse has won at this course.", "")
    R("course_runs", "How many times the horse has run at this course.", "")

    H("8. COLUMNS IN THE RESEARCH AND LOG TABS")
    R("logged_at", "When the runner's information was recorded.", "")
    R("dec_market", "The market (median bookmaker) price in decimal form when logged.", "")
    R("dec_best", "The best bookmaker price in decimal form when logged.", "")
    R("src", "Where the market price came from: b for bookmakers, p for Probable SP, n for a night-time bookmaker price recorded for a horse with no other price.", "Price moves are only compared like with like.")
    R("runs_n", "How many previous runs were found for the horse.", "Horses with fewer than 2 are treated as lightly raced.")
    R("cd", "Course and distance record: 1 means it has won at this course and distance, 0.5 means it has won at the course or the distance, 0 means neither.", "")
    R("bf", "1 if the horse was beaten as favourite last time.", "The model learns whether that matters.")
    R("weight_lb", "The weight the horse carries today, in pounds.", "Recorded for research.")
    R("flag", "The race flag at the time of logging (see race_flag).", "")
    R("handicap", "Whether the race is a handicap.", "")
    R("ts", "The time of a price snapshot.", "")
    R("dec_mv", "The average bookmaker price in decimal form in a snapshot (for the Probable SP it is the forecast itself).", "Used to measure price moves. An average moves a little whenever any bookmaker moves, unlike a middle price which can stay put for hours.")
    R("dec_price", "The market price in decimal form in a snapshot.", "")
    R("ref_move_pct", "In the movers tabs: how far the price has moved against the earlier price, as a percentage.", "")
    R("direction", "In the movers tabs: shortening or drifting.", "")
    R("url", "The web page the race was read from.", "")
    R("runners", "In RACES_TARGET and RUN_LOG: how many runners were found.", "")
    R("priced", "How many of those runners had a price.", "Should be nearly all of them.")
    R("races", "In RUN_LOG: how many races were found in that run. In WEIGHTS: how many races were used for the fit.", "")
    R("bets", "In RUN_LOG: how many bets were selected in that run.", "")
    R("run_time", "When the run happened.", "")
    R("target_day", "The racing day the run was working on.", "")
    R("generated", "When TARGET_DAY was last written.", "")

    H("9. COLUMNS IN WEIGHTS AND MODEL_CALIBRATION")
    R("feature", "In WEIGHTS: a factor the model uses, such as form, rating, trainer, jockey, cd (course and distance), layoff, move, move1h, wwr, course and bf. 'market' is how much the model trusts the market price.", "")
    R("current", "In WEIGHTS: how much that factor counts at the moment.", "Bigger means a bigger push on the horse's rating.")
    R("fitted", "In WEIGHTS: what the data suggests the number should be, worked out from your own results once 300+ races are logged.", "Blank until there is enough data.")
    R("apply", "In WEIGHTS: 1 means the fitted numbers beat the current ones in a fair test and can be switched on (the USE_FITTED_WEIGHTS setting). 0 means keep the current ones.", "")
    R("needed", "In WEIGHTS: how many races are needed before fitting starts.", "")
    R("cv_loss_current", "In WEIGHTS: how well the current numbers predicted winners in a cross-check (lower is better).", "Cross-check means each race is scored using numbers that never saw that race, so the test is fair.")
    R("cv_loss_fitted", "In WEIGHTS: the same for the fitted numbers.", "If this is lower than cv_loss_current, the fitted numbers are better.")
    R("updated", "In WEIGHTS: when the fit was last done.", "")
    R("Calibration table", "In MODEL_CALIBRATION: groups horses by the model's percentage and shows how often they really won. 'Avg model %' should be close to 'Actual win %'.", "If horses rated 20% win only 12%, the model is overconfident.")
    R("Log loss", "In MODEL_CALIBRATION: a score for how much probability the model (and the market) gave to the horse that actually won. Lower is better.", "If the model's score is lower than the market's, the model beats the market.")
    R("Verdict", "In MODEL_CALIBRATION: MODEL BEATS MARKET or NOT YET.", "The honest test of whether the extra information is helping.")

    H("10. BETTING WORDS EXPLAINED")
    R("SP (starting price)", "The official price a horse goes off at when the race starts.", "")
    R("Probable SP", "A forecast of the starting prices published on the racecard before the race.", "An estimate, not a price you can bet at.")
    R("Fractional and decimal odds", "9/2 means win 9 for every 2 staked. In decimal the same price is 5.5 (the 5.5 includes your stake coming back).", "The sheet shows fractions for reading and decimals for the maths.")
    R("Each way", "Two bets in one: half on the horse to win and half on it to place. The place part pays a fraction of the odds (1/5 here) if the horse finishes in the places.", "Each way softens the loss on big-priced horses that run well.")
    R("Place terms", "How many places are paid and what fraction of the odds. Here: 2 places for 5 to 7 runners, 3 for 8 or more, 4 for big handicaps (16+); no place betting for 4 or fewer runners.", "A horse finishing 3rd in a 7-runner race does not place.")
    R("Non-runner", "A horse that doesn't take part. Your stake is returned, so it isn't counted as a bet.", "")
    R("Favourite", "The horse with the shortest price in the race.", "")
    R("Handicap", "A race where better horses carry more weight to even things up.", "Harder to predict, especially with big fields.")
    R("Market margin (overround)", "Bookmaker prices imply chances that add up to more than 100%. The extra is their profit margin.", "It's why fair_prob is lower than the price suggests.")
    R("Edge", "How much better the model rates a horse than the market does (see value_edge).", "")
    R("Strike rate", "The share of bets that win (Win %).", "")
    R("CLV (closing line value)", "Whether you beat the final price. See Avg CLV %.", "A good early sign when there aren't many results yet.")
    R("Units and euro", "A unit is just a measuring stick (1 unit staked per bet). Euro columns use your real staking.", "Use the euro columns for the real picture.")

    H("11. SETTINGS YOU CAN CHANGE (GitHub: Settings > Secrets and variables > Actions)")
    R("REGION", "all, gb or ire. Which countries to include.", "")
    R("BOOKMAKERS", "The bookmakers you bet with, separated by commas, for example PaddyPower,Boylesports (the default). Write all to use the best price from every bookmaker instead.", "Your price and the night-before price use only these.")
    R("WIN_FOCUS", "1 (the default) makes BETS_TO_PLACE back only the model's top-rated horse. 0 uses the older biggest-value-edge method.", "")
    R("MAX_WIN_DEC", "The biggest decimal price allowed for BETS_TO_PLACE. 5.0 is 4/1.", "Lower means shorter prices and more winners.")
    R("MAX_FIELD", "The most runners allowed in a BETS_TO_PLACE race. 12 by default.", "")
    R("MIN_WIN_PROB", "The smallest model win chance allowed for BETS_TO_PLACE. 0.25 means 25%.", "")
    R("BEST_RULE", "prob (default) makes BEST_PER_RACE pick the likeliest winner. edge picks the biggest value instead.", "")
    R("MIN_VALUE_EDGE", "The smallest edge for the older value method and for VALUE_BETS_TARGET. 0.02 means 2%.", "")
    R("MIN_EV", "The smallest expected value at the best price. Left empty it isn't applied in win-focus mode.", "")
    R("STRICT_FILTERS", "1 (default) makes BETS_TO_PLACE skip flagged races and horses with fewer than 2 previous runs.", "")
    R("AUTO_FILTER", "1 (default) lets the program switch off groups that have clearly lost over 40+ bets.", "")
    R("USE_FITTED_WEIGHTS", "1 lets the model use the weights fitted from your own results once they have beaten the current ones in a fair test.", "Off by default.")
    R("LOG_WINDOW_MIN", "How many minutes before the off a race's information is saved for research. 35 by default.", "Use 65 if you run hourly instead of every 30 minutes.")
    R("MAX_FIELD_HCAP", "A handicap with this many runners or more gets the BIG-FIELD HCAP flag. 16 by default.", "")
    return GUIDE_HEAD, rows, heads


def guide_requests(meta_sheets):
    """Layout for the GUIDE tab: wrapped text, sensible column widths, shaded section headings."""
    reqs = []
    head, rows, heads = guide_rows()
    for m in meta_sheets:
        props = m.get("properties", {})
        if props.get("title") != "GUIDE":
            continue
        sid, n = props.get("sheetId"), len(rows) + 1
        reqs.append({"updateSheetProperties": {"properties": {"sheetId": sid, "gridProperties": {"frozenRowCount": 1}},
                                               "fields": "gridProperties.frozenRowCount"}})
        for i, w in enumerate((170, 210, 560, 440)):
            reqs.append({"updateDimensionProperties": {
                "range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": i, "endIndex": i + 1},
                "properties": {"pixelSize": w}, "fields": "pixelSize"}})
        reqs.append({"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": n, "startColumnIndex": 0, "endColumnIndex": 4},
            "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP", "verticalAlignment": "TOP"}},
            "fields": "userEnteredFormat(wrapStrategy,verticalAlignment)"}})
        for r in [0] + [h + 1 for h in heads]:
            reqs.append({"repeatCell": {
                "range": {"sheetId": sid, "startRowIndex": r, "endRowIndex": r + 1, "startColumnIndex": 0, "endColumnIndex": 4},
                "cell": {"userEnteredFormat": {"textFormat": {"bold": True},
                                               "backgroundColor": {"red": 0.85, "green": 0.89, "blue": 0.95}}},
                "fields": "userEnteredFormat(textFormat,backgroundColor)"}})
    return reqs


# ----------------------------------------------------------------------------
# Added / removed horses (compared with the first field seen for each race)
# ----------------------------------------------------------------------------
BASE_HEAD = ["race_key", "date", "course", "time", "horse", "norm", "first_seen"]
ADDED_FMT = {"backgroundColor": {"red": 1.0, "green": 0.92, "blue": 0.6}, "textFormat": {"bold": True}}
REMOVED_FMT = {"backgroundColor": {"red": 0.96, "green": 0.8, "blue": 0.8},
               "textFormat": {"strikethrough": True,
                              "foregroundColor": {"red": 0.6, "green": 0.0, "blue": 0.0}}}


def load_baseline(sh, today):
    """{race_key: {norm: horse}}: the field first seen for each race. Rows older than 3 days are dropped."""
    cutoff = (today - dt.timedelta(days=3)).isoformat()
    rows = read_tab_dicts(sh, "FIELD_BASELINE")
    keep = [r for r in rows if r.get("race_key") and r.get("date", "") >= cutoff]
    if len(keep) < len(rows):
        write_tab(sh, "FIELD_BASELINE", BASE_HEAD, [[r.get(c, "") for c in BASE_HEAD] for r in keep])
    base = {}
    for r in keep:
        base.setdefault(r["race_key"], {})[r["norm"]] = r["horse"]
    return base


def field_changes(fields, base, now):
    """Returns (added {race_key: {norm}}, removed {race_key: [(name, norm)]}, new baseline rows).
    A race with no baseline yet gets one. If more than half a field changes at once it is treated
    as a scrape glitch and nothing is flagged."""
    added, removed, new_rows = {}, {}, []
    iso = now.isoformat(timespec="seconds")
    for rk, f in fields.items():
        cur = {n: name for name, n in f["horses"]}
        if len(cur) < 2:
            continue
        orig = base.get(rk)
        if not orig:
            for n, name in cur.items():
                new_rows.append([rk, f["date"], f["course"], f["time"], name, n, iso])
            continue
        gone = [n for n in orig if n not in cur]
        add = {n for n in cur if n not in orig}
        if len(gone) * 2 > len(orig) or len(add) * 2 > len(orig):
            continue
        if add:
            added[rk] = add
        if gone:
            removed[rk] = [(orig[n], n) for n in gone]
    return added, removed, new_rows


def removed_runner(date, course, time_, name):
    """A display-only row for a horse that has left the field."""
    return {"date": date, "course": course, "region": region_of(course), "time": time_, "horse": name,
            "odds": "", "dec": "", "fair_prob": "", "model_prob": "", "value_edge": "", "ev": "",
            "mover_2h_pct": "", "mover_night_pct": "", "shorten_score": "",
            "reason": "removed from the field since the first run", "field_change": "REMOVED"}


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
    added, removed, removed_rows, nr_adj = {}, {}, [], {}
    try:
        base = load_baseline(sh, now.date())
        added, removed, new_base = field_changes(FIELDS, base, now)
        append_rows(sh, "FIELD_BASELINE", BASE_HEAD, new_base)
        removed_rows = [removed_runner(FIELDS[rk]["date"], FIELDS[rk]["course"], FIELDS[rk]["time"], name)
                        for rk, items in removed.items() for name, _ in items]
        nr_adj = nr_adjustments(removed, snaps)
    except Exception as ex:                     # never let this break the main job
        warnings.append(f"field change step failed: {ex}")
    for r in runners:
        rk = f"{r['date']}|{norm(r['course'])}|{r['time']}"
        r["field_change"] = "ADDED" if r["norm"] in added.get(rk, ()) else ""
    add_movers(runners, snaps, now, nr_adj)
    stamp = now.isoformat(timespec="seconds")
    append_rows(sh, "MARKET_SNAPSHOTS_LOG", SNAP_HEAD,
                [[stamp, r["date"], r["course"], r["time"], r["horse"], r["dec"], r.get("src", "p"),
                  r.get("dec_best") or r.get("book_best_any"), r.get("dec_mv")] for r in runners]
                + [[stamp] + x for x in BOOK_ONLY])

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
    cur_by_key = {runner_key(r): r for r in runners}        # keep pending bets' night price up to date
    for t in tracker:
        cr = cur_by_key.get(t["key"]) if t.get("status") == "PENDING" else None
        if cr and cr.get("night_best"):
            t["night_dec"] = cr["night_best"]
    save_tracker(sh, tracker)

    picks = choose_best_per_race(runners)
    new_race_bests = upsert_race_bests(race_best, picks, BEST_RULE)
    save_tracker(sh, race_best, "RACE_BEST_TRACKER")
    upsert_race_bests(fav_best, choose_favourite_per_race(runners), "fav")
    save_tracker(sh, fav_best, "FAV_BASELINE_TRACKER")

    # 5. write tabs
    removed_norms = {(rk, n) for rk, items in removed.items() for _, n in items}
    removed_bets = [removed_runner(t["date"], t["course"], t["time"], t["horse"]) for t in tracker
                    if t["status"] == "PENDING"
                    and (f"{t['date']}|{norm(t['course'])}|{t['time']}", norm(t["horse"])) in removed_norms]
    removed_cards = [{**{c: "" for c in TRACKER_COLS}, "date": r["date"], "course": r["course"],
                      "time": r["time"], "region": r["region"], "horse": r["horse"],
                      "status": "REMOVED", "reason": r["reason"], "field_change": "REMOVED"}
                     for r in removed_rows]
    r_head = R_HEAD

    def rr(r):
        return [r["date"], r["course"], r["region"], r["time"], r["horse"], r["odds"], r["dec"],
                r["fair_prob"], r["model_prob"], r["value_edge"], r["ev"],
                r["mover_2h_pct"], r["mover_night_pct"], r.get("form_score"), r.get("rating"),
                r.get("days_off"), r.get("trainer_rate"), r.get("jockey_rate"),
                r.get("confidence"), r.get("flag"), r.get("reason"),
                frac_text(r.get("dec_best")), r.get("books"), r.get("mover_1h_pct"),
                r.get("wwr_gap"), r.get("course_wins"), r.get("course_runs"),
                frac_text(r.get("night_best")), r.get("best_book"), r.get("field_change", ""),
                r.get("nr_deduction", "")]

    if runners:
        def _k(r):
            return (r["time"], r["course"], r["dec"] if isinstance(r["dec"], (int, float)) else 1e9)
        write_tab(sh, "RUNNERS_TARGET", r_head, [rr(r) for r in sorted(runners + removed_rows, key=_k)])
        write_tab(sh, "SIGNALS", r_head + ["shorten_score"],
                  [rr(r) + [r["shorten_score"]] for r in sorted(runners, key=lambda x: -x["value_edge"])]
                  + [rr(r) + [""] for r in removed_rows])
        write_tab(sh, "VALUE_BETS_TARGET", r_head,
                  [rr(r) for r in sorted(runners, key=lambda x: -x["value_edge"])
                   if r["value_edge"] >= MIN_EDGE])
        write_tab(sh, "BETS_TO_PLACE", r_head, [rr(r) for r in bets] + [rr(r) for r in removed_bets])
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
    card = sorted([r for r in race_best if r["date"] >= recent] + removed_cards, key=card_order)
    write_tab(sh, "BEST_PER_RACE", BEST_HEAD,
              [[r["date"], r["time"], r["course"], r["region"], r["horse"], r["odds_at_pick"],
                frac_text(to_float(r.get("night_dec"))), r["model_prob"], r["value_edge"],
                r["form_score"], r["confidence"], r["reason"], r["race_flag"], r["status"],
                r["position"], r["field_size"], r["places_paid"], r["sp"], r["clv_pct"],
                r["night_clv_pct"], r["won"], r["placed"], r["pnl_win_units"],
                r["pnl_place_units"], r["bet_type"], r["stake_eur"], r["pnl_eur"], r["pnl_eur_sp"],
                r["pick_rule"] or "edge", r.get("field_change", "")]
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
            ["night_book_rows_logged", len(BOOK_ONLY)],
            ["fields_added", sum(len(v) for v in added.values())],
            ["fields_removed", sum(len(v) for v in removed.values())],
            ["night_prices_nr_adjusted", sum(1 for r in runners if r.get("nr_deduction"))],
            ["races_on_probable_sp", sum(1 for x in races if x.get("price_src") == "probable SP")],
            ["bets_staking_pnl_eur", round(sum(to_float(r.get("pnl_eur"), 0) for r in tracker
                                               if r["status"] == "SETTLED"), 2)],
            ["race_best_staking_pnl_eur", round(sum(to_float(r.get("pnl_eur"), 0) for r in race_best
                                                    if r["status"] == "SETTLED"), 2)],
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
    for x in [x for x in races if x.get("price_src") == "probable SP"][:10]:
        dash.append(["probable_sp_race", f"{x['course']} {x['time']}: bookmaker prices cover {x.get('books_cover', '?')} runners"])
    for course, (nr, nrun) in sorted(per_meeting.items()):
        dash.append([f"meeting: {course}", f"{nr} races, {nrun} priced runners"])
    for w in warnings[:40]:
        dash.append(["warning", w])
    write_tab(sh, "DASHBOARD", ["item", "value"], dash)
    append_rows(sh, "RUN_LOG", ["run_time", "target_day", "races", "runners", "bets", "status"],
                [[stamp, day.isoformat(), len(races), len(runners), len(bets), status]])
    try:
        ghead, grows, _ = guide_rows()
        write_tab(sh, "GUIDE", ghead, grows)
    except Exception as ex:                     # the guide is a convenience, never fail the run for it
        log(f"  could not write the guide tab: {ex}")
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

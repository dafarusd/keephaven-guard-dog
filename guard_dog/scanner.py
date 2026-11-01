"""One scan of one person: breach checks for every email, the people-search sites, then a sweep of the open web."""
import hashlib
import logging
import math
import os
import queue
import random
import re
import signal
import threading
import time
from pathlib import Path

from . import accounts, archive, breaches, removal, sweep
from .brokers import BROKERS, search_urls

# Block pages, matched on the visible text of short pages (a real results page is long and may mention captcha).
# "Automatic submission failed": the captcha TruePeopleSearch and FamilyTreeNow served PiCloud on 2026-10-30, read as
# "not found" because their name wasn't on it.
CHALLENGE = re.compile(r"just a moment|verify you are (?:a )?human|are you a robot|access denied|press (?:&|and) hold|"
                       r"checking your browser|enable javascript and cookies|request blocked|unusual traffic|"
                       r"complete this challenge|flagged as being suspicious|automatic submission failed|"
                       r"please click the button below to try again", re.I)
# Under this many characters of visible text a page can be a block page. Real results pages run far longer (the
# shortest real one saved on PiCloud is several times this) and may mention "captcha" in passing; block pages, like
# the 1,398-character TruePeopleSearch captcha, are a few lines.
SHORT_PAGE = 4000
# A finding that was listed and isn't seen on one scan: gone only when the next scan agrees.
UNSEEN = "not seen this scan; confirmed if the next scan agrees"
# Where the TruePeopleSearch family sends a browser it wants to check: a captcha that submits itself after a few
# seconds, then the real results. Anything still on that address is a block, whatever the page says.
CAPTCHA_URL = re.compile(r"/internalcaptcha\b", re.I)
# Stealth pages are read this long after they load, not when the network goes quiet: on the slow sites the network
# never does (Playwright waited its full 90 s), and TruePeopleSearch's and FamilyTreeNow's captcha only gets through,
# under the Cloudflare solver, when the page has these seconds to submit itself (laptop A/B/C runs, 2026-10-30:
# same answers on the other 15 sites, 582 s against 1,227 s for 3 rounds of 17 pages).
SETTLE_MS = 5000
# Scrapling's own list of 3,526 ad and tracker hosts, blocked in every stealth page. Each ad frame is one more
# renderer process: on the same 17 pages the median page went from 25-52 processes to 13, and from 1,020 MB (laptop)
# or 1,300-1,730 MB (PiCloud) of real memory to 750-800 MB, with the same answer from every site and the Cloudflare
# check frame untouched (2026-10-30). The list leaves Google's and Cloudflare's captcha hosts alone.
BLOCK_ADS = True
NO_RESULTS = re.compile(r"no (?:results|records|matches|people) (?:were )?found|we (?:could not|couldn't|did not|didn't) find (?:any|anyone|a match|results)|"
                        r"\b0 (?:results|records|matches)\b|no one (?:was )?found|returned no results", re.I)
PROVEN = ("listed", "possible", "not found")  # a check that ends in any other status proved nothing
WINDOW = 400  # characters either side of a name mention that count as the same result
NO_RESULTS_NEAR = 80  # a no-results message this close after a name mention is about that name
KEEP_COPIES = 3

_running = {}  # person id -> True
_cancel = set()
_lock = threading.Lock()


def digits(s):
    return re.sub(r"\D", "", s)


def first_last(name):
    p = name.lower().split()
    return (p[0], p[-1]) if len(p) >= 2 else None


def name_spans(low, fl):
    f, l = map(re.escape, fl)
    pat = rf"\b{f}\b\s+(?:[\w'-]+\.?\s+){{0,2}}\b{l}\b|\b{l}\b,\s*\b{f}\b"
    return [m.span() for m in re.finditer(pat, low)]


# A person's age, not a statistic about everyone with the name ("average age is 47", "median age 52").
AGE = re.compile(r"(?<!average )(?<!median )\bage[:\s]+(\d{2})\b|\b(\d{2}) years old\b|\baged? (\d{2})\b|"
                 r"[a-z]\.?, (\d{2}),", re.I)
# Ages some sites only give as a range: "Age 50s", "in their 50s", "Age 65+", "Age 50-59" (411 and Spokeo do; seen
# on PiCloud's saved pages 2026-10-29). Read as an exact age, "Age 70s" made a 75-year-old someone else. From
# Scrubbr (MIT): a range only tells containment.
AGE_RANGE = re.compile(r"\b(?:age[:\s]+|in (?:his|her|their) )(\d)0'?s\b|\bage[:\s]+(\d{2})\s*\+|"
                       r"\bage[:\s]+(\d{2})\s*(?:-|–|to)\s*(\d{2})\b", re.I)
OLDEST = 120


def age_ranges(text):
    """[(low, high, as shown)] for the ranged ages in text, and the text with them taken out."""
    out = []
    for m in AGE_RANGE.finditer(text):
        d, plus, lo, hi = m.groups()
        if d:
            out.append((int(d) * 10, int(d) * 10 + 9, f"age {d}0s"))
        elif plus:
            out.append((int(plus), OLDEST, f"age {plus}+"))
        elif int(lo) <= int(hi):
            out.append((int(lo), int(hi), f"age {lo}-{hi}"))
    return out, AGE_RANGE.sub(" ", text)
BORN = re.compile(r"\bborn (?:on |in )?(?:\w+ \d{1,2}, |\d{1,2}/\d{1,2}/)?(19\d\d|20[0-2]\d)\b", re.I)
# Where they live or lived, in a listing's words. Bare "living" or "lives" isn't one: FamilyTreeNow's menu says
# "Living People" on a page with no records (VM 2026-10-30).
LIVED = re.compile(r"\b(?:(?:lives|lived|living|resides|resided) in|used to live in|current address|"
                   r"past address(?:es)?|previous cit(?:y|ies))\b", re.I)
KIN = re.compile(r"\b(?:related to|relatives?|family members|associates)\b", re.I)
COUNT = re.compile(r"\b(?:found|we found)?\s*(\d{1,4})\s+(?:records?|results?|people|matches)\b(?:\s+(?:found|named|for))?", re.I)
STREET = re.compile(r"\b\d{1,6}\s+(?:[NSEW]\.?\s+)?[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?\s+"
                    r"(?:St|Street|Ave|Avenue|Rd|Road|Dr|Drive|Ln|Lane|Blvd|Ct|Court|Way|Pl|Place|Cir|Ter|Pkwy)\b")


GATE = re.compile(r"agree to (?:our|the) terms|accept (?:our|the) terms|terms (?:of (?:service|use) )?to continue|"
                  r"i agree to the terms|by continuing you agree", re.I)
SEP = re.compile(r"\.\s+|;\s+|\s\|\s")
OTHER_NAME = re.compile(r"^\W*(?:[A-Z][a-z]+,\s+[A-Z][a-z]+|[A-Z][a-z]+\s+(?:[A-Z]\.?\s+)?[A-Z][a-z]+)\b")


def entries(raw, spans):
    """The entry each name mention sits in: the text between the nearest separators (". ", "; ", " | "),
    merged with the following entries while they mention one of the person's names too (a profile page), plus one more
    entry unless it starts with someone else's name (a list of other people). Capped at WINDOW either side."""
    cuts = [0] + [m.end() for m in SEP.finditer(raw)] + [len(raw)]
    segs = [(cuts[i], cuts[i + 1]) for i in range(len(cuts) - 1) if cuts[i + 1] > cuts[i]]
    named = {i for i, (x, y) in enumerate(segs) if any(x <= a < y for a, _ in spans)}
    out = []
    for i in sorted(named):
        if i - 1 in named:
            continue  # already part of the run that starts earlier
        j = i
        while j + 1 in named:
            j += 1
        if j + 1 < len(segs) and not OTHER_NAME.match(raw[segs[j + 1][0]:segs[j + 1][1]]):
            j += 1
        first = min(a for a, _ in spans if segs[i][0] <= a < segs[i][1])
        last = max(b for a, b in spans if segs[i][0] <= a < segs[j][1])
        out.append((max(segs[i][0], first - WINDOW), min(segs[j][1], last + WINDOW)))
    return out


def city_of(place):
    from .brokers import split_place
    return split_place(place)[0].lower()


def block_signals(block, raw_block, person):
    """Listing details in one result block: (anchors, signals, age mismatch).
    Anchors tie the block to this person (a city or relative of theirs); signals are what a listing shows."""
    anchors, signals = [], []
    for pl in person["places"]:
        c = city_of(pl)
        if len(c) > 3 and re.search(r"\b" + re.escape(c) + r"\b", block):
            anchors.append(f"city: {pl}")
    for r in person.get("relatives") or []:
        fl = first_last(r)
        if fl and name_spans(block, fl):
            anchors.append(f"relative: {r}")
    stats = re.sub(r"\b(?:average|median|typical) age (?:is |of )?\d{2}s?\+?", " ", block)
    ranges, stats = age_ranges(stats)
    ages = [int(next(g for g in m.groups() if g)) for m in AGE.finditer(stats)]
    years = [int(y) for y in BORN.findall(block)]
    born = person.get("birth_year")
    mismatch = False
    if ages or ranges:
        signals.append(f"age {ages[0]}" if ages else ranges[0][2])
        if born:
            now = time.gmtime().tm_year - born  # or one less, before their birthday
            mismatch = (all(abs(now - a) > 1 for a in ages)
                        and all(not (lo <= now and now - 1 <= hi) for lo, hi, _ in ranges))
    if years:
        signals.append(f"born {years[0]}")
        if born and all(abs(y - born) > 1 for y in years):
            mismatch = True
    if LIVED.search(block):
        signals.append("lives / lived in")
    if KIN.search(block):
        signals.append("relatives")
    if STREET.search(raw_block):
        signals.append("street address")
    return anchors, signals, mismatch


def match(text, person, query_name="", query_place=""):
    """What of this person the page shows.

    Results pages repeat the search back, so the searched name and place alone prove nothing. People-search
    listings rarely show phones or emails; they show an age, cities, relatives and addresses. So:
    listed      = a name of theirs + a phone, email, username or another of their names; or a result block that
                  carries one of their cities or relatives (an anchor) and two listing details;
    possible    = an anchored block with one detail, or a result count with the details hidden;
    not found   = none of their names, a no-results page, or only namesakes (blocks with details but none of
                  their cities or relatives, or an age that doesn't fit their birth year);
    unconfirmed = their name and nothing else: the page didn't show any listing details.
    """
    # Page text puts every element of a listing card on its own line, so line breaks are just spaces here;
    # entries are separated by ". ", "; " and " | " (checked on real saved pages: splitting on lines broke the cards).
    raw = " ".join(text.split())
    low = raw.lower()
    flat = digits(text)
    names = {}
    for n in person["names"]:
        fl = first_last(n)
        if fl and fl not in names:
            names[fl] = n
    q = first_last(query_name) if query_name else None
    spans = {fl: name_spans(low, fl) for fl in names}
    if not any(spans.values()):
        return "not found", []
    # The site talking about the search, not about a person (FamilyTreeNow, VM 2026-10-30): its title and search box
    # repeat "Jane Doe in Columbus, OH", and "0 Records for Jane Doe No records found" follows the name. Those
    # mentions are set aside; if nothing else on the page names them, it's a no-results page.
    city = city_of(query_place) if query_place else ""
    echo = re.compile(r"\s+in\s+" + re.escape(city) + r"\b") if len(city) > 3 else None
    said = {fl: [(a, b) for a, b in ss if not NO_RESULTS.search(low[b:b + NO_RESULTS_NEAR])
                 and not (echo and echo.match(low, b))] for fl, ss in spans.items()}
    if not any(said.values()) and NO_RESULTS.search(low[:20000]):
        return "not found", []
    spans = said if any(said.values()) else spans
    shown = [f"name: {n}" for fl, n in names.items() if spans[fl]]
    windows = entries(raw, [sp for ss in spans.values() for sp in ss])

    strong = []
    if q and spans.get(q):  # another of their names on a page that has the searched one
        strong += [f"other name: {n}" for fl, n in names.items() if fl != q and spans[fl]]
    for ph in person["phones"]:
        d = digits(ph)[-10:]
        if len(d) == 10 and d in flat:
            strong.append(f"phone: {ph}")
    for e in person["emails"]:
        if e.lower() in low:
            strong.append(f"email: {e}")
    for u in person["usernames"]:
        if len(u) >= 6 and any(re.search(r"\b" + re.escape(u.lower()) + r"\b", low[x:y]) for x, y in windows):
            strong.append(f"username: {u}")
    if strong:
        return "listed", shown + strong

    best, namesakes, unanchored = None, False, False
    for x, y in windows:
        anchors, signals, mismatch = block_signals(low[x:y], raw[x:y], person)
        if mismatch:  # an age or birth year that rules them out: someone else
            namesakes = namesakes or bool(signals)
            continue
        if not anchors:  # details, but nothing ties them to this person (and nothing rules them out)
            unanchored = unanchored or any(not s.startswith("lives") for s in signals)
            continue
        if best is None or len(signals) > len(best[1]):
            best = (anchors, signals)
    if best and len(best[1]) >= 2:
        return "listed", shown + best[0] + best[1]
    if best and len(best[1]) == 1:
        return "possible", shown + best[0] + best[1]
    if NO_RESULTS.search(low[:20000]):
        return "not found", []
    if GATE.search(low):  # the details sit behind a terms page: we can't say who is or isn't there
        return "unconfirmed", shown + ["the details are behind the site's terms page"]
    if unanchored:
        return "unconfirmed", shown + ["details near the name, but none of their cities, relatives or birth year"]
    if namesakes:
        return "not found", ["only other people with this name (ages don't fit)"]
    counts = [int(m.group(1)) for m in COUNT.finditer(low) if int(m.group(1)) > 0]
    if counts:
        return "possible", shown + [f"{counts[0]} records, details hidden"]
    return "unconfirmed", shown + ["the page showed the name but no listing details"]


def blocked_page(status, text, url=""):
    """A refusal: an error status, a captcha address, or a short page in a block page's words or titled "Captcha"
    (the title is the first thing in the page's visible text)."""
    if status in (401, 403, 429, 503) or CAPTCHA_URL.search(url or ""):
        return True
    return len(text) < SHORT_PAGE and (CHALLENGE.search(text) is not None or text.lstrip().lower().startswith("captcha"))


# Memory on a Pi: a scan with 3 stealth browsers ran up to 174 Chromium processes (one per site and page) and took
# MemAvailable from 7.2 GB to 1.5 GB (PiCloud, 2026-10-29). One page per browser and at most two renderer processes
# keep that down; the next scan's load log says by how much.
# Site isolation still gave every cross-site frame its own renderer (Pi: 77 at once). Off, except for Cloudflare's
# challenge frame: fastpeoplesearch 52 → 2 renderers, PSS 1.4 → 0.6–1.1 GB (laptop, 2026-10-31). Isolation off for
# that frame too broke the solver: 403 on 6 of 6 Cloudflare pages.
BROWSER_LIMITS = {"max_pages": 1, "extra_flags": ["--renderer-process-limit=2", "--disable-site-isolation-trials",
                                                  "--isolate-origins=https://challenges.cloudflare.com"]}
# One page gets this long, whatever it does. Playwright's own timeouts don't cover a page whose scripts never stop:
# on PiCloud (2026-10-29) one Google results page ran 2 hours, grew to 7 GB and took the whole Pi down.
FETCH_LIMIT = 180
LOW_MEMORY_MB = 400  # below this much free memory the page being waited on is stopped before the machine is
CHECK_EVERY = 5
LET_GO = 60  # s for a stopped browser's stuck call to fail
# What one stealth browser costs while it loads a people-search page, measured 2026-10-30 (laptop and PiCloud): real
# memory (PSS) 1.5 GB typical, 3.5 GB on Cloudflare pages; three browsers drew 5.4 GB on the 8 GB Pi, 3.6 GB on 4 GB.
# At 2000 the Pi got 3 browsers at 7.5 GB free and fell to 509 MB free once all three had a page loaded (scan 11).
BROWSER_MB = 2500
RESERVE_MB = 1500  # left for everything else; with 1000 the Pi got the 3 browsers that took it to 566 MB free


def browser_count(db):
    """(plain browsers, browsers in all, free MB) for a stealth phase: about one browser per 2 GB free beyond the
    reserve, the Cloudflare solver counted as one of them, never more than the owner's setting allows, at least one
    plain browser. Where free memory can't be read (Windows, macOS) the owner's setting decides."""
    setting = max(1, int(number(db, "stealth_workers", 2, 4)))
    free = free_memory_mb()
    if free is None:
        return setting, setting + 1, None
    total = max(1, min((free - RESERVE_MB) // BROWSER_MB, setting + 1))
    return max(1, total - 1), max(2, total), free


ADDRESS = re.compile(r"""(https?://[^/\s?#'"<>]+)[^\s'"<>]*""")


class NoQueries(logging.Filter):
    """Search addresses carry the person's name, city or email (people-search sites put the name in the path:
    /name/jane-doe_columbus-oh), and the browser library logs every address it fetches and puts it in its timeout
    messages. The journal gets the site, never the rest of the address."""

    def filter(self, record):
        cut = lambda s: ADDRESS.sub(r"\1/[…]", s)
        record.msg, record.args = cut(record.getMessage()), ()
        if record.exc_info:  # tracebacks carry the address too ("navigating to <url>")
            record.exc_text = logging.Formatter().formatException(record.exc_info)
            record.exc_info = None
        if record.exc_text:
            record.exc_text = cut(record.exc_text)
        if record.stack_info:
            record.stack_info = cut(record.stack_info)
        return True


def quiet_logs():
    """Warnings and errors only from the browser libraries, and no addresses past the site in anything logged.
    The filter sits on the handlers, not the loggers: a logger's filter never sees records that come up from its
    children ("scrapling.engines"), a handler's sees everything it prints. Scrapling prints through its own handler
    and stops there; everything else goes to one filtered handler on the root (instead of logging's last resort)."""
    import scrapling.core.utils._utils  # noqa: F401  sets up the "scrapling" logger and its handler on import
    for name in ("scrapling", "patchright", "playwright"):
        logging.getLogger(name).setLevel(logging.WARNING)
    scr = logging.getLogger("scrapling")
    scr.propagate = False  # its own handler prints it; the root's would print it twice
    root = logging.getLogger()
    if not root.handlers:
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(name)s: %(message)s"))
        h.setLevel(logging.WARNING)
        root.addHandler(h)
    for h in scr.handlers + root.handlers + [logging.lastResort]:
        if h is not None and not any(isinstance(f, NoQueries) for f in h.filters):
            h.addFilter(NoQueries())


def free_memory_mb():
    """MemAvailable in MB, or None where there's no /proc/meminfo (Windows, macOS)."""
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except OSError:
        pass
    return None


def kill_browser(s):
    """Stop a browser from outside its own thread: kill its Playwright driver and everything under it.
    Its thread's stuck call then fails like a crashed page and the worker starts a fresh browser."""
    try:
        pid = s.playwright._impl_obj._connection._transport._proc.pid
    except AttributeError:
        return False
    kids = {}
    for d in Path("/proc").glob("[0-9]*"):  # Linux: parent of each process (field 4 of /proc/<pid>/stat)
        try:
            kids.setdefault(int((d / "stat").read_text().rsplit(")", 1)[1].split()[1]), []).append(int(d.name))
        except (OSError, ValueError, IndexError):
            continue
    tree, todo = [], [pid]
    while todo:
        p = todo.pop()
        tree.append(p)
        todo += kids.get(p, [])
    for p in tree:
        try:
            os.kill(p, getattr(signal, "SIGKILL", signal.SIGTERM))
        except OSError:
            pass
    return True


class Browsers:
    """Stealth browsers shared by one in_parallel run: `size` plain-stealth Chromiums plus one with the Cloudflare
    solver (adds ~5 s a page, so it only sees pages that came back blocked).

    A Playwright browser belongs to the thread that started it: it can only be used and closed from that thread
    (on PiCloud, using one from another thread failed and left ~200 Chromium processes running). So each browser
    lives in its own worker thread for the whole run; scan threads hand it addresses through a queue and wait for
    the answer. The worker that started a browser is the one that closes it."""
    STOP = object()

    def __init__(self, db, size, factory=None, killer=None, single=False):
        """single: one browser in all, which also does the Cloudflare pages (the solver switched on per page). Used
        where memory is short: two browsers alive on 3.6 GB free took a 4 GB machine to 250 MB (VM 2026-10-30)."""
        self.db, self.size, self.single = db, max(1, size), single
        self.factory = factory or self._make
        self.killer = killer or kill_browser
        self.stopped = set()  # browsers killed on purpose: their close fails, and that's expected
        self.inflight = {}  # id(holder) -> when a worker started on that page
        self.pages = 0  # pages a worker has started and not finished
        self.memory_kill = float("-inf")  # last time low memory stopped a page: one per check, across the pool
        self.queues = {False: queue.Queue(), True: queue.Queue()}
        self.workers = {False: [], True: []}
        self.lock = threading.Lock()
        self.errors = []

    def _make(self, solve):
        from scrapling.fetchers import StealthySession
        s = StealthySession(headless=True, solve_cloudflare=solve, network_idle=False, wait=SETTLE_MS, block_ads=BLOCK_ADS,
                            disable_resources=True,
                            proxy=self.db.get("proxy") or None, timeout=90000, **BROWSER_LIMITS)
        s.start()
        return s

    def _close(self, s):
        try:
            s.close()
        except Exception as ex:  # a browser that won't close is reported, not hidden
            if id(s) in self.stopped:
                return
            with self.lock:
                self.errors.append(("closing a browser", f"{type(ex).__name__}: {str(ex)[:120]}"))

    def _work(self, solve):
        s = None
        q = self.queues[solve]
        try:
            while True:
                item = q.get()
                if item is self.STOP:
                    return
                url, box, done, holder, wants_solver = item
                self._room()
                try:
                    if s is None:
                        s = self.factory(solve)
                    holder.append(s)
                    box.append(("ok", s.fetch(url, solve_cloudflare=True) if self.single and wants_solver else s.fetch(url)))
                except Exception as ex:
                    box.append(("err", ex))
                    if s is not None:  # a browser whose page crashed may be in any state: start a fresh one
                        self._close(s)
                        s = None
                finally:
                    with self.lock:
                        self.pages -= 1
                    done.set()
        finally:
            if s is not None:
                self._close(s)

    def _room(self):
        """Called by a browser's worker just before it starts a page. Don't open a page into low memory while another
        of ours is still loading: wait for it to finish. With nothing of ours loading, go anyway (memory held by
        something else would otherwise stall the scan forever); the low-memory stop in fetch() is the backstop.
        Checked here, not when the page is queued: a page queued while memory was fine may start much later
        (VM 2026-10-30: six queued at 3.6 GB free, then two browsers loading at once took it to 327 MB).
        The wait doesn't count toward FETCH_LIMIT."""
        while True:
            with self.lock:
                free = free_memory_mb()
                if not self.pages or free is None or free >= LOW_MEMORY_MB + BROWSER_MB:
                    self.pages += 1
                    return
            time.sleep(CHECK_EVERY)

    def fetch(self, url, solve):
        key = False if self.single else solve  # single: the one plain browser takes the Cloudflare pages too
        with self.lock:
            if not self.workers[key]:
                for _ in range(1 if key or self.single else self.size):
                    t = threading.Thread(target=self._work, args=(key,), daemon=True)
                    t.start()
                    self.workers[key].append(t)
        box, done, holder = [], threading.Event(), []
        self.queues[key].put((url, box, done, holder, solve))
        try:
            while not done.wait(CHECK_EVERY):
                if not holder:  # still queued behind another page: that page has its own limit
                    continue
                now = time.monotonic()
                with self.lock:
                    t0 = self.inflight.setdefault(id(holder), now)
                why = None
                if now - t0 > FETCH_LIMIT:
                    why = f"the page didn't finish within {FETCH_LIMIT} s"
                elif (free := free_memory_mb()) is not None and free < LOW_MEMORY_MB:
                    with self.lock:  # one page per check, the one running longest; the rest get to finish
                        if now - self.memory_kill >= CHECK_EVERY and t0 <= min(self.inflight.values()):
                            self.memory_kill = now
                            why = f"memory ran low ({free} MB free)"
                if why:
                    with self.lock:
                        self.stopped.add(id(holder[0]))
                    if not self.killer(holder[0]):
                        with self.lock:
                            self.errors.append(("stopping a page", "couldn't find the browser's process"))
                    elif not done.wait(LET_GO):
                        with self.lock:
                            self.errors.append(("stopping a page", "a stopped browser didn't let go within a minute"))
                    raise TimeoutError(why)
        finally:
            with self.lock:
                self.inflight.pop(id(holder), None)
        kind, value = box[0]
        if kind == "err":
            raise value
        return value

    def close(self):
        for solve, ts in self.workers.items():
            for _ in ts:
                self.queues[solve].put(self.STOP)
            for t in ts:
                t.join(120)
                if t.is_alive():  # a browser that won't finish: say so instead of returning as if all is well
                    self.errors.append(("closing a browser", "a browser didn't shut down within 2 minutes"))
            ts.clear()
        errors, self.errors = self.errors, []
        return errors


_tls = threading.local()
NO_NETWORK = os.environ.get("GUARD_DOG_NO_NETWORK") == "1"  # tests: any real fetch is a bug


def get(url, db, stealth, solve=False):
    """The raw Scrapling response. Stealth fetches borrow a browser from the running in_parallel's pool."""
    if NO_NETWORK:
        raise RuntimeError(f"network call in a no-network run: {url}")
    if stealth:
        pool = getattr(_tls, "browsers", None)
        if pool is None:  # outside a scan run: a one-off browser
            from scrapling.fetchers import StealthyFetcher
            return StealthyFetcher.fetch(url, headless=True, solve_cloudflare=solve, network_idle=False, wait=SETTLE_MS,
                                         block_ads=BLOCK_ADS,
                                         disable_resources=True, proxy=db.get("proxy") or None, timeout=90000)
        return pool.fetch(url, solve)
    from scrapling.fetchers import Fetcher
    return Fetcher.get(url, impersonate="chrome", timeout=30, follow_redirects=True, proxy=db.get("proxy") or None,
                       retries=1)


def fetch(url, db, stealth=None, solve=False):
    """Returns (status, html, text, how, final address). Stealth only when the owner switched it on."""
    if stealth is None:
        stealth = db.get("stealth") == "1"
    p = get(url, db, stealth, solve)
    html = p.html_content if hasattr(p, "html_content") else str(p.body)
    text = p.get_all_text(ignore_tags=("script", "style", "noscript"))
    return (p.status, html, text, ("stealth+solve" if solve else "stealth") if stealth else "plain",
            str(getattr(p, "url", "") or ""))


def in_parallel(groups, work, workers, pause, cancelled, browsers=None):
    """Run groups side by side; each group is one site, worked in order with a pause between its pages,
    so no single site gets hammered. Stealth fetches share `browsers` (closed at the end).
    Returns [(item, error text)] for every item whose work raised."""
    from concurrent.futures import ThreadPoolExecutor
    errors = []

    def setup():
        _tls.browsers = browsers

    def go(items):
        for n, it in enumerate(items):
            if cancelled():
                return
            if n:
                pause()
            try:
                work(it)
            except Exception as ex:
                errors.append((it, f"{type(ex).__name__}: {str(ex)[:120]}"))
    try:
        with ThreadPoolExecutor(max_workers=max(1, workers), initializer=setup) as ex:
            list(ex.map(go, [g for g in groups if g]))
    finally:
        if browsers is not None:
            errors += browsers.close()
    return errors


def save_copy(db, pid, source, url, html):
    """Keep a copy only when the page changed, and only the newest few per search."""
    h = hashlib.sha256(html.encode()).hexdigest()[:12]
    key = f"{pid}-{re.sub(r'[^a-z0-9.-]', '_', source.lower())}-{hashlib.sha256(url.encode()).hexdigest()[:8]}-"
    folder = db.dir / "copies"
    mine = sorted(folder.glob(key + "*.html"))
    if mine and mine[-1].name.endswith(f"-{h}.html"):
        return mine[-1].name
    name = f"{key}{time.strftime('%Y%m%d-%H%M%S')}-{h}.html"
    (folder / name).write_text(html, encoding="utf-8")
    for old in mine[:max(0, len(mine) - (KEEP_COPIES - 1))]:
        old.unlink(missing_ok=True)
    return name


def record(db, pid, b, url, status, matched, copy):
    """Store what this check proved. A check that proved nothing (blocked, error, needs stealth, unconfirmed)
    never overwrites an earlier proven result; it only notes when we last tried."""
    now = time.time()
    source = b["id"]
    old = db.q("SELECT * FROM findings WHERE person=? AND source=? AND url=?", (pid, source, url))
    old = dict(old[0]) if old else None
    note = "; ".join(matched)
    if status not in PROVEN:
        if old and old["status"] in PROVEN:
            if old["check_note"] == UNSEEN:  # proves nothing either way: the listing still waits for a second look
                db.x("UPDATE findings SET last_checked=? WHERE id=?", (now, old["id"]))
            else:
                db.x("UPDATE findings SET last_checked=?, check_note=? WHERE id=?", (now, f"{status} {note}".strip(), old["id"]))
            return
        db.x("""INSERT INTO findings(person, source, url, status, matched, copy, first_seen, last_seen, last_checked,
                check_note, kind) VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(person, source, url) DO UPDATE SET
                status=excluded.status, copy=COALESCE(excluded.copy, findings.copy), last_checked=excluded.last_checked,
                check_note=excluded.check_note""",
             (pid, source, url, status, "", copy, now, now, now, note, b.get("kind", "broker")))
        return
    was = old["status"] if old else None
    rstate = removal.state(db, old["id"]) if old else "found"
    pending = bool(old and old["check_note"] == UNSEEN)  # last scan didn't see a listing this one may confirm
    keep = ""
    if status == "listed" and was != "listed":
        if rstate == "gone":  # proven gone once, now back: they need asking again
            removal.move(db, old["id"], "back", note="listed again on a re-scan")
            db.alert(pid, f"{b['name']}: the listing is back after it was removed. Ask them again.")
        elif pending:
            db.alert(pid, f"{b['name']}: the listing shows again. Last scan didn't see it, so that miss wasn't real.")
        elif not (old and old["ever_listed"]):
            db.alert(pid, f"{b['name']}: new listing found.")
    if status == "not found" and old and old["ever_listed"] and rstate != "gone":
        # One miss could be a page we misread (a new captcha wording, say): gone only when two scans in a row agree.
        if was != "not found":
            keep = UNSEEN
            db.alert(pid, f"{b['name']} no longer shows the listing; proven when the next scan agrees.")
        elif rstate in ("requested", "confirmed"):
            removal.move(db, old["id"], "gone", note="two re-scans in a row no longer find it")
            db.alert(pid, f"{b['name']}: the listing is gone.")
        elif pending:
            db.alert(pid, f"{b['name']}: the listing is gone (two scans in a row didn't find it).")
    db.x("""INSERT INTO findings(person, source, url, status, matched, copy, first_seen, last_seen, last_checked,
            check_note, ever_listed, kind) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(person, source, url) DO UPDATE SET status=excluded.status, matched=excluded.matched,
            copy=COALESCE(excluded.copy, findings.copy), last_seen=excluded.last_seen,
            last_checked=excluded.last_checked, check_note=excluded.check_note,
            ever_listed=MAX(findings.ever_listed, excluded.ever_listed)""",
         (pid, source, url, status, note, copy, now, now, now, keep, int(status == "listed"), b.get("kind", "broker")))


def refetch_details(db, pid, prog=lambda s: None):
    """Sites waiting on the person (a captcha only they can tick) whose card has nothing to paste yet: run the dry run
    again, once per scan, so the profile address and removal email appear on the card. Only from a scan, never from
    opening a page."""
    from . import removal
    from .optouts import SCRIPTS
    for r in db.q("""SELECT f.* FROM findings f JOIN removals m ON m.finding=f.id WHERE f.person=? AND m.state='manual'
                     AND (m.prefill IS NULL OR m.prefill IN ('', '{}'))""", (pid,)):
        script = SCRIPTS.get(r["source"])
        if script is None:
            continue
        prog(f"getting the details for {r['source']} again")
        try:
            removal.run(db, dict(r), script, "dry_run", removal.browser_page(db))
        except Exception as ex:  # a BadMove or a browser failure: say so, the scan goes on
            db.alert(pid, f"people-search check failed ({r['source']}): {type(ex).__name__}: {str(ex)[:120]}")


def supersede(db, pid, current_urls):
    """People-search rows whose search address this scan no longer uses (the person's details changed, or an older
    version searched differently) are kept for history but no longer shown or counted. A row with a removal in
    progress stays, so nothing in flight disappears. With no search addresses at all (only one-word names) there is
    nothing to compare against, so nothing is hidden."""
    if not current_urls:
        return
    for r in db.q("""SELECT id, url FROM findings WHERE person=? AND kind='broker' AND status != 'superseded'
                     AND id NOT IN (SELECT finding FROM removals WHERE state NOT IN ('found', 'failed'))""", (pid,)):
        if r["url"] not in current_urls:
            db.x("UPDATE findings SET status='superseded', check_note='an older search address, no longer used' WHERE id=?",
                 (r["id"],))


def number(db, k, default, top):
    try:
        v = float(db.get(k))
    except (TypeError, ValueError):
        return default
    return min(max(v, 0.0), top) if math.isfinite(v) else default


def check_page(db, pid, person, b, url, qn, qp, stealth):
    """Fetch one page and record what it shows. Plain first (faster); the stealth browser for sites known to
    block bots, or as a retry when a plain fetch is blocked, and only when the owner allows it."""
    try:
        first = stealth and b.get("blocks_bots", False)
        st, html, text, how, at = fetch(url, db, first)
        if stealth and not first and blocked_page(st, text, at):
            st, html, text, how, at = fetch(url, db, True)
        if stealth and blocked_page(st, text, at):  # a challenge page: now bring in the Cloudflare solver
            st, html, text, how, at = fetch(url, db, True, solve=True)
    except Exception as ex:
        record(db, pid, b, url, "error", [str(ex)[:120]], None)
        return "error"
    if blocked_page(st, text, at):
        s = "blocked" if stealth else "needs stealth"
        record(db, pid, b, url, s, [f"http {st}"], None)
        return s
    if st in (404, 410) or (st == 200 and match(text, person, qn, qp)[0] == "not found"):
        # A miss only counts where this site's search pattern is known to work.
        s = "not found" if b.get("checked") else "unconfirmed"
        was = db.q("SELECT ever_listed FROM findings WHERE person=? AND source=? AND url=?", (pid, b["id"], url))
        # A listing that seems to have gone: keep the page as evidence of what the site showed instead
        copy = save_copy(db, pid, b["id"], url, html) if s == "not found" and was and was[0]["ever_listed"] and html else None
        record(db, pid, b, url, s, [] if b.get("checked") else ["this site's search address isn't confirmed yet"], copy)
        return s
    if st != 200:
        record(db, pid, b, url, "error", [f"http {st}"], None)
        return "error"
    status, got = match(text, person, qn, qp)
    record(db, pid, b, url, status, got, save_copy(db, pid, b["id"], url, html))
    return status


def run(db, pid):
    person = db.person(pid)
    stealth = db.get("stealth") == "1"
    sid = db.x("INSERT INTO scans(person, started, state, progress, stealth) VALUES(?,?,?,?,?)",
               (pid, time.time(), "running", "starting", int(stealth)))
    prog = lambda s: db.x("UPDATE scans SET progress=? WHERE id=?", (s, sid))
    lo = number(db, "delay_min", 2, 120)
    hi = max(lo, number(db, "delay_max", 6, 120))
    pause = lambda: time.sleep(random.uniform(lo, hi))
    try:
        key = db.get("hibp_key")
        for n, e in enumerate(person["emails"], 1):
            if pid in _cancel:
                return
            prog(f"breach check {n}/{len(person['emails'])}")  # no address: progress text lands in logs
            try:
                found = breaches.hibp(e, key) if key else breaches.xposedornot(e)
            except Exception as ex:
                db.alert(pid, f"breach check for {e} failed: {ex}")
                continue
            for b in found:
                new = db.n("""INSERT OR IGNORE INTO breaches(person, email, breach, domain, date, exposed, password_risk,
                              first_seen) VALUES(?,?,?,?,?,?,?,?)""",
                           (pid, e, b["breach"], b["domain"], b["date"], b["exposed"], b["password_risk"], time.time()))
                if new and person["last_scan"]:
                    db.alert(pid, f"{e} is in a new breach: {b['breach']}.")

        if archive.due(db, person):
            prog("old copies of profiles in the Internet Archive")
            hits, problems = archive.check(person, cancelled=lambda: pid in _cancel)
            if pid in _cancel:
                return
            archive.record(db, pid, hits, first_time=not person.get("archive_checked"), complete=not problems)
            if problems:
                db.alert(pid, f"archive check: {len(problems)} problem{'s' if len(problems) != 1 else ''}: {problems[0]}")
        if accounts.due(db, person):
            prog("accounts with their usernames")
            found, problems = accounts.check(db, person, cancelled=lambda: pid in _cancel)
            if pid in _cancel:
                return
            accounts.record(db, pid, found, first_time=not person.get("accounts_checked"), complete=not problems)
            if problems:
                db.alert(pid, f"accounts check: {len(problems)} problem{'s' if len(problems) != 1 else ''}: {problems[0]}")

        workers = max(1, int(number(db, "workers", 6, 16)))
        cancelled = lambda: pid in _cancel

        def browsers():
            """A fresh pool for one phase, sized to the memory free now (the last phase's browsers are closed)."""
            if not stealth:
                return Browsers(db, 1)  # never started: plain fetches only
            plain, total, free = browser_count(db)
            single = total <= 2  # one plain and the solver would be two alive: one browser does both instead
            n = 1 if single else total
            prog(f"{n} browser{'s' if n != 1 else ''}" + (f", {free / 1024:.1f} GB free" if free is not None else ""))
            return Browsers(db, plain, single=single)
        done = [0]
        dlock = threading.Lock()

        def ticker(label, total):
            def tick(name):
                with dlock:
                    done[0] += 1
                    prog(f"{label} {done[0]}/{total}: {name}")
            return tick

        groups = []
        for b in BROKERS:
            urls = search_urls(b, person)
            if b["blocks_bots"] and not stealth:
                for u, qn, qp in urls:
                    record(db, pid, b, u, "needs stealth", [], None)
            else:
                groups.append([(b, u, qn, qp) for u, qn, qp in urls])
        tick = ticker("people-search sites", sum(map(len, groups)))

        def broker_job(j):
            b, u, qn, qp = j
            tick(b["name"])
            check_page(db, pid, person, b, u, qn, qp, stealth)
        for it, why in in_parallel(groups, broker_job, workers, pause, cancelled, browsers()):
            db.alert(pid, f"people-search check failed ({it[0]['name'] if isinstance(it, tuple) else it}): {why}")
        if cancelled():
            return
        supersede(db, pid, {u for b in BROKERS for u, _, _ in search_urls(b, person)})
        refetch_details(db, pid, prog)

        if db.get("sweep") != "0":
            limit = int(number(db, "sweep_limit", 200, 2000))
            hits, failed = sweep.search(db, person, prog, pause, cancelled, workers, browsers())
            per = max(1, len(sweep.queries(person)))
            for eng, why in failed.items():
                kinds = ", ".join(sorted(set(why)))
                db.alert(pid, f"web search: {eng} failed {len(why)} of {per} searches ({kinds}); the sweep missed what it would have found.")
            known = {r["url"] for r in db.q("SELECT url FROM findings WHERE person=?", (pid,))}
            todo = ([h for h in hits if h["url"] not in known] + [h for h in hits if h["url"] in known])[:limit]
            by_site = {}
            for h in todo:
                by_site.setdefault(h["domain"], []).append(h)
            done[0] = 0
            tick = ticker("web sweep", len(todo))

            def page_job(h):
                tick(h["domain"])
                check_page(db, pid, person, sweep.source_for(h), h["url"], h["name"], h["place"], stealth)
            for it, why in in_parallel(list(by_site.values()), page_job, workers, pause, cancelled, browsers()):
                db.alert(pid, f"web page check failed ({it['domain'] if isinstance(it, dict) else it}): {why}")

        db.x("UPDATE people SET last_scan=? WHERE id=?", (time.time(), pid))
        db.x("UPDATE scans SET state='done', finished=?, progress='done' WHERE id=?", (time.time(), sid))
    except Exception as ex:
        db.x("UPDATE scans SET state='failed', finished=?, progress=? WHERE id=?", (time.time(), str(ex)[:200], sid))
    finally:
        with _lock:
            _running.pop(pid, None)
            if pid in _cancel:
                _cancel.discard(pid)
                db.delete_person(pid)  # deleted mid-scan: clear what this scan wrote


def start(db, pid):
    with _lock:
        if pid in _running:
            return False
        _running[pid] = True
    threading.Thread(target=run, args=(db, pid), daemon=True).start()
    return True


def delete(db, pid):
    """Delete a person; if a scan is running for them, it stops and cleans up after itself."""
    with _lock:
        if pid in _running:
            _cancel.add(pid)
    db.delete_person(pid)


def running(pid):
    return pid in _running


def scheduler(db):
    """Keep watch around the clock: re-scan each person once their last scan is older than the owner's
    interval (6 hours by default), one person at a time. Never starts a first scan unasked."""
    from . import inbox, requests
    # A scan still marked running belongs to a Guard Dog that stopped mid-scan (a crash, a power cut, a restart)
    db.x("UPDATE scans SET state='failed', finished=?, progress='stopped: Guard Dog restarted' WHERE state='running'",
         (time.time(),))
    last_look = 0.0
    while True:
        pid = due(db)
        if pid is not None and not _running:
            start(db, pid)
        requests.send_waiting(db)
        if time.time() - last_look >= inbox.POLL_EVERY:
            last_look = time.time()
            try:
                inbox.poll(db)
            except Exception as ex:  # a wrong password or no network: say so once per look, keep watching
                for p in db.people():
                    db.alert(p["id"], f"Couldn't read the removal inbox: {type(ex).__name__}: {str(ex)[:100]}")
        time.sleep(300)


def due(db, now=None):
    """The person to re-scan next, or None: the longest-waiting one whose last scan is older than the interval.
    Someone never scanned is never due (their first scan is the owner's call)."""
    hours = number(db, "rescan_hours", 6, 720)
    if hours <= 0:
        return None
    now = time.time() if now is None else now
    waiting = [p for p in db.people() if p["last_scan"] and now - p["last_scan"] > max(hours, 1) * 3600]
    return min(waiting, key=lambda p: p["last_scan"])["id"] if waiting else None

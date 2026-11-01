"""Removals: the states a listing moves through, the evidence each step leaves, and the form-script runner.

found → ready (a dry run filled the form and stopped) → requested (submitted) → confirmed (the site's emailed link
was followed) → gone (a re-scan no longer finds it) → back (it came back; ask again). Sites that need a picture
captcha or a phone call go to "manual" with the exact steps. Nothing is ever submitted except from the owner's
Submit button; a dry run never presses a site's submit button.
"""
import datetime as dt
import json
import re
import time
from pathlib import Path

STATES = ("found", "ready", "requested", "confirmed", "gone", "back", "manual", "failed")
ALLOWED = {
    "found": {"ready", "manual", "failed"},
    "ready": {"ready", "requested", "manual", "failed"},
    "requested": {"confirmed", "gone", "failed"},
    "confirmed": {"gone", "failed"},
    "gone": {"back"},
    "back": {"ready", "manual", "failed"},
    "manual": {"manual", "ready", "requested", "gone"},  # manual → manual: details fetched again for the person
    "failed": {"ready", "manual"},
}
# What a form script may type into a broker's form, and nothing else (never a phone, a second email, an address
# or a relative).
TYPEABLE = ("listing_url", "name_as_shown", "removal_email")



class BadMove(ValueError):
    pass


class NeedsHand(Exception):
    """The site put up something only a person should do (a picture captcha, a phone call)."""


def pick_record(cards, pick):
    """Their record among [(address, card text)]: the first the matcher calls "listed", else the first "possible".
    Raises LookupError when none of the cards is them."""
    from .scanner import match
    person = {"names": list(pick["names"]), "places": list(pick["cities"]), "relatives": list(pick["relatives"]),
              "birth_year": pick["birth_year"], "phones": [], "emails": [], "usernames": []}
    best = None
    for url, text in cards:
        st = match(text, person)[0]
        if st == "listed":
            return url
        if st == "possible" and best is None:
            best = url
    if best:
        return best
    raise LookupError(f"their record isn't among the {len(cards)} on this page")


def state(db, finding):
    r = db.q("SELECT state FROM removals WHERE finding=?", (finding,))
    return r[0]["state"] if r else "found"


def move(db, finding, to, note="", evidence="", force=False):
    """Change a removal's state, refusing moves the flow doesn't allow. Re-submitting a request needs force
    (the owner asked for it explicitly)."""
    if to not in STATES:
        raise BadMove(f"unknown state {to}")
    now = state(db, finding)
    if to not in ALLOWED[now] and not (force and now == "requested" and to == "requested"):
        raise BadMove(f"{now} → {to} isn't allowed")
    r = db.q("SELECT history FROM removals WHERE finding=?", (finding,))
    hist = json.loads(r[0]["history"]) if r else []
    hist.append({"from": now, "to": to, "at": time.time(), "note": note, "evidence": evidence})
    db.x("""INSERT INTO removals(finding, state, updated, evidence, note, history) VALUES(?,?,?,?,?,?)
            ON CONFLICT(finding) DO UPDATE SET state=excluded.state, updated=excluded.updated,
            evidence=COALESCE(NULLIF(excluded.evidence, ''), removals.evidence), note=excluded.note,
            history=excluded.history""", (finding, to, time.time(), evidence, note, json.dumps(hist)))
    return to


def what_to_type(db, finding_row, script):
    """The only values a form script gets: the removal email, the listing's own address, and the name exactly as
    the site showed it. Anything the script didn't declare isn't given to it."""
    email = db.get("removal_email").strip()
    if "removal_email" in script.fields and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise BadMove("set a removal email in Settings first")
    shown = re.findall(r"name: ([^;]+)", finding_row.get("matched") or "")
    have = {"listing_url": finding_row["url"], "name_as_shown": shown[0].strip() if shown else "", "removal_email": email}
    return {k: have[k] for k in script.fields if k in TYPEABLE}


def pick_for(db, finding_row):
    """What identifies their record on a site (never typed): names, cities, relatives, birth year — read-only."""
    from types import MappingProxyType

    from .brokers import split_place
    p = db.person(finding_row["person"]) or {}
    cities = tuple(c for c in (split_place(x)[0] for x in p.get("places", [])) if c)
    return MappingProxyType({"names": tuple(p.get("names", [])), "cities": cities,
                             "relatives": tuple(p.get("relatives") or []), "birth_year": p.get("birth_year")})


def ready_is_fresh(db, finding, hours=24):
    r = db.q("SELECT state, updated FROM removals WHERE finding=?", (finding,))
    return bool(r) and r[0]["state"] == "ready" and time.time() - r[0]["updated"] < hours * 3600


CONSENT = "[id*=osano i], [class*=osano i]"
DECLINE = re.compile(r"reject|decline|deny|necessary only|only necessary|essential only|do not accept|refuse", re.I)


def decline_consent(page, ev):
    """A cookie banner in the way: press its reject/decline button, never "accept". Logged either way."""
    if not any(el.is_visible() for el in page.query_selector_all(CONSENT)):
        return False
    seen = set()
    for el in page.query_selector_all(", ".join(f"{s} button, {s} a, {s} [role=button]" for s in CONSENT.split(", "))):
        key = el.evaluate("e => e.outerHTML")
        if key in seen or not el.is_visible():
            continue
        seen.add(key)
        label = (el.inner_text() or el.get_attribute("aria-label") or "").strip()
        if DECLINE.search(label):
            try:
                el.click(timeout=5000)
            except Exception:  # something on the page sits over the button: press it directly
                el.evaluate("e => e.click()")
            ev.step(f"cookie banner: pressed '{label[:40]}'")
            return True
    ev.step("cookie banner: no reject or decline button on it, left alone")
    return False


class Evidence:
    """A folder per run: steps.log, numbered screenshots, result.json."""

    def __init__(self, root, finding, mode):
        self.prefill = {}  # what the person will need to paste if the site makes them finish by hand
        self.dir = Path(root) / "removals" / str(finding) / f"{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-{mode}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.n = 0

    def step(self, what, page=None):
        self.n += 1
        with open(self.dir / "steps.log", "a") as f:
            f.write(f"{dt.datetime.now().isoformat(timespec='seconds')} {self.n:02d} {what}\n")
        if page is not None:
            safe = re.sub(r"[^a-z0-9]+", "-", what.lower()).strip("-")[:40]
            page.screenshot(path=str(self.dir / f"{self.n:02d}-{safe}.png"), full_page=True)

    def form_shape(self, page):
        """What the page's form fields looked like, without their values: for working out why a run stopped."""
        try:
            fields = page.evaluate("""() => [...document.querySelectorAll('input,textarea,select,button,iframe')].map(e => ({
                tag: e.tagName.toLowerCase(), name: e.name || '', type: e.type || '', id: e.id || '',
                visible: !!(e.offsetWidth || e.offsetHeight), filled: !!(e.value && e.value.length),
                invalid: !!(e.matches && e.matches(':invalid')), title: (e.title || '').slice(0, 60),
                note: (e.getAttribute('aria-describedby') || '')}))""")
            msgs = page.evaluate("""() => [...document.querySelectorAll('[role=alert],.error,.errors,[class*=error]')]
                .map(e => e.innerText.trim()).filter(Boolean).slice(0, 10)""")
            banner = page.evaluate("""() => [...document.querySelectorAll('[id*=osano i],[class*=osano i]')]
                .some(e => !!(e.offsetWidth || e.offsetHeight))""")
            (self.dir / "form-shape.json").write_text(json.dumps({"url": page.url, "title": page.title(), "fields": fields,
                                                                   "messages_on_page": msgs, "consent_banner_visible": banner},
                                                                  indent=1) + "\n")
        except Exception as ex:  # the page may already be gone
            (self.dir / "form-shape.json").write_text(json.dumps({"error": f"{type(ex).__name__}: {ex}"}) + "\n")

    def result(self, **kw):
        (self.dir / "result.json").write_text(json.dumps(kw, indent=1) + "\n")
        return kw


class OptOut:
    """One broker's opt-out form. A script declares what it needs and how the site confirms, then fills the form in
    fill() and presses the button in press(). The runner calls press() only in submit mode."""
    site = ""             # the broker id this script handles
    start_url = ""        # the opt-out page
    fields = ()           # which of TYPEABLE it types
    reads = ()            # what else it types that it read from the site itself (e.g. the profile's own address)
    confirm = "email"     # email | none | phone | captcha
    domains = ()          # where its confirmation mail comes from and where its links may point

    def fill(self, page, data, pick, ev):
        """Type `data` (the only values allowed into the form). `pick` is read-only: their names, cities, relatives
        and birth year, used to find their own record among namesakes and never typed."""
        raise NotImplementedError

    def press(self, page, ev):
        raise NotImplementedError


def browser_page(db):
    """with_page for real use. With the owner's stealth switch on: Scrapling's stealth browser (it gets through
    Cloudflare checks). Off: a plain headless Chromium. Either way the steps run on the page and their error comes
    back out (Scrapling runs page_action inside its own fetch)."""
    stealth = db.get("stealth") == "1"

    def with_page(url, steps):
        from . import browser
        if browser.pin() and not browser.installed():  # packaged edition: the one browser is a download from Settings
            raise RuntimeError("this needs the hidden browser: Settings, Download it")
        err = []

        def act(page):
            try:
                steps(page)
            except Exception as ex:
                err.append(ex)
        if stealth:
            from scrapling.fetchers import StealthyFetcher
            from .scanner import BLOCK_ADS, SETTLE_MS
            StealthyFetcher.fetch(url, headless=True, network_idle=False, wait=SETTLE_MS, block_ads=BLOCK_ADS,
                                  solve_cloudflare=True, page_action=act,
                                  proxy=db.get("proxy") or None, timeout=90000)
        else:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                # the full browser, the one the packaged edition downloads (it has no headless shell)
                b = pw.chromium.launch(channel="chromium", headless=True,
                                       proxy={"server": db.get("proxy")} if db.get("proxy") else None)
                try:
                    page = b.new_page()
                    page.goto(url, wait_until="networkidle", timeout=90000)
                    act(page)
                finally:
                    b.close()
        if err:
            raise err[0]
    return with_page


def run(db, finding_row, script, mode, with_page):
    """Dry run or submit one removal. with_page(url, steps) opens the page and calls steps(page): the stealth
    browser in real use (stealth_page), a plain one in tests. Records evidence and moves the state."""
    if mode not in ("dry_run", "submit"):
        raise BadMove(f"unknown mode {mode}")
    fid = finding_row["id"]
    now = state(db, fid)
    if mode == "submit" and now not in ("ready", "manual"):
        raise BadMove(f"submit needs a dry run first (state is {now})")
    if mode == "submit" and now == "ready" and not ready_is_fresh(db, fid):
        raise BadMove("the dry run is more than 24 hours old: run it again, the page may have changed")
    if script.confirm in ("phone", "captcha"):
        move(db, fid, "manual", note=f"this site confirms by {script.confirm}: do it by hand")
        return {"state": "manual"}
    data = what_to_type(db, finding_row, script)
    pick = pick_for(db, finding_row)
    ev = Evidence(db.dir, fid, mode)
    ev.step(f"{mode} for {script.site}: will type {sorted(data)}")

    def steps(page):
        ev.step("opened the opt-out page", page)
        try:
            script.fill(page, data, pick, ev)
            ev.step("form filled", page)
            if mode == "submit":
                script.press(page, ev)
                ev.step("submitted", page)
        except Exception as ex:
            ev.step(f"the page when it stopped ({type(ex).__name__})", page)
            ev.form_shape(page)
            raise
    try:
        with_page(script.start_url or finding_row["url"], steps)
    except NeedsHand as ex:
        ev.step(f"needs a person: {ex}")
        move(db, fid, "manual", note=str(ex)[:200], evidence=str(ev.dir))
        db.x("UPDATE removals SET prefill=? WHERE finding=?", (json.dumps(ev.prefill), fid))
        return ev.result(state="manual", reason=str(ex)[:200], typed=sorted(data))
    except Exception as ex:
        ev.step(f"stopped: {type(ex).__name__}: {str(ex)[:200]}")
        move(db, fid, "failed", note=str(ex)[:200], evidence=str(ev.dir))
        return ev.result(state="failed", error=str(ex)[:200], typed=sorted(data))
    to = "ready" if mode == "dry_run" else "requested"
    move(db, fid, to, note=f"{mode} ok", evidence=str(ev.dir))
    return ev.result(state=to, typed=sorted(data), confirm=script.confirm)

"""Accounts under a person's usernames: maigret (MIT, soxoj/maigret) checks a profile address for each username on
its 500 most visited sites. Optional: installed with `pip install keephaven-guard-dog[accounts]`, and off until the
owner turns it on. Guard Dog runs it with its bundled site list (no update from the internet, so where a username
goes is fixed by the installed version), without its recursive search, page parsing or AI report, and with its
cache in Guard Dog's own folder. Each username goes to each of those sites."""
import importlib
import importlib.util
import json
import os
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from . import browser
from .archive import usernames

SITES = 500
EVERY_DAYS = 7
RUN_LIMIT = 900  # seconds for one username before it's given up on
PACKAGE = "maigret==0.6.6"  # pinned: its bundled site list decides where usernames go
# ...and so is every package it pulls in, with hashes: a bad release of any of them is refused, not installed
LOCK = Path(__file__).parent / "data" / "accounts-lock.txt"
INSTALL = {"state": "", "note": ""}  # "", "installing", "done", "failed"
NOT_HERE = "Not in this version yet: looking for accounts by username comes in a later update."


def installed():
    return importlib.util.find_spec("maigret") is not None


def packaged():
    """The .deb: its Python under /opt is read-only, so there's nothing to install maigret into."""
    return browser.pin() is not None


def install(run=None):
    """The owner pressed Add it: install maigret into Guard Dog's own Python, in the background."""
    if INSTALL["state"] == "installing":
        return
    if packaged():
        INSTALL.update(state="failed", note=NOT_HERE)
        return
    if not LOCK.is_file():
        INSTALL.update(state="failed", note="the list of checked packages is missing from this install")
        return
    INSTALL.update(state="installing", note="")  # before the thread: a second press finds it already going

    def go():
        try:
            p = (run or subprocess.run)(install_command(), capture_output=True, text=True, timeout=1800)
            importlib.invalidate_caches()
            if p.returncode == 0 and installed():
                INSTALL.update(state="done")
            else:
                INSTALL.update(state="failed", note=(p.stderr or p.stdout or "").strip().splitlines()[-1:][0][:160]
                               if (p.stderr or p.stdout) else f"pip stopped ({p.returncode})")
        except Exception as ex:
            INSTALL.update(state="failed", note=f"{type(ex).__name__}: {str(ex)[:120]}")
    threading.Thread(target=go, daemon=True).start()


def install_command():
    return [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-q", "--require-hashes",
            "-r", str(LOCK)]


def site_list():
    spec = importlib.util.find_spec("maigret")
    return str(Path(spec.origin).parent / "resources" / "data.json")


def command(username, out, proxy=""):
    cmd = [sys.executable, "-I", "-m", "maigret", username, "--db", site_list(), "--no-autoupdate",
           "--top-sites", str(SITES), "--timeout", "15", "--retries", "1", "--no-recursion", "--no-extracting",
           "--no-progressbar", "--no-color", "-J", "simple", "-fo", str(out)]
    return cmd + (["--proxy", proxy] if proxy else [])


def parse(report):
    """[{site, url, tags}] for the accounts maigret found (similar-name guesses left out)."""
    out = []
    for site, v in sorted(report.items()):
        st = v.get("status") or {}
        if st.get("status") == "Claimed" and not v.get("is_similar") and (v.get("url_user") or st.get("url")):
            out.append(dict(site=site, url=v.get("url_user") or st["url"], tags=", ".join(st.get("tags") or [])))
    return out


def control():
    """A made-up username nobody has: a site that 'finds' it finds everyone."""
    return "gd" + "".join(secrets.choice("abcdefghijkmnpqrstuvwxyz23456789") for _ in range(11))


def check(db, person, run=None, cancelled=lambda: False):
    """[{site, username, url, tags}] and [problems]. First a made-up name: some sites answer 'found' for any name
    (on PiCloud, 2 of the 500 claimed one on 2026-10-29, one of them an adult site); those are left out of every
    result. If that check fails, nothing is kept and the next scan tries again."""
    run = run or subprocess.run
    home = db.dir / "maigret"
    home.mkdir(exist_ok=True)

    def look(u):
        with tempfile.TemporaryDirectory(dir=home) as out:
            try:
                p = run(command(u, out, db.get("proxy")), capture_output=True, timeout=RUN_LIMIT,
                        env=dict(os.environ, HOME=str(home)), cwd=str(home))
            except subprocess.TimeoutExpired:
                return None, f"took longer than {RUN_LIMIT // 60} minutes"
            reports = list(Path(out).glob("*.json"))
            if p.returncode != 0 or not reports:
                return None, f"maigret stopped ({p.returncode})"
            return parse(json.loads(reports[0].read_text() or "{}")), ""

    names = usernames(person)
    if not names or cancelled():
        return [], []
    says_yes, why = look(control())
    if says_yes is None:
        return [], [f"the made-up name check: {why}"]
    noisy = {r["site"] for r in says_yes}
    found, problems = [], []
    for u in names:
        if cancelled():
            break
        got, why = look(u)
        if got is None:
            problems.append(f"{u}: {why}")
            continue
        found += [dict(r, username=u) for r in got if r["site"] not in noisy]
    return found, problems


def due(db, person, now=None):
    now = time.time() if now is None else now
    last = person.get("accounts_checked")
    return (db.get("accounts") == "1" and installed() and bool(usernames(person))
            and (not last or now - last >= EVERY_DAYS * 86400))


def record(db, pid, found, first_time, complete, now=None):
    """Keep what was found; an account not seen before is news after the first look."""
    now = time.time() if now is None else now
    for a in found:
        new = db.n("INSERT OR IGNORE INTO accounts(person, site, username, url, tags, found) VALUES(?,?,?,?,?,?)",
                   (pid, a["site"], a["username"], a["url"], a["tags"], now))
        if new and not first_time:
            db.alert(pid, f"New account with the username {a['username']} on {a['site']}.")
    if complete:
        db.x("UPDATE people SET accounts_checked=? WHERE id=?", (now, pid))

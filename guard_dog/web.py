"""The dashboard. Plain HTTP on 127.0.0.1 by default; any other address needs a password."""
import hashlib
import hmac
import html
import http.server
import ipaddress
import json
import math
import os
import re
import secrets
import socket
import socketserver
import threading
import time
import urllib.parse
from pathlib import Path
from collections import Counter

from . import __version__, accounts, archive, breaches, browser, inbox, removal, requests, scanner, sweep
from .optouts import SCRIPTS
from .brokers import BROKERS, covered_by, family_of, sister_note

BY_ID = {b["id"]: b for b in BROKERS}
SESSIONS = {}  # token -> expiry
SETUP_WINDOW = 30 * 60  # after a start, the first password can be set from your own network without the code
STARTED = time.time()
LOGIN_LOCK = threading.Lock()  # one password check at a time, whatever the number of threads
FAILS = {}  # client ip -> (failures, locked until)
e = html.escape

CSS = """
:root{--bg:#f6f5f2;--fg:#1c1c1c;--muted:#6b6b6b;--card:#fff;--line:#e3e1dc;--accent:#2f6f4f;--warn:#a5521b;--bad:#a12a2a}
@media (prefers-color-scheme:dark){:root{--bg:#141414;--fg:#ececec;--muted:#9a9a9a;--card:#1e1e1e;--line:#333;--accent:#6fc196;--warn:#e39b5f;--bad:#ef7b7b}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}
header{display:flex;gap:18px;align-items:center;padding:14px 20px;border-bottom:1px solid var(--line);flex-wrap:wrap}
header b{font-size:17px}header a{color:var(--fg);text-decoration:none}header a:hover{text-decoration:underline}
main{max-width:980px;margin:0 auto;padding:20px 16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin:0 0 14px}
h1{font-size:22px;margin:0 0 12px}h2{font-size:17px;margin:0 0 10px}
.k{color:var(--muted);font-size:13px}.big{font-size:28px;font-weight:600}
.row{display:flex;gap:14px;flex-wrap:wrap}.row>.card{flex:1;min-width:160px}
table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:7px 6px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:13px;color:var(--muted);font-weight:500}
.tag{display:inline-block;padding:1px 8px;border-radius:99px;font-size:12px;border:1px solid var(--line)}
.listed{color:var(--bad);border-color:var(--bad)}.stealth{color:var(--warn);border-color:var(--warn)}.clean{color:var(--accent);border-color:var(--accent)}
input,textarea,select{width:100%;padding:8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg);font:inherit}
textarea{min-height:70px}label{display:block;margin:10px 0 4px;font-weight:500}
button,.btn{display:inline-block;padding:7px 14px;border-radius:6px;border:1px solid var(--accent);background:var(--accent);color:#fff;font:inherit;cursor:pointer;text-decoration:none}
button.grey,.btn.grey{background:transparent;color:var(--fg);border-color:var(--line)}button.red{background:var(--bad);border-color:var(--bad)}
form.inline{display:inline}a{color:var(--accent)}
.switch{display:flex;align-items:center;gap:10px}.switch input{width:auto}
.lead{font-size:18px;margin:4px 0 12px}.site{border-top:1px solid var(--line);padding:12px 0}.site:first-of-type{border-top:0}
.site h3{font-size:16px;margin:0 0 4px}.pill{display:inline-block;padding:2px 10px;border-radius:99px;font-size:13px;font-weight:500;border:1px solid}
.p-bad{color:var(--bad);border-color:var(--bad)}.p-warn{color:var(--warn);border-color:var(--warn)}.p-ok{color:var(--accent);border-color:var(--accent)}
.p-quiet{color:var(--muted);border-color:var(--line)}.actions{margin-top:8px;display:flex;gap:8px;flex-wrap:wrap;align-items:center}
details{margin-top:6px}summary{cursor:pointer;color:var(--accent)}details .k{overflow-wrap:anywhere}
main{overflow-wrap:anywhere}button,.btn{padding:9px 16px}
"""


SIGN_OUT = False  # a password is in use: the nav offers Sign out (a shared computer needs a way to leave)
NETWORK_SWITCH = False  # the packaged edition: Settings opens the dashboard to the network, serve() rebinds
EVERYWHERE = "0.0.0.0"


def page(title, body, nav=True):
    links = ('<a href="/">People</a><a href="/password">Check a password</a><a href="/settings">Settings</a>'
             + ("<form method=post action=/logout style='display:inline'><button class=grey "
                "style='margin-left:12px'>Sign out</button></form>" if SIGN_OUT else "")
             if nav else "")
    return (f"<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,"
            f"initial-scale=1'><title>{e(title)} · Guard Dog</title><style>{CSS}</style></head><body>"
            f"<header><b>Keephaven Guard Dog</b>{links}</header><main>{body}</main></body></html>")


def when(ts):
    """'today 1:50 pm', 'yesterday 7:05 am', 'Oct 27', or 'never'."""
    if not ts:
        return "never"
    t, now = time.localtime(ts), time.localtime()
    clock = time.strftime("%I:%M %p", t).lstrip("0").lower()
    days = (time.mktime(time.strptime(time.strftime("%Y-%m-%d", now), "%Y-%m-%d"))
            - time.mktime(time.strptime(time.strftime("%Y-%m-%d", t), "%Y-%m-%d"))) // 86400
    if days == 0:
        return f"today {clock}"
    if days == 1:
        return f"yesterday {clock}"
    if days == -1:
        return f"tomorrow {clock}"
    return time.strftime("%b %d", t).replace(" 0", " ")


def lines(s):
    return [x.strip() for x in (s or "").replace("\r", "").split("\n") if x.strip()]


def pw_hash(pw, salt=None):
    salt = salt or secrets.token_hex(16)
    return salt + "$" + hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 200_000).hex()


def pw_ok(pw, stored):
    salt, _, _h = stored.partition("$")
    return bool(salt) and hmac.compare_digest(pw_hash(pw, salt), stored)


STATUS_TAG = {"listed": "listed", "possible": "stealth", "needs stealth": "stealth", "blocked": "stealth",
              "unconfirmed": "", "not found": "clean", "error": ""}
UNCHECKED = ("needs stealth", "blocked", "unconfirmed", "error")
# Sites whose search page isn't confirmed: a page without a listing there proves nothing, scan after scan
SEARCH_UNPROVEN = {b["id"] for b in BROKERS if not b.get("checked")}


def bounded(v, top):
    """A settings number: finite, 0..top, or None."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return min(max(f, 0.0), top) if math.isfinite(f) else None


def source_info(r):
    b = BY_ID.get(r["source"])
    return b or {"name": r["source"], "optout": sweep.GOOGLE_RAY, "how": sweep.GENERIC_HOW}


CGNAT = ipaddress.ip_network("100.64.0.0/10")


def on_tailscale():
    """100.64/10 is Tailscale's range only when this machine has a Tailscale address; otherwise it's an ISP's
    shared space (carrier-grade NAT) and the neighbours in it are strangers."""
    names = set(my_addresses())
    try:  # the address this machine would use to reach Tailscale's own DNS (no packet is sent)
        u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        u.connect(("100.100.100.100", 53))
        names.add(u.getsockname()[0])
        u.close()
    except OSError:
        pass
    for n in names:
        try:
            if ipaddress.ip_address(n) in CGNAT:
                return True
        except ValueError:
            pass
    return False


TAILSCALE = None


def own_network(ip):
    """Loopback, a home network (10/8, 172.16/12, 192.168/16, fd00::/8) or, with Tailscale here, 100.64/10."""
    global TAILSCALE
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if a.is_link_local:
        return False
    if a.version == 4 and a in CGNAT:
        if TAILSCALE is None:
            TAILSCALE = on_tailscale()
        return TAILSCALE
    return a.is_loopback or a.is_private


def setup_open(ip):
    return time.time() - STARTED < SETUP_WINDOW and own_network(ip)


REMOVAL_WORDS = {"requested": "requested", "confirmed": "confirmed by the site"}
RUNNING = set()  # findings with a removal run in progress
SENDING = set()  # people whose removal emails are going out
BY_EMAIL_NAME = {b["id"]: b["name"] for b in requests.brokers()}


def start_sending(db, pid):
    with RUN_LOCK:
        if pid in SENDING:
            return False
        SENDING.add(pid)

    def go():
        try:
            sent, later, failed = requests.send(db, pid)
            db.alert(pid, f"Sent {sent} removal email{'s' if sent != 1 else ''}."
                     + (f" {later} more go out over the next days." if later else "")
                     + (f" {failed} couldn't be sent; they're listed on the Remove from all page." if failed else ""))
        except Exception as ex:
            db.alert(pid, f"Couldn't send removal emails: {type(ex).__name__}: {str(ex)[:120]}")
        finally:
            with RUN_LOCK:
                SENDING.discard(pid)
    threading.Thread(target=go, daemon=True).start()
    return True
RUN_LOCK = threading.Lock()


def start_removal(db, row, mode):
    """Run a dry run or a submit in the background (a browser takes a while); the page shows "working…"."""
    with RUN_LOCK:
        if row["id"] in RUNNING:
            return False
        RUNNING.add(row["id"])

    def go():
        try:
            removal.run(db, row, SCRIPTS[row["source"]], mode, removal.browser_page(db))
        except removal.BadMove as ex:
            db.alert(row["person"], f"{SCRIPTS[row['source']].site}: {ex}")
        finally:
            with RUN_LOCK:
                RUNNING.discard(row["id"])
    threading.Thread(target=go, daemon=True).start()
    return True


def last_run(db, fid):
    r = db.q("SELECT evidence, note, updated, prefill FROM removals WHERE finding=?", (fid,))
    return dict(r[0]) if r else {}


def who(p):
    """Words for the person: 'you/your/You' when it's the owner ('Me'), otherwise their name."""
    label = (p.get("label") or "").strip()
    if label.lower() in ("", "me", "myself", "i", "you") and label != "":
        return {"you": "you", "your": "your", "You": "You", "Your": "Your"}
    name = label or (p["names"][0] if p.get("names") else "this person")
    return {"you": name, "your": f"{name}'s", "You": name, "Your": f"{name}'s"}


SHOWS = (("phone", "phone number"), ("email", "email address"), ("street address", "home address"), ("age", "age"),
         ("born", "birth year"), ("city", "city"), ("lives", "where they live"), ("relative", "relatives"),
         ("other name", "other names"), ("username", "username"), ("records, details hidden", "a record (details hidden)"))


def shows(matched):
    """'age, city and relatives' from the matcher's evidence."""
    got = []
    low = (matched or "").lower()
    for key, words in SHOWS:
        if key in low and words not in got and not (words == "where they live" and "city" in got):
            got.append(words)
    if not got:
        return ""
    return got[0] if len(got) == 1 else ", ".join(got[:-1]) + " and " + got[-1]


def companies(sources):
    """How many companies run these sites (sister sites count once)."""
    return len({(family_of(s) or {"id": s})["id"] for s in sources})


def tally(rows):
    """Findings by what the headline says about them."""
    def kind(r):
        if r["check_note"] == scanner.UNSEEN:
            return "not seen"
        if r["status"] == "unconfirmed" and r["source"] in SEARCH_UNPROVEN:
            return "not here"
        return r["status"]
    return Counter(kind(r) for r in rows)


def summary(p, c, nb, running, w, listed_sources=(), stealth=False):
    """One sentence that is true whatever state the scan is in."""
    listed, maybe = c["listed"], c["possible"]
    unchecked = sum(c[x] for x in UNCHECKED) + c["not here"]
    breach = f" {w['Your']} email was in {nb} data breach{'es' if nb != 1 else ''}." if nb else ""
    if running:
        return f"Checking sites for {w['you']} now. Results appear as they come in."
    if not p["last_scan"] and not sum(c.values()):
        return f"Not checked yet. Press Scan now to look for {w['you']}."
    if listed:
        more = f", and {maybe} more that might be {w['you']}" if maybe else ""
        sites = f"{listed} site that sells" if listed == 1 else f"{listed} sites that sell"
        n = companies(listed_sources)
        run = f" (run by {n} compan{'ies' if n != 1 else 'y'})" if listed_sources and n < listed else ""
        unseen = c["not seen"]
        gone = (f" {unseen} more that listed {w['you']} didn't show it this scan; the next scan checks again."
                if unseen else "")
        return f"We found {w['you']} on {sites} personal details{run}{more}." + gone + breach
    if maybe:
        return f"{maybe} site{'s' if maybe != 1 else ''} might list {w['you']}; check them below." + breach
    if c["not seen"]:  # listed before, missed once: not an all-clear until the next scan agrees
        n = c["not seen"]
        return (f"{n} site{'s' if n != 1 else ''} that listed {w['you']} didn't show it this scan; the next scan "
                f"checks again before calling it gone." + breach)
    if c["needs stealth"] and not stealth:
        return (f"We couldn't check {unchecked} site{'s' if unchecked != 1 else ''} yet — scan again with "
                f"'get past bot walls' on in Settings." + breach)
    s = lambda n: "s" if n != 1 else ""
    blocked, unclear = c["blocked"] + c["needs stealth"], c["unconfirmed"] + c["error"]
    parts = ([f"{c['not here']} site{s(c['not here'])} can't be checked in this version"] if c["not here"] else []) + \
            ([f"{blocked} site{s(blocked)} blocked the check; the next scan tries {'it' if blocked == 1 else 'them'} again"]
             if blocked else []) + \
            ([f"{unclear} site{s(unclear)} gave no clear answer; the next scan tries again"] if unclear else [])
    if not parts:
        return f"No site we checked lists {w['you']}." + breach
    lead = f"No site we checked lists {w['you']}. " if c["not found"] else ""
    return lead + ". ".join(x[0].upper() + x[1:] for x in parts) + "." + breach


def status_word(r, rstate, w):
    """(words, colour) — the only status words a person sees."""
    if rstate in ("requested", "confirmed"):
        return "Removal requested", "p-ok"
    if rstate == "gone":
        return "Removed", "p-ok"
    if rstate == "manual":
        return "Needs " + w["you"], "p-warn"
    if r["status"] == "listed":
        return "Listing " + w["you"], "p-bad"
    if r["status"] == "possible":
        return "Maybe " + w["you"], "p-warn"
    if r["status"] in UNCHECKED or r["status"] == "unconfirmed":
        return "Not checked yet", "p-quiet"
    return "Not listed", "p-ok"


def worth_telling(text):
    """What's new is for things that change what a person does: a listing found, back or gone, a new breach, a
    removal that needs them. Search engines turning a scan away is not news; it shows under Not checked yet."""
    return not re.match(r"web search: |people-search check failed|web page check failed|breach check for|archive check: |accounts check: ",
                        text)


def plain_alert(text):
    """Alerts in words a person uses: no engine names' http codes, no 'sweep'."""
    m = re.match(r"web search: (\w+) failed (\d+) of (\d+) searches", text)
    if m:
        return f"{m.group(1).title()} turned away {m.group(2)} of {m.group(3)} web searches, so some pages weren't checked."
    m = re.match(r"(people-search check|web page check) failed \((.+?)\)", text)
    if m:
        return f"Couldn't check {m.group(2)} this time; it's tried again on the next scan."
    if text.startswith("accounts check: "):
        return "Couldn't finish looking for accounts with your usernames; it's tried again on the next scan."
    if text.startswith("archive check: "):
        return "Couldn't finish looking for old profile copies in the Internet Archive; it's tried again on the next scan."
    return text


def browser_install():
    """Under the stealth switch in the packaged edition: the hidden browser is a download from Settings, checked
    against the fingerprint the package was built with. Empty where it isn't this edition's job."""
    p = browser.pin()
    if not p:
        return ""
    if browser.installed(p):
        return f"<div class=k>Hidden browser: Chromium {e(p['version'])}, checked against its fingerprint.</div>"
    st = browser.INSTALL
    mb = lambda n: round(n / 1_000_000)  # decimal, as Files and App Center count
    if st["state"] == "downloading":
        return (f"<div class=k><b>Downloading the hidden browser… {mb(st['got'])} of {mb(p['size'])} MB.</b> "
                "Reload this page to see how it's going.</div>")
    note = f"<div class=k>That didn't work: {e(st['note'])}</div>" if st["state"] == "failed" else ""
    return (f"{note}<div class=k>Needs its hidden browser first: a {mb(p['size'])} MB download (about 420 MB on disk), "
            "from Google's Chrome for Testing.</div><button formaction=/settings/download-browser formmethod=post "
            f"class=grey>{'Try again' if note else 'Download it'}</button>")


def network_block(db, port):
    """Settings' switch for the packaged edition: open the dashboard to other devices without editing a service."""
    if not NETWORK_SWITCH:
        return ""
    on = db.get("network") == "1"
    where = ""
    if on:
        name = socket.gethostname().lower() + ".local"
        ips = sorted(a for a in my_addresses() if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", a) and not a.startswith("127."))
        where = (f"<div class=k>Open it at {' or '.join(e(f'http://{a}:{port}') for a in [name] + ips)}. "
                 f"{e(name)} may not work on every phone; the IP address always does.</div>")
    changes = "".join(f"<li>{e(a['text'])} <span class=k>{when(a['ts'])}</span></li>" for a in
                      db.q("SELECT text, ts FROM alerts WHERE person IS NULL ORDER BY ts DESC LIMIT 3"))
    return (f"""<h2 style="margin-top:18px">Other devices</h2>
<label class=switch><input type=checkbox name=network value=1 {'checked' if on else ''}> Open this dashboard from your phone and other computers</label>
<div class=k>Anyone on the same network as this computer (at home, or wherever it's connected) can reach the sign-in
page. Your password and results cross that network unencrypted (http).</div>{where}"""
            + (f"<ul class=k>{changes}</ul>" if changes else ""))


def accounts_install():
    """Under the accounts switch when maigret isn't there yet: a button, not a command to type."""
    if accounts.installed():
        return ""
    if accounts.packaged():
        return f"<div class=k>{e(accounts.NOT_HERE)}</div>"
    st = accounts.INSTALL
    if st["state"] == "installing":
        return "<div class=k><b>Adding it…</b> this takes a few minutes. Reload this page to see when it's done.</div>"
    note = (f"<div class=k>That didn't work: {e(st['note'])}</div>" if st["state"] == "failed" else "")
    return (f"{note}<div class=k>Not added yet. It's a separate download (maigret 0.6.6 from the Python package index).</div>"
            "<button formaction=/settings/add-accounts formmethod=post class=grey>Add it</button>")


def email_line(q, name, pid, w):
    """Where the removal email to this site stands, on the site's card."""
    if q["state"] == "draft" and (q["note"] or "").startswith("Try "):
        return (f"<div>The removal email to {e(name)} bounced. One to their other address is waiting on "
                f"<a href=/person/{pid}/remove-all>Remove {w['you']} from all</a>.</div>")
    if q["state"] == "draft":
        what = "A follow-up email" if q["note"] == "follow-up" else "A removal email"
        return f"<div>{what} to {e(name)} is ready on <a href=/person/{pid}/remove-all>Remove {w['you']} from all</a>.</div>"
    if q["state"] == "approved":
        return f"<div>A removal email to {e(name)} goes out soon.</div>"
    if q["state"] == "confirmed":
        return f"<div>{e(name)} {inbox.WORDS['confirmed']}.</div>"
    if q["state"] == "failed":
        return f"<div>The removal email to {e(name)} couldn't be sent: {e(q['note'] or 'unknown reason')}.</div>"
    days = int((time.time() - (q["sent"] or time.time())) // 86400)
    if q["follow_up"] and q["follow_up"] <= time.time() and (q["reply"] or "") in ("", "acknowledged"):
        return (f"<div>No answer after {days} days — send a follow-up.</div><div class=actions><form method=post "
                f"action=/person/{pid}/followup class=inline><input type=hidden name=rid value={q['id']}>"
                "<button class=grey>Send a follow-up</button></form></div>")
    sent = f"Removal email sent {when(q['sent'])}."
    if q["reply"] and q["reply"] in inbox.WORDS:
        tail = "." if q["reply"] in ("acknowledged", "use_form") else ": the email is in the removal inbox."
        return f"<div>{sent} {e(name)} {inbox.WORDS[q['reply']]}{tail}</div>"
    left = int(((q["follow_up"] or time.time()) - time.time()) // 86400) + 1
    return f"<div>{sent} No answer yet; Guard Dog reminds {w['you']} in {left} day{'s' if left != 1 else ''} if none comes.</div>"


def removal_cell(db, pid, r, script, st, w):
    """What a person can do about a listing Guard Dog can remove itself."""
    state, updated = st
    fid = r["id"]
    site = BY_ID.get(r["source"], {}).get("name", script.site.title())

    def button(mode, label, grey=False):
        return (f"<form method=post action=/person/{pid}/remove class=inline><input type=hidden name=fid value={fid}>"
                f"<input type=hidden name=mode value={mode}><button{' class=grey' if grey else ''}>{label}</button></form>")
    if fid in RUNNING:
        return "<div class=k>Working on it… this takes about a minute. Reload the page to see how it went.</div>"
    info = last_run(db, fid)
    ev = Path(info.get("evidence") or "")
    shots = sorted(ev.glob("*.png")) if info.get("evidence") else []
    link = (f"<a href='/evidence/{fid}/{ev.name}/{e(shots[-1].name)}' target=_blank>See the filled-in form</a>"
            if shots else "")
    sends = "".join(f"{x} and " for x in script.reads) + "your removal email"
    if state == "ready" and removal.ready_is_fresh(db, fid):
        return (f"<div>Here's what we'll send {e(site)}: {e(sends)}. Nothing has been sent yet.</div>"
                f"<div class=actions>{button('submit', 'Send it')}{link}</div>")
    if state in ("requested", "confirmed"):
        return (f"<div>Removal sent {when(updated)}. Guard Dog checks it's gone on the next scan.</div>"
                f"<div class=actions>{link}</div>")
    if state == "manual":
        fill = json.loads(info.get("prefill") or "{}")
        steps = "".join(f"<li>{e(s)}</li>" for s in getattr(script, "by_hand", ()))
        boxes = "".join(f"<label>{e(k)}</label><input readonly value='{e(v)}' onclick='this.select()'>" for k, v in fill.items())
        again = ("" if fill else f"<div class=actions>{button('dry_run', 'Get the details again', grey=True)}</div>"
                 "<div class=k>Guard Dog looks up what to paste, then stops again.</div>")
        return (f"<div>This one needs {w['you']}: {e(info.get('note') or '')}.</div>"
                + (f"<ol>{steps}</ol>" if steps else "") + boxes + again
                + f"<div class=actions><a class=btn href='{e(BY_ID.get(r['source'], {}).get('optout', ''))}' target=_blank "
                f"rel=noreferrer>Open {e(site)}'s removal page</a>"
                f"<form method=post action=/person/{pid}/removal class=inline><input type=hidden name=fid value={fid}>"
                "<button class=grey>I've done it</button></form></div>")
    if state == "gone":
        return "<div>Removed. Guard Dog keeps checking it doesn't come back.</div>"
    if state == "failed":
        return (f"<div>That didn't work: {e(info.get('note') or 'the site changed something')}. Nothing was sent.</div>"
                f"<div class=actions>{button('dry_run', 'Try again')}{link}</div>")
    if state == "ready":  # older than a day: the site's page may have changed since
        return (f"<div>The check of {e(site)}'s form was more than a day ago, so Guard Dog looks again first.</div>"
                f"<div class=actions>{button('dry_run', 'Remove me' if w['you'] == 'you' else 'Remove')}</div>")
    return (f"<div class=actions>{button('dry_run', 'Remove me' if w['you'] == 'you' else 'Remove')}</div>"
            f"<div class=k>Guard Dog fills in {e(site)}'s removal form and stops, so you can check it before anything is sent.</div>")


def mark_asked_by_hand(db, fid):
    """The owner removed it by hand ("I've asked them"): a manual request, through the one removal state system."""
    st = removal.state(db, fid)
    if st in ("found", "back", "failed"):
        removal.move(db, fid, "manual", note="the owner is doing this one by hand")
        st = "manual"
    if st in ("ready", "manual"):
        removal.move(db, fid, "requested", note="the owner asked the site by hand")


def make_handler(db, local_only, allowed_hosts, setup_code, require_password=False):
    local_password = local_only and require_password  # the packaged edition: other accounts on this computer
    class H(http.server.BaseHTTPRequestHandler):
        server_version = "GuardDog/" + __version__

        def log_message(self, *a):
            pass

        # ---------------------------------------------------------------- plumbing
        def send(self, body, code=200, ctype="text/html; charset=utf-8", extra=None):
            b = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(b)))
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "same-origin")
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(b)

        def go(self, where, extra=None):
            self.send_response(303)
            self.send_header("Location", where)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def form(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > 1_000_000:
                # Too big to use, but read it off (up to 16 MB) before answering: left unread, closing the connection
                # resets it, and a slow sender sees a broken connection instead of the answer (arm64 build, 2026-10-30)
                left = min(n, 16_000_000)
                while left > 0:
                    got = self.rfile.read(min(65536, left))
                    if not got:
                        break
                    left -= len(got)
                self.close_connection = True
                return {}
            return {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(n).decode(), keep_blank_values=True).items()}

        def authed(self):
            if local_only and not require_password:
                return True
            if not db.get("password_hash"):
                return False
            for part in (self.headers.get("Cookie") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == "gd" and SESSIONS.get(v, 0) > time.time():
                    return True
            return False

        def host_ok(self):
            # A web page can point its own name at 127.0.0.1 (DNS rebinding); only names we serve are answered.
            return (self.headers.get("Host") or "").lower() in allowed_hosts

        def same_origin(self):
            host = (self.headers.get("Host") or "").lower()
            site = self.headers.get("Sec-Fetch-Site")
            if site is not None and site != "same-origin":
                return False
            for h in ("Origin", "Referer"):
                n = urllib.parse.urlsplit(self.headers.get(h) or "").netloc.lower()
                if n:
                    return n in allowed_hosts and n == host
            return site == "same-origin"  # browsers that hide both still say where the request came from

        # ---------------------------------------------------------------- routes
        def do_GET(self):
            if not self.host_ok():
                return self.send("unknown host name", 421, "text/plain")
            u = urllib.parse.urlsplit(self.path)
            q = urllib.parse.parse_qs(u.query)
            if not self.authed():
                return self.login_page()
            parts = [p for p in u.path.split("/") if p]
            if not parts:
                return self.home()
            if parts == ["settings"]:
                if q.get("switching") and self.server.rebind is True:
                    self.send(page("Settings", "<meta http-equiv=refresh content='2;url=/settings'><div class=card>Saved. "
                                   "Switching over: this page comes back in a moment.</div>"))
                    return self.server.switch_over()
                return self.settings_page()
            if parts == ["password"]:
                return self.password_page()
            if parts == ["inbox"]:
                return self.inbox_page()
            if parts == ["person", "new"]:
                return self.edit_page(None)
            if len(parts) >= 2 and parts[0] == "person" and parts[1].isdigit():
                pid = int(parts[1])
                if not db.person(pid):
                    return self.send(page("Not found", "<p>No such person.</p>"), 404)
                if len(parts) == 3 and parts[2] == "edit":
                    return self.edit_page(pid)
                if len(parts) == 3 and parts[2] == "status":
                    return self.status_json(pid)
                if len(parts) == 3 and parts[2] == "remove-all":
                    return self.remove_all_page(pid, q.get("problem", [""])[0])
                return self.report(pid, q.get("show", ["all"])[0])
            if len(parts) == 4 and parts[0] == "evidence":
                return self.evidence(*parts[1:])
            if len(parts) == 2 and parts[0] == "copy":
                return self.copy(parts[1])
            self.send(page("Not found", "<p>Not found.</p>"), 404)

        def do_POST(self):
            if not self.host_ok():
                return self.send("unknown host name", 421, "text/plain")
            if not self.same_origin():
                return self.send("cross-site request refused", 403, "text/plain")
            parts = [p for p in urllib.parse.urlsplit(self.path).path.split("/") if p]
            f = self.form()
            if parts == ["login"]:
                return self.login(f)
            if parts == ["logout"]:
                for part in (self.headers.get("Cookie") or "").split(";"):
                    k, _, v = part.strip().partition("=")
                    if k == "gd":
                        SESSIONS.pop(v, None)
                return self.go("/", {"Set-Cookie": "gd=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"})
            if not self.authed():
                return self.go("/")
            if parts == ["settings"]:
                return self.save_settings(f)
            if parts == ["settings", "download-browser"]:
                browser.install(proxy=db.get("proxy") or "")
                return self.go("/settings")
            if parts == ["settings", "add-accounts"]:
                if not accounts.installed() and not accounts.packaged():
                    accounts.install()
                return self.go("/settings")
            if parts == ["password"]:
                return self.check_password(f)
            if parts == ["inbox"] and f.get("mid", "").isdigit():
                db.x("UPDATE mail SET state='done' WHERE id=?", (int(f["mid"]),))
                return self.go("/inbox")
            if parts == ["person", "save"]:
                return self.save_person(f)
            if len(parts) == 3 and parts[0] == "person" and parts[1].isdigit():
                pid = int(parts[1])
                if not db.person(pid):
                    return self.send(page("Not found", "<p>No such person.</p>"), 404)
                if parts[2] == "scan":
                    scanner.start(db, pid)
                    return self.go(f"/person/{pid}")
                if parts[2] == "delete" and f.get("confirm") == "yes":
                    scanner.delete(db, pid)
                    return self.go("/")
                if parts[2] == "removal" and f.get("fid", "").isdigit():
                    fid = int(f["fid"])
                    if db.q("SELECT 1 FROM findings WHERE id=? AND person=?", (fid, pid)):
                        mark_asked_by_hand(db, fid)
                    return self.go(f"/person/{pid}?show=listed")
                if parts[2] == "remove" and f.get("fid", "").isdigit() and f.get("mode") in ("dry_run", "submit"):
                    fid = int(f["fid"])
                    row = db.q("SELECT * FROM findings WHERE id=? AND person=?", (fid, pid))
                    if row and row[0]["source"] in SCRIPTS:
                        start_removal(db, dict(row[0]), f["mode"])
                    return self.go(f"/person/{pid}?show=listed")
                if parts[2] == "remove-all":
                    requests.plan(db, pid, everyone=f.get("everyone") == "1")
                    return self.go(f"/person/{pid}/remove-all")
                if parts[2] == "send":
                    try:
                        requests.approve(db, pid)
                    except requests.NotReady as ex:
                        return self.go(f"/person/{pid}/remove-all?problem={urllib.parse.quote(str(ex))}")
                    start_sending(db, pid)
                    return self.go(f"/person/{pid}")
                if parts[2] == "followup" and f.get("rid", "").isdigit():
                    if db.q("SELECT 1 FROM requests WHERE id=? AND person=? AND state='sent'", (int(f["rid"]), pid)):
                        requests.follow_up(db, int(f["rid"]))
                    return self.go(f"/person/{pid}/remove-all")
                if parts[2] == "seen":
                    db.x("UPDATE alerts SET seen=1 WHERE person=?", (pid,))
                    return self.go(f"/person/{pid}")
            self.send(page("Not found", "<p>Not found.</p>"), 404)

        # ---------------------------------------------------------------- login
        def login_page(self, msg=""):
            first = not db.get("password_hash")
            window = setup_open(self.client_address[0])
            if local_password and first and not window:  # no code here: its file isn't readable from a desktop account
                body = ("<div class=card><h1>Set a password</h1><p class=k>The time to set the first password has run out "
                        "(it's open for 30 minutes after Guard Dog starts). Use <b>Reset Guard Dog password</b> in your apps "
                        "menu (it asks for this computer's admin password), or restart this computer and open this page "
                        "again.</p></div>")
                return self.send(page("Sign in", body, nav=False), 401)
            intro = ("<p class=k>Anyone who uses this computer could open this page, so pick a password.</p>" if local_password
                     else "<p class=k>Other devices on your network can reach this dashboard, so it needs a password. "
                          "Pick one now; you'll use it on every device.</p>")
            body = (f"<div class=card><h1>{'Set a password' if first else 'Sign in'}</h1>"
                    + (intro if first and window else
                       ("<p class=k>The time to set the first password without a code has run out (it's open for 30 minutes "
                        "after Guard Dog starts). Restart the machine Guard Dog runs on and open this page again, or enter "
                        "the setup code from <code>setup-code.txt</code> in its data folder.</p>") if first else "")
                    + (f"<p style='color:var(--bad)'>{e(msg)}</p>" if msg else "")
                    + "<form method=post action=/login>"
                    + ("<label>Setup code</label><input name=code autocomplete=off required>" if first and not window else "")
                    + "<label>Password</label><input type=password name=pw autocomplete=current-password minlength=8 required>"
                    + ("<label>Again</label><input type=password name=pw2 minlength=8 required>" if first else "")
                    + "<p><button>Continue</button></p></form>"
                    + ("" if local_only else
                       "<p class=k>This connection isn't encrypted. Use it only on your own home network.</p>") + "</div>")
            self.send(page("Sign in", body, nav=False), 401)

        def login(self, f):
            ip = self.client_address[0]
            pw = f.get("pw", "")
            with LOGIN_LOCK:
                n, until = FAILS.get(ip, (0, 0))
                if until > time.time():
                    return self.send(page("Wait", f"<p>Too many wrong tries. Try again in {int(until - time.time()) + 1} "
                                                  "seconds.</p>", nav=False), 429)
                if not db.get("password_hash"):
                    code_ok = bool(setup_code) and hmac.compare_digest(f.get("code", "").strip(), setup_code)
                    if not (code_ok or setup_open(ip)):
                        ok, msg = False, "That setup code isn't right."
                    elif len(pw) < 8 or pw != f.get("pw2"):
                        return self.login_page("Both passwords must match and be at least 8 characters.")
                    else:
                        db.set("password_hash", pw_hash(pw))
                        (db.dir / "setup-code.txt").unlink(missing_ok=True)
                        ok = True
                else:
                    ok, msg = pw_ok(pw, db.get("password_hash")), "Wrong password."
                if not ok:
                    n += 1
                    FAILS[ip] = (n, time.time() + (min(300, 2 ** (n - 4)) if n >= 5 else 0))
                    time.sleep(1)
                    return self.login_page(msg)
            FAILS.pop(ip, None)
            tok = secrets.token_urlsafe(32)
            SESSIONS[tok] = time.time() + 30 * 86400
            self.go("/", {"Set-Cookie": f"gd={tok}; HttpOnly; SameSite=Strict; Path=/; Max-Age={30 * 86400}"})

        # ---------------------------------------------------------------- pages
        def home(self):
            people = db.people()
            cards = []
            for p in people:
                w = who(p)
                c = tally(db.q("SELECT source, status, check_note FROM findings WHERE person=? AND status != 'superseded'",
                               (p["id"],)))
                nb = db.q("SELECT COUNT(*) n FROM breaches WHERE person=?", (p["id"],))[0]["n"]
                name = p["label"] or (p["names"][0] if p["names"] else "Unnamed")
                cards.append(f"""<div class=card><h2>{e(name)}</h2>
<div class=lead>{e(summary(p, c, nb, scanner.running(p['id']), w, [r['source'] for r in db.q("SELECT source FROM findings WHERE person=? AND status='listed'", (p['id'],))], db.get("stealth") == "1"))}</div>
<div class=k>{e('Checking now…' if scanner.running(p['id']) else 'Last checked ' + when(p['last_scan']))}</div>
<div class=actions><a class=btn href="/person/{p['id']}">Open</a></div></div>""")
            intro = ("" if people else "<div class=card><p>Add the first person to look after: you, or someone who asked you to. "
                     "Guard Dog finds the sites that sell their address, phone and relatives, checks if their email was in a "
                     "data breach, and removes them.</p></div>")
            waiting = len(inbox.queue(db))
            mailcard = (f"<div class=card><b>{waiting} email{'s' if waiting != 1 else ''} in the removal inbox need"
                        f"{'s' if waiting == 1 else ''} you.</b> <a href=/inbox>See them</a></div>" if waiting else "")
            self.send(page("People", f"<h1>People</h1>{mailcard}{intro}{''.join(cards)}<p><a class='btn grey' href=/person/new>Add a person</a></p>"))

        def edit_page(self, pid):
            p = db.person(pid) if pid else {"label": "", "names": [], "emails": [], "phones": [], "places": [],
                                             "usernames": [], "relatives": [], "birth_year": None, "consent": "",
                                             "home_state": "", "home_country": "US"}
            def ta(name, label, hint):
                return (f"<label>{label}</label><div class=k>{hint}</div>"
                        f"<textarea name={name}>{e(chr(10).join(p[name]))}</textarea>")
            body = f"""<h1>{'Edit' if pid else 'Add a person'}</h1><form method=post action=/person/save class=card>
<input type=hidden name=id value="{pid or ''}">
<label>Label</label><div class=k>What you call them here, e.g. "Me" or "Mom".</div><input name=label value="{e(p['label'] or '')}">
{ta('names', 'Names', 'One per line, first and last: every name they have used, e.g. a maiden name.')}
{ta('emails', 'Email addresses', 'One per line. Each one is sent to the breach service; never to people-search sites.')}
{ta('phones', 'Phone numbers', 'One per line, any format.')}
{ta('places', 'Places', 'One per line as City, ST (e.g. Columbus, OH). Every city you have lived in, current and past: '
    'sites list past cities too.')}
{ta('usernames', 'Usernames', 'Optional. One per line.')}
{ta('relatives', 'Relatives', 'Optional, one per line: parents, spouse, siblings, grown children. People-search sites list them, '
    'so they help tell your listing apart from someone with the same name. They are never typed into any site.')}
<label>Birth year</label><div class=k>Optional. Sites show an age; this tells your listing apart from a namesake's.</div>
<input name=birth_year type=number min=1900 max=2030 value="{e(str(p.get('birth_year') or ''))}">
<label>Where they live now</label><div class=k>State (two letters, e.g. OH) and country. This decides which law a removal
email can call on: only someone living in California can use California's.</div>
<div class=row><input name=home_state maxlength=2 placeholder="State" value="{e(p.get('home_state') or '')}">
<input name=home_country maxlength=2 placeholder="Country" value="{e(p.get('home_country') or 'US')}"></div>
<label>Who agreed</label><div class=k>If this is someone else, note that they asked you to do this and when.</div>
<input name=consent value="{e(p['consent'] or '')}">
<p><button>Save</button> <a class="btn grey" href="{'/person/' + str(pid) if pid else '/'}">Cancel</a></p></form>"""
            if pid:
                body += (f"<form method=post action=/person/{pid}/delete class=card><h2>Remove this person</h2>"
                         "<p class=k>Deletes everything Guard Dog keeps about them on this machine.</p>"
                         "<label class=switch><input type=checkbox name=confirm value=yes required> Yes, delete it all</label>"
                         "<p><button class=red>Delete</button></p></form>")
            self.send(page("Edit", body))

        def save_person(self, f):
            pid = int(f["id"]) if f.get("id", "").isdigit() else None
            names = lines(f.get("names"))
            if not names:
                return self.send(page("Edit", "<p>At least one name is needed. <a href='javascript:history.back()'>Back</a></p>"), 400)
            by = f.get("birth_year", "").strip()
            by = int(by) if by.isdigit() and 1900 <= int(by) <= 2030 else None
            pid = db.save_person(pid, f.get("label", "").strip(), f.get("consent", "").strip(), birth_year=by, names=names,
                                 emails=lines(f.get("emails")), phones=lines(f.get("phones")),
                                 places=lines(f.get("places")), usernames=lines(f.get("usernames")),
                                 relatives=lines(f.get("relatives")),
                                 home_state=re.sub(r"[^A-Za-z]", "", f.get("home_state", ""))[:2].upper(),
                                 home_country=(re.sub(r"[^A-Za-z]", "", f.get("home_country", "")) or "US")[:2].upper())
            self.go(f"/person/{pid}")

        def report(self, pid, show):
            p = db.person(pid)
            wd = who(p)
            rows = db.q("SELECT * FROM findings WHERE person=? AND status != 'superseded' ORDER BY source, url", (pid,))
            c = tally(rows)
            br = db.q("SELECT * FROM breaches WHERE person=? ORDER BY date DESC", (pid,))
            asked = {q["broker"]: dict(q) for q in db.q("SELECT * FROM requests WHERE person=?", (pid,))}
            alerts = [a for a in db.q("SELECT * FROM alerts WHERE person=? AND seen=0 ORDER BY ts DESC LIMIT 30", (pid,))
                      if worth_telling(a["text"])][:5]
            running = scanner.running(pid)
            last = db.q("SELECT * FROM scans WHERE person=? ORDER BY id DESC LIMIT 1", (pid,))
            rs = {x["finding"]: (x["state"], x["updated"]) for x in
                  db.q("SELECT finding, state, updated FROM removals WHERE finding IN (SELECT id FROM findings WHERE person=?)",
                       (pid,))}
            name = p["label"] or p["names"][0]
            hours = float(db.get("rescan_hours") or 0)

            listed_sources = [r["source"] for r in rows if r["status"] == "listed"]
            body = (f"<h1>{e(name)}</h1><div class=card><div class=lead>"
                    f"{e(summary(p, c, len(br), running, wd, listed_sources, db.get('stealth') == '1'))}</div>")
            if running:
                prog = last[0]["progress"] if last else ""
                body += (f"<div class=k>Checking: <span id=prog>{e(prog)}</span></div>"
                         f"<script>setInterval(()=>fetch('/person/{pid}/status').then(r=>r.json()).then(j=>{{"
                         f"if(!j.running||j.found!={c['listed'] + c['possible']})location.reload();"
                         f"else document.getElementById('prog').textContent=j.progress}}),4000)</script>")
            else:
                nxt = (f" Next check {when(p['last_scan'] + hours * 3600)}." if p["last_scan"] and hours > 0 else "")
                body += (f"<div class=k>Last checked {when(p['last_scan'])}.{nxt}</div>"
                         f"<div class=actions><form method=post action=/person/{pid}/scan class=inline><button>Scan now</button>"
                         f"</form><a class='btn grey' href=/person/{pid}/remove-all>Remove {wd['you']} from all</a>"
                         f"<a class='btn grey' href=/person/{pid}/edit>Edit details</a></div>")
            emails = Counter(r["state"] for r in db.q("SELECT state FROM requests WHERE person=?", (pid,)))
            if emails["sent"] or emails["approved"] or emails["confirmed"] or pid in SENDING:
                body += (f"<div class=k>Removal emails: {emails['sent'] + emails['confirmed']} sent"
                         + (f", {emails['confirmed']} confirmed done" if emails["confirmed"] else "")
                         + (f", {emails['approved']} going out over the next days" if emails["approved"] else "")
                         + (" (sending now…)" if pid in SENDING else "")
                         + (f". <a href=/person/{pid}/remove-all>{len(requests.due(db, pid))} need a follow-up</a>"
                            if requests.due(db, pid) else "") + ".</div>")
            body += "</div>"

            if alerts:
                items = "".join(f"<li>{e(plain_alert(a['text']))} <span class=k>{when(a['ts'])}</span></li>" for a in alerts)
                body += (f"<div class=card><h2>What's new</h2><ul>{items}</ul>"
                         f"<form method=post action=/person/{pid}/seen><button class=grey>Got it</button></form></div>")

            if len(p["places"]) <= 1 and not p.get("relatives") and not p.get("birth_year"):
                lived = "you've" if wd["you"] == "you" else f"{wd['you']} has"
                body += (f"<div class=card>Add every city {lived} lived in, relatives and birth year in "
                         f"<a href=/person/{pid}/edit>Edit details</a>. People-search sites show those, so they tell "
                         f"{wd['your']} listing apart from someone with the same name.</div>")

            def card(r):
                b = source_info(r)
                rst = rs.get(r["id"], ("found", 0))
                word, colour = status_word(r, rst[0], wd)
                what = shows(r["matched"])
                act = ""
                sister = likely.get(r["id"])
                note = ""
                if sister:
                    word, colour = "Likely listing " + wd["you"], "p-bad"
                    note = f"<div>{e(sister_note(r['source'], BY_ID.get(sister, {}).get('name', sister), wd['you']))}</div>"
                if r["status"] == "not found" and r["check_note"] == scanner.UNSEEN:
                    note += (f"<div>Not seen on the last scan. Guard Dog counts it as gone when the next scan agrees; "
                             f"until then, don't count on it.</div>")
                main = covered_by(r["source"])
                if main and (r["status"] in ("listed", "possible") or sister):
                    act = (f"<div>Removing {wd['you']} from {e(BY_ID.get(main, {}).get('name', main))} also removes "
                            f"{wd['you']} here: its own removal page sends you there.</div>")
                elif r["status"] in ("listed", "possible") or sister or rst[0] not in ("found",):
                    if r["source"] in SCRIPTS:
                        act = removal_cell(db, pid, r, SCRIPTS[r["source"]], rst, wd)
                    elif rst[0] in ("requested", "confirmed"):
                        act = f"<div>Removal requested {when(rst[1])}. Guard Dog checks it's gone on the next scan.</div>"
                    elif rst[0] == "gone":
                        act = "<div>Removed. Guard Dog keeps checking it doesn't come back.</div>"
                    else:
                        act = (f"<details><summary>How to remove</summary><div>{e(b['how'])}</div>"
                               f"<div class=actions><a class=btn href='{e(b['optout'])}' target=_blank rel=noreferrer>"
                               f"Open {'the' if r['kind'] == 'web' else e(b['name']) + chr(39) + 's'} removal page</a>"
                               f"<form method=post action=/person/{pid}/removal class=inline><input type=hidden name=fid "
                               f"value={r['id']}><button class=grey>I've done it</button></form></div></details>")
                copy = f" · <a href='/copy/{e(r['copy'])}'>the page as Guard Dog saw it</a>" if r["copy"] else ""
                details = (f"<details><summary>Details</summary><div class=k><a href='{e(r['url'])}' target=_blank "
                           f"rel=noreferrer>the search</a>{copy}<br>what matched: {e(r['matched'] or '—')}<br>"
                           f"result: {e(r['status'])}"
                           + (f"; last try {when(r['last_checked'])}: {e(r['check_note'])}" if r["check_note"] else "")
                           + "</div></details>")
                q = asked.get(r["source"])
                act = note + act + (email_line(q, BY_EMAIL_NAME.get(q["broker"], b["name"]), pid, wd) if q else "")
                return (f"<div class=site><h3>{e(b['name'])}</h3><span class='pill {colour}'>{e(word)}</span>"
                        + (f"<div>Shows {wd['your']} {e(what)}.</div>" if what and r["status"] in ("listed", "possible") else "")
                        + act + details + "</div>")

            def section(title, rs_, open_=True, note=""):
                if not rs_:
                    return ""
                inner = note + "".join(card(r) for r in rs_)
                if open_:
                    return f"<div class=card><h2>{title}</h2>{inner}</div>"
                return f"<div class=card><details><summary><b>{title}</b></summary>{inner}</details></div>"

            live = lambda r: rs.get(r["id"], ("found",))[0]
            listed_families = {family_of(s)["id"]: s for s in listed_sources if family_of(s)}
            likely = {r["id"]: listed_families[family_of(r["source"])["id"]] for r in rows
                      if (r["status"] in UNCHECKED or r["status"] == "unconfirmed") and family_of(r["source"])
                      and family_of(r["source"])["id"] in listed_families}
            done = [r for r in rows if live(r) in ("requested", "confirmed", "gone", "manual")]
            listed = [r for r in rows if (r["status"] == "listed" or r["id"] in likely) and r not in done]
            maybe = [r for r in rows if r["status"] == "possible" and r not in done]
            unchecked = [r for r in rows if (r["status"] in UNCHECKED or r["status"] == "unconfirmed") and r not in done
                         and r["id"] not in likely]
            unseen = [r for r in rows if r["status"] == "not found" and r["check_note"] == scanner.UNSEEN and r not in done]
            clear = [r for r in rows if r["status"] == "not found" and r not in done and r not in unseen]

            body += section(f"Sites listing {wd['you']}", listed)
            body += section("Removals in progress", done)
            body += section(f"Might be {wd['you']} — check these", maybe, open_=not listed,
                            note="<div class=k>These pages show the name but not enough to be sure it's "
                                 f"{wd['you']}. Open Details to look.</div>")
            body += section("Not seen this time", unseen)
            if br:
                leaked = [b for b in br if breaches.password_leaked(dict(b))]
                items = "".join(
                    f"<div class=site><h3>{e(b['breach'])}</h3><div class=k>{e(b['date'] or '')} · {e(b['email'])}</div>"
                    + (f"<div>A password leaked here: change it at {e(b['domain'] or b['breach'])}, and anywhere "
                       f"{wd['you']} used the same one.</div>" if breaches.password_leaked(dict(b)) else "")
                    + f"<details><summary>Details</summary><div class=k>What leaked: {e(b['exposed'] or '—')}</div></details></div>"
                    for b in br)
                body += (f"<div class=card><h2>{wd['Your']} email was in {len(br)} data breach{'es' if len(br) != 1 else ''}</h2>"
                         + (f"<div>{len(leaked)} of them leaked a password.</div>" if leaked else "") + items + "</div>")
            ghosts = db.q("SELECT * FROM ghosts WHERE person=? ORDER BY platform, username", (pid,))
            if ghosts:
                year = lambda d: (d or "")[:4]
                items = "".join(
                    f"<div class=site><h3>{e(g['platform'])}: {e(g['username'])}</h3><div class=k>{e(g['url'])}</div>"
                    f"<div>{g['snapshots']} cop{'ies' if g['snapshots'] != 1 else 'y'}, "
                    + (f"{year(g['first'])} to {year(g['last'])}" if year(g['first']) != year(g['last']) else year(g['first']))
                    + f"</div><div class=actions><a class='btn grey' href='{e(g['copy'])}' target=_blank rel=noreferrer>"
                    "See the newest copy</a></div></div>" for g in ghosts)
                body += (f"<div class=card><details><summary><b>Old profile copies in the Internet Archive ({len(ghosts)})"
                         f"</b></summary><div class=k>Profiles with {e(wd['your'])} usernames that the Internet Archive kept a "
                         f"copy of, even if they're gone now. Someone else may have used the same name. If one was "
                         f"{'yours' if wd['you'] == 'you' else e(wd['your'])}, the Internet Archive removes copies on request: "
                         f"email {archive.REMOVE_TO} with the address, the years to remove, and when the account was "
                         f"{'yours' if wd['you'] == 'you' else 'theirs'}.</div>{items}</details></div>")
            accts = db.q("SELECT * FROM accounts WHERE person=? ORDER BY username, site", (pid,))
            if accts:
                items = "".join(
                    f"<div class=site><h3>{e(a['site'])}: {e(a['username'])}</h3>"
                    + (f"<div class=k>{e(a['tags'])}</div>" if a["tags"] else "")
                    + f"<div class=actions><a class='btn grey' href='{e(a['url'])}' target=_blank rel=noreferrer>Open it</a>"
                    "</div></div>" for a in accts)
                body += (f"<div class=card><details><summary><b>Accounts with {e(wd['your'])} usernames ({len(accts)})</b>"
                         f"</summary><div class=k>Sites where an account has one of {e(wd['your'])} usernames. Someone else "
                         "may use the same name. An old account you no longer use is worth closing on that site: it's "
                         "one more place a password can leak.</div>" + items + "</details></div>")
            stealth_off = db.get("stealth") != "1"
            turned_away = [a["text"] for a in db.q("SELECT text FROM alerts WHERE person=? AND seen=0 ORDER BY ts DESC LIMIT 30",
                                                     (pid,)) if not worth_telling(a["text"])]
            body += section(f"Not checked yet ({len(unchecked)})", unchecked, open_=False,
                            note=("<div class=k>Some sites turn automated visitors away. Turn on 'get past bot walls' in "
                                  "<a href=/settings>Settings</a> and scan again to check them.</div>" if stealth_off else
                                  "<div class=k>These sites didn't give a clear answer this time. Guard Dog tries again "
                                  "on the next scan.</div>")
                            + (f"<details><summary>Details</summary><div class=k>{'<br>'.join(e(plain_alert(t)) for t in turned_away)}"
                               "</div></details>" if turned_away else ""))
            body += section(f"Not listed ({len(clear)})", clear, open_=False)
            if not rows and not running:
                body += "<div class=card>No scan yet.</div>"
            self.send(page(name, body))

        def inbox_page(self):
            mails = inbox.queue(db)
            box = requests.inbox(db).get("address", "")
            items = "".join(
                f"<div class=site><h3>{e(BY_EMAIL_NAME.get(m['broker'], m['broker']) if m['broker'] else m['from_domain'])}"
                f"</h3><div>{e(m['subject'] or '(no subject)')}</div><div class=k>{when(m['date'])} · "
                f"{e(inbox.WORDS.get(m['cls'], 'answered — check it'))}</div>"
                f"<div class=actions><a class='btn grey' href='https://mail.google.com/' target=_blank rel=noreferrer>"
                f"Open the removal inbox</a><form method=post action=/inbox class=inline><input type=hidden name=mid "
                f"value={m['id']}><button class=grey>Done</button></form></div></div>" for m in mails)
            ignored = db.q("SELECT from_domain, COUNT(*) n FROM mail WHERE state='ignored' GROUP BY from_domain ORDER BY n DESC")
            total = sum(r["n"] for r in ignored)
            body = ("<h1>Emails that need you</h1><div class=card>"
                    + (f"<div class=k>Answers in {e(box)} that Guard Dog couldn't settle by itself.</div>{items}" if mails
                       else "Nothing waiting. Guard Dog reads the removal inbox every 15 minutes while it waits for answers.")
                    + (f"<details><summary>Details</summary><div class=k>{total} other email{'s' if total != 1 else ''} "
                       f"ignored (not from a broker): " + ", ".join(f"{e(r['from_domain'])} ({r['n']})" for r in ignored)
                       + "</div></details>" if ignored else "")
                    + "</div>")
            self.send(page("Emails that need you", body))

        def remove_all_page(self, pid, problem=""):
            p = db.person(pid)
            wd = who(p)
            waiting = requests.drafts(db, pid)
            sent = db.q("SELECT COUNT(*) n FROM requests WHERE person=? AND state IN ('sent','approved')", (pid,))[0]["n"]
            body = f"<h1>Remove {e(wd['you'])} from all of them</h1>"
            try:
                requests.check_ready(db, p)
            except requests.NotReady as ex:
                problem = problem or str(ex)
            if problem:
                body += f"<div class=card><b>{e(problem)}</b> <a href=/settings>Settings</a> · <a href=/person/{pid}/edit>Edit details</a></div>"
            if not waiting:
                body += (f"<div class=card><div>No emails waiting. {sent} removal email{'s' if sent != 1 else ''} already "
                         f"sent or on the way.</div><div class=actions><form method=post action=/person/{pid}/remove-all "
                         f"class=inline><button>Prepare the emails</button></form></div><div class=k>Guard Dog writes one "
                         "to every data broker that lists " + e(wd['you']) + " and to every people-search site, and shows "
                         "them to you before anything is sent.</div></div>")
            else:
                first = next((r for r in waiting if r["finding"]), waiting[0])
                names = ", ".join(e(BY_EMAIL_NAME.get(r["broker"], r["broker"])) for r in waiting)
                body += (f"<div class=card><div class=lead>Guard Dog will email {len(waiting)} compan"
                         f"{'ies' if len(waiting) != 1 else 'y'} asking them to delete {e(wd['your'])} information and stop "
                         f"selling it.</div><details><summary>Who gets one</summary><div class=k>{names}</div></details>"
                         f"<h2 style='margin-top:12px'>Here's the email to {e(BY_EMAIL_NAME.get(first['broker'], first['broker']))}"
                         f"</h2><pre style='white-space:pre-wrap;font:inherit'>{e(first['subject'])}\n\n{e(first['body'])}</pre>"
                         f"<div class=k>Each one is the same, with that company's name, and {e(wd['your'])} listing on that "
                         "site if the scan found one. Nothing else about you is in it.</div>"
                         f"<div class=actions><form method=post action=/person/{pid}/send class=inline>"
                         f"<button{' disabled' if problem else ''}>Send {len(waiting)} emails</button></form></div>"
                         f"<div class=k>Up to {requests.DAILY_CAP} go out a day; any more go the next day.</div></div>")
            asked = {r["broker"] for r in db.q("SELECT broker FROM requests WHERE person=?", (pid,))}
            default = {b["id"] for b, _ in requests.targets(db, pid)}
            rest = len([b for b in requests.brokers() if b.get("request_email") and b["id"] not in asked | default])
            if rest > 0:
                body += (f"<div class=card><h2>Also the other {rest} data brokers</h2><div>Most data brokers sell "
                         "marketing data and don't show up in a search, so the scan can't tell if they have "
                         f"{e(wd['you'])}. Writing to all of them reaches the ones that do, but also tells the ones that "
                         f"don't that {e(wd['your'])} name and this email address exist.</div>"
                         f"<div class=actions><form method=post action=/person/{pid}/remove-all class=inline>"
                         f"<input type=hidden name=everyone value=1><button class=grey>Add them too</button></form></div></div>")
            due = requests.due(db, pid)
            if due:
                items = "".join(
                    f"<div class=site><h3>{e(BY_EMAIL_NAME.get(r['broker'], r['broker']))}</h3><div>No answer after "
                    f"{int((time.time() - r['sent']) // 86400)} days.</div><div class=actions><form method=post "
                    f"action=/person/{pid}/followup class=inline><input type=hidden name=rid value={r['id']}>"
                    "<button class=grey>Send a follow-up</button></form></div></div>" for r in due)
                body += f"<div class=card><h2>Waiting too long</h2>{items}</div>"
            body += f"<p><a href=/person/{pid}>Back</a></p>"
            self.send(page("Remove from all", body))

        def status_json(self, pid):
            last = db.q("SELECT progress FROM scans WHERE person=? ORDER BY id DESC LIMIT 1", (pid,))
            found = db.q("SELECT COUNT(*) n FROM findings WHERE person=? AND status IN ('listed','possible')", (pid,))[0]["n"]
            self.send(json.dumps({"running": scanner.running(pid), "progress": last[0]["progress"] if last else "",
                                  "found": found}),
                      ctype="application/json")

        def evidence(self, fid, run, name):
            if not (fid.isdigit() and re.fullmatch(r"\d{8}-\d{6}-(dry_run|submit)", run)
                    and re.fullmatch(r"[\w.-]+\.(png|log|json)", name)):
                return self.send("no", 400, "text/plain")
            f = db.dir / "removals" / fid / run / name
            if not f.is_file():
                return self.send("gone", 404, "text/plain")
            ctype = {"png": "image/png", "log": "text/plain; charset=utf-8", "json": "text/plain; charset=utf-8"}[name.rsplit(".", 1)[1]]
            self.send(f.read_bytes(), ctype=ctype, extra={"Content-Security-Policy": "sandbox; default-src 'none'"})

        def copy(self, name):
            if "/" in name or "\\" in name or ".." in name:
                return self.send("no", 400, "text/plain")
            f = db.dir / "copies" / name
            if not f.is_file():
                return self.send("gone", 404, "text/plain")
            # A saved page is someone else's HTML: show it with scripts, forms and network off.
            self.send(f.read_bytes(), extra={"Content-Security-Policy": "sandbox; default-src 'none'; img-src data:; style-src 'unsafe-inline'"})

        def settings_page(self, msg=""):
            g = db.get
            body = f"""<h1>Settings</h1>{f'<div class=card>{e(msg)}</div>' if msg else ''}<form method=post action=/settings class=card>
<h2>Stealth mode</h2>
<label class=switch><input type=checkbox name=stealth value=1 {'checked' if g('stealth') == '1' else ''}{' disabled' if browser.pin() and not browser.installed() else ''}> Get past bot walls (uses a hidden browser; some sites' terms forbid it)</label>{browser_install()}
<div class=k>Off: plain requests that look like Chrome; sites that block bots are skipped and shown as "Not checked yet".
On: a real hidden browser that looks like a person and gets through Cloudflare checks, so those sites get checked too.
Some sites' terms forbid automated searches; turning this on is your call.</div>
<label>Proxy (optional)</label><div class=k>e.g. http://user:pass@host:port. Empty = searches go out over your own connection.</div>
<input name=proxy value="{e(g('proxy'))}">
{network_block(db, self.server.server_address[1])}<h2 style="margin-top:18px">Web sweep</h2>
<label class=switch><input type=checkbox name=sweep value=1 {'checked' if g('sweep') != '0' else ''}> Search the whole web too</label>
<div class=k>Asks search engines (Brave, Bing, Mojeek; with stealth on also DuckDuckGo, Google, Startpage) for every name,
email, phone and username, then opens every page they return and checks it. Your emails and phone numbers go to those
search engines as quoted searches.</div>
<label class=switch><input type=checkbox name=archive value=1 {'checked' if g('archive') != '0' else ''}> Look for old profile copies in the Internet Archive</label>
<div class=k>Once a week, asks the Internet Archive whether it kept copies of profiles with each username (or the part
of each email before the @) on 15 sites. The usernames go to web.archive.org.</div>
<label class=switch><input type=checkbox name=accounts value=1 {'checked' if g('accounts') == '1' else ''}{'' if accounts.installed() else ' disabled'}> Look for accounts with each username</label>
{'' if accounts.packaged() else f"""<div class=k>Once a week, checks {accounts.SITES} popular sites for an account with each username (or the part of each
email before the @), using maigret. Each username goes to each of those sites.</div>"""}{accounts_install()}
<label>Most pages to check per scan</label><input name=sweep_limit type=number min=0 max=2000 value="{e(g('sweep_limit'))}">
<label>Sites checked at the same time</label><div class=k>Each site still gets one request at a time with the
wait below between them; this is how many different sites run side by side.</div>
<input name=workers type=number min=1 max=16 value="{e(g('workers'))}">
<label>Hidden browsers at the same time (stealth)</label><div class=k>1 to 4, plus one more that's only used on Cloudflare
checks. Each takes 1.5 to 3.5 GB of memory while it loads a page, so Guard Dog uses fewer when the machine is short
of memory (about one per 2 GB free).</div>
<input name=stealth_workers type=number min=1 max=4 value="{e(g('stealth_workers'))}">
<h2 style="margin-top:18px">Schedule</h2>
<label>Watch: re-scan every (hours)</label><div class=k>Guard Dog keeps watch around the clock and re-scans each person
this often (1 to 720). 0 = only when you press Scan.</div>
<input name=rescan_hours type=number min=0 max=720 step=1 value="{e(g('rescan_hours'))}">
<label>Wait between requests (seconds, min and max)</label>
<div class=row><input name=delay_min type=number min=0 step=0.5 value="{e(g('delay_min'))}"><input name=delay_max type=number min=0 step=0.5 value="{e(g('delay_max'))}"></div>
<h2 style="margin-top:18px">Removals</h2>
<label>Removal email</label><div class=k>The address you give opt-out forms and send removal emails from. Use a
separate address, only for this: sites learn it's real, and Guard Dog sends from it.</div>
<input name=removal_email value="{e(g('removal_email'))}">
<label>App password for that address</label><div class=k>For Gmail: turn on 2-step verification, then make an app
password at myaccount.google.com/apppasswords. Guard Dog uses it only to send removal emails, and keeps it in a file
only you can read. {'A password is saved; leave this empty to keep it.' if requests.inbox(db).get('password') else ''}</div>
<input name=app_password type=password autocomplete=off value="">
<label class=switch><input type=checkbox name=removal_only value=1> This address is used only for removals</label>
<h2 style="margin-top:18px">Breach checks</h2>
<label>Have I Been Pwned key (optional)</label><div class=k>Without one, Guard Dog uses XposedOrNot (free). With your own
paid key it uses Have I Been Pwned instead.</div><input name=hibp_key type=password value="{e(g('hibp_key'))}">
<p><button>Save</button></p></form>"""
            self.send(page("Settings", body))

        def save_settings(self, f):
            ready = not browser.pin() or browser.installed()  # packaged edition: no stealth before its browser is there
            db.set("stealth", "1" if f.get("stealth") == "1" and ready else "0")
            for k in ("proxy", "removal_email", "hibp_key"):
                db.set(k, f.get(k, "").strip())
            if f.get("app_password", "").strip():
                if f.get("removal_only") != "1":
                    return self.settings_page("Not saved: tick 'This address is used only for removals' to save an app "
                                              "password. Guard Dog won't send from an inbox you use for anything else.")
                requests.save_inbox(db, f.get("removal_email", ""), f["app_password"].strip().replace(" ", ""))
            elif requests.inbox(db):
                requests.save_inbox(db, f.get("removal_email", ""), "")  # the address changed: keep the password
            db.set("sweep", "1" if f.get("sweep") == "1" else "0")
            db.set("archive", "1" if f.get("archive") == "1" else "0")
            db.set("accounts", "1" if f.get("accounts") == "1" and accounts.installed() else "0")
            for k, top in (("rescan_hours", 720), ("delay_min", 120), ("delay_max", 120), ("sweep_limit", 2000), ("workers", 16), ("stealth_workers", 4)):
                v = bounded(f.get(k), top)
                if v is not None and k == "rescan_hours" and 0 < v < 1:
                    v = 1.0  # the watch never runs more often than hourly
                if v is not None:
                    db.set(k, str(int(max(v, 1 if k in ("workers", "stealth_workers") else 0)))
                           if k in ("sweep_limit", "workers", "stealth_workers") else str(v))
            want = "1" if f.get("network") == "1" else "0"
            if NETWORK_SWITCH and want != db.get("network"):
                db.set("network", want)
                db.alert(None, "Dashboard opened to the network" if want == "1" else "Dashboard closed to the network again")
                # serve() builds the new server once this one stops. A redirect, so no POST is left in the history to
                # resend; this server stops once that page has gone out, or after 3 s if nobody comes for it
                self.server.rebind = True
                t = threading.Timer(3, self.server.switch_over)
                t.daemon = True
                t.start()
                return self.go("/settings?switching=1")
            self.settings_page("Saved.")

        def password_page(self, msg=""):
            body = f"""<h1>Check a password</h1><form method=post action=/password class=card>
<p class=k>Has this password shown up in a breach? Only the first 5 characters of its SHA-1 fingerprint leave this
machine; the password itself never does, and Guard Dog doesn't keep it.</p>
<input type=password name=pw autocomplete=off required><p><button>Check</button></p>{msg}</form>"""
            self.send(page("Check a password", body))

        def check_password(self, f):
            try:
                n = breaches.password_seen(f.get("pw", ""))
                msg = (f"<p style='color:var(--bad)'><b>Seen {n:,} times in breaches.</b> Stop using it anywhere.</p>" if n
                       else "<p style='color:var(--accent)'><b>Not found in any known breach.</b></p>")
            except Exception as ex:
                msg = f"<p>Couldn't check: {e(str(ex))}</p>"
            self.password_page(msg)

    return H


SWITCH_LOCK = threading.Lock()


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    rebind = False  # set by the network switch just before it stops this server
    switched = False

    def switch_over(self):
        """Stop serving, once, so serve() builds the server on the new address."""
        with SWITCH_LOCK:
            if self.switched:
                return
            self.switched = True
        threading.Thread(target=self.shutdown, daemon=True).start()


def my_addresses():
    """This machine's names and addresses, for the Host allowlist in network mode."""
    names = {socket.gethostname().lower(), socket.gethostname().lower() + ".local"}
    try:
        for fam, *_, sa in socket.getaddrinfo(socket.gethostname(), None):
            names.add(sa[0])
    except OSError:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 9))  # no packet is sent; this just picks the outgoing address
        names.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return names


def serve(db, host, port, extra_hosts=(), ready=None, require_password=False, switch=False):
    """With `switch` (the packaged edition), Settings' network switch picks the address, and a toggle stops the server
    so it's built again here on the new one. Scans run in their own thread and carry on through it."""
    while True:
        # Never open with no password: after a reset, the first one is set on this computer, not by whoever is nearby
        open_ = switch and db.get("network") == "1" and bool(db.get("password_hash"))
        try:
            httpd, code, local_only = build(db, EVERYWHERE if open_ else host, port, extra_hosts,
                                            require_password=require_password, switch=switch)
        except OSError as ex:
            if not open_:
                raise  # __main__ turns it into one line; there's no other address to try
            db.set("network", "0")
            db.alert(None, f"Couldn't open the dashboard to the network ({ex.strerror or ex}); it's on this computer only")
            continue
        if ready:
            ready()
            ready = None
        print(f"Guard Dog {__version__}: open http://{'127.0.0.1' if local_only or open_ else host}:{port}  (data in {db.dir})",
              flush=True)
        if code:
            print(f"Listening beyond this machine. Setup code for the first password: {code}", flush=True)
        httpd.serve_forever()
        httpd.server_close()
        if not (switch and httpd.rebind is True):  # only Settings' switch rebuilds; anything else ends here
            return


def build(db, host, port, extra_hosts=(), network=None, require_password=False, switch=False):
    """The server, its setup code and whether it's local-only. `network` forces network mode (tests)."""
    if host == "localhost":
        host = "127.0.0.1"
    local_only = ipaddress.ip_address(host).is_loopback if network is None else not network
    global TAILSCALE
    if ipaddress.ip_address(host).version == 4 and ipaddress.ip_address(host) in CGNAT:
        TAILSCALE = True  # bound to a Tailscale address
    names = {"127.0.0.1", "localhost", "[::1]"}
    if not local_only:
        names |= my_addresses() | {h.lower() for h in extra_hosts}
        if host not in ("0.0.0.0", "::"):
            names.add(host)
    allowed = {f"{n}:{port}" for n in names} | ({f"{n}" for n in names} if port == 80 else set())
    code = ""
    if not local_only and not db.get("password_hash"):
        code = "-".join(secrets.token_hex(2) for _ in range(3))
        f = db.dir / "setup-code.txt"
        f.write_text(code + "\n")
        os.chmod(f, 0o600) if os.name == "posix" else None
    global SIGN_OUT, NETWORK_SWITCH
    SIGN_OUT = require_password or not local_only
    NETWORK_SWITCH = switch
    httpd = Server((host, port), make_handler(db, local_only, allowed, code, require_password))
    return httpd, code, local_only

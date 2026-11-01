"""Reading the removal inbox: match each broker's answer to the request it answers, sort it, and follow a
confirmation link only when it is plainly that broker's own.

Read-only by construction: the mailbox is opened with EXAMINE (select readonly) and messages are read with
BODY.PEEK, so nothing is marked read, moved or deleted. The client wrapper below refuses every other IMAP command.
The idea (a reference tag in the subject, keyword classes, a review queue for anything unsure) follows Scrubbr's
inbox.py (MIT); the code and the words are ours. Only headers we need and the class are stored, never a body.
"""
import email
import hashlib
import imaplib
import re
import ssl
import time
from email.header import decode_header
from email.utils import parseaddr, parsedate_to_datetime
from urllib.parse import urlsplit

from . import requests

POLL_EVERY = 15 * 60
OPEN_DAYS = 120  # requests older than this aren't waited on any more
KEEP_DAYS = 180  # mail rows are forgotten after this
ALLOWED = {"login", "select", "search", "fetch", "logout"}

VERIFY = re.compile(r"(verify|confirm)\s+(your\s+)?(request|email|identity|removal|opt.?out|deletion)|"
                    r"click\s+(the|this)\s+(link|button)\s+(below\s+)?to\s+(confirm|verify|complete)|"
                    r"confirm\s+(that\s+)?you\s+(want|would\s+like)|"
                    r"to\s+complete\s+the\s+(removal|opt.?out|deletion)\s+(process|request),?\s+(please\s+)?click", re.I)
# "Use our self-service tool instead" (Spokeo's help desk, 2026-10-29): the email route is closed; the site's own
# removal form is the way. Checked before VERIFY: these mails explain the form's own confirmation step.
USE_FORM = re.compile(r"self-service\s+(tool|form|page)|use\s+our\s+(online\s+)?(opt.?out|removal|privacy)\s+(form|tool|page)|"
                      r"(submit|make)\s+(your|a|the)\s+request\s+(through|via|using|on)\s+our\s+(website|form|opt.?out)", re.I)
NO_RECORD = re.compile(r"(could\s+not|couldn't|unable\s+to)\s+(find|locate)|no\s+(matching\s+)?(records?|profiles?|listings?)\s+"
                       r"(was\s+|were\s+)?found|we\s+(do\s+not|don't)\s+have\s+(any\s+)?(information|records|data)\s+(about|on)\s+you",
                       re.I)
REJECTED = re.compile(r"(cannot|can't|unable\s+to|will\s+not|won't)\s+(process|complete|honou?r|fulfill?)|"
                      r"request\s+(was\s+|has\s+been\s+)?(denied|rejected|declined)|not\s+(eligible|subject\s+to)", re.I)
ACKNOWLEDGED = re.compile(r"inquiry\s+received|request\s+(has\s+been\s+)?received|(we('ve|\s+have)?|we\s+have)\s+received\s+your\s+"
                          r"(request|email|message|inquiry)|ticket\s+(#|number|id)|this\s+is\s+an\s+automated\s+(reply|response)",
                          re.I)
BOUNCE_SUBJECT = re.compile(r"delivery\s+status\s+notification\s+\(failure\)|undeliverable|mail\s+delivery\s+failed|"
                            r"returned\s+mail|delivery\s+failure", re.I)
HELPDESKS = ("zendesk.com", "freshdesk.com", "helpscoutapp.com", "zohodesk.com", "kayako.com", "gorgias.help")
CONFIRMED = re.compile(r"(has|have)\s+been\s+(removed|deleted|suppressed|opted\s+out)|"
                       r"(removal|deletion|opt.?out)\s+(is\s+|has\s+been\s+)?(complete|completed|processed|successful)|"
                       r"successfully\s+(removed|deleted|opted\s+out|processed)", re.I)
TAG = re.compile(r"\[GD-(\d+)\]")
CONFIRM_LINK = re.compile(r"confirm|verif|token|activate|validate", re.I)
URL = re.compile(r"https?://[^\s\"'<>)\]]+", re.I)

WORDS = {"confirmed": "says it's done; the next scan checks it's gone",
         "acknowledged": "says it received the request",
         "use_form": "says to use its own removal form instead of email",
         "needs_verification": "asked to confirm the request",
         "no_record": "says it has no record — check it",
         "rejected": "turned the request down — check it",
         "unknown": "answered — check it"}


class ReadOnly:
    """An IMAP client that can only read: any other command is refused before it reaches the server."""

    def __init__(self, client):
        self._c = client

    def __getattr__(self, name):
        if name not in ALLOWED:
            raise PermissionError(f"inbox is read-only: {name} refused")
        return getattr(self._c, name)

    def select(self, mailbox="INBOX", readonly=True):
        if readonly is not True:
            raise PermissionError("inbox is read-only: select without readonly refused")
        return self._c.select(mailbox, readonly=True)

    def fetch(self, num, parts):
        if "PEEK" not in parts.upper():
            raise PermissionError("inbox is read-only: only BODY.PEEK fetches")
        return self._c.fetch(num, parts)


def classify(subject, body):
    """confirmed / needs_verification / no_record / rejected / unknown. A 'click to confirm' mail often also says
    'will be removed', so the request to verify is checked first; no record before rejected for the same reason."""
    text = f"{subject}\n{body}"
    for name, pat in (("use_form", USE_FORM), ("needs_verification", VERIFY), ("no_record", NO_RECORD), ("rejected", REJECTED),
                      ("acknowledged", ACKNOWLEDGED), ("confirmed", CONFIRMED)):
        if pat.search(text):
            return name
    return "unknown"


def broker_domains(broker):
    hosts = set()
    for u in (broker.get("website"), *(o.get("url") for o in broker.get("optout", []))):
        if u:
            hosts.add(urlsplit(u).netloc.lower().removeprefix("www."))
    if broker.get("request_email"):
        hosts.add(broker["request_email"].split("@")[-1].lower())
    return {h for h in hosts if h}


def on(host, domains):
    host = host.lower().removeprefix("www.")
    return any(host == d or host.endswith("." + d) for d in domains)


def sent_by(from_domain, domains):
    """The broker's own domain, or a help-desk service under the broker's name (spokeo.zendesk.com for spokeo.com).
    Only for knowing who wrote: links are followed on the broker's own domains alone."""
    if on(from_domain, domains):
        return True
    label = from_domain.split(".")[0]
    return any(from_domain.endswith("." + h) for h in HELPDESKS) and label in {d.split(".")[0] for d in domains}


def is_bounce(from_addr, subject):
    local = from_addr.split("@")[0].lower()
    return local in ("mailer-daemon", "postmaster") or bool(BOUNCE_SUBJECT.search(subject or ""))


def open_items(db, now=None):
    """Requests sent and not answered, and removals waiting on the site, from the last OPEN_DAYS."""
    now = time.time() if now is None else now
    since = now - OPEN_DAYS * 86400
    reqs = [dict(r) for r in db.q("""SELECT * FROM requests WHERE state='sent' AND (reply IS NULL OR reply IN ('','acknowledged','unknown'))
                                     AND sent >= ?""", (since,))]
    rems = [dict(r) for r in db.q("""SELECT f.source AS broker, f.person, f.id AS finding, m.updated FROM removals m
                                     JOIN findings f ON f.id=m.finding WHERE m.state IN ('requested','manual')
                                     AND m.updated >= ?""", (since,))]
    return reqs, rems


def decoded(value):
    out = []
    for text, enc in decode_header(value or ""):
        out.append(text.decode(enc or "utf-8", errors="replace") if isinstance(text, bytes) else text)
    return "".join(out)


def every_part(msg):
    """All the text in a mail, attached parts too: a bounce carries the original subject in its returned headers."""
    out = []
    for part in msg.walk():
        if part.get_content_maintype() == "text":
            raw = part.get_payload(decode=True) or b""
            out.append(raw.decode(part.get_content_charset() or "utf-8", errors="replace"))
    return "\n".join(out)


def body_text(msg):
    for part in msg.walk():
        if part.get_content_type() in ("text/plain", "text/html"):
            raw = part.get_payload(decode=True) or b""
            text = raw.decode(part.get_content_charset() or "utf-8", errors="replace")
            return re.sub(r"<[^>]+>", " ", text) if part.get_content_type() == "text/html" else text
    return ""


def match(db, subject, from_domain, by_broker, reqs, rems):
    """(request row or None, removal row or None, broker id) this mail answers, or (None, None, None)."""
    m = TAG.search(subject or "")
    if m:
        r = next((x for x in reqs if x["id"] == int(m.group(1))), None)
        if r and sent_by(from_domain, broker_domains(by_broker.get(r["broker"], {}))):
            return r, None, r["broker"]
        return None, None, None  # a tag with a stranger's From: don't trust it
    rq = [r for r in reqs if sent_by(from_domain, broker_domains(by_broker.get(r["broker"], {})))]
    rm = [x for x in rems if sent_by(from_domain, broker_domains(by_broker.get(x["broker"], {})))]
    # one household can have two people waiting on the same broker; without reading names we can't tell them apart
    people = {x["person"] for x in rq + rm}
    if len(people) != 1 or len(rq) > 1 or len(rm) > 1:
        return None, None, None
    if rm and (not rq or re.search(r"removal|opt.?out|listing|directory", subject or "", re.I)):
        return None, rm[0], rm[0]["broker"]  # a listing taken down isn't the deletion the email asked for
    return rq[0], None, rq[0]["broker"]


def follow(db, mail_id, url, open_page):
    """Open a broker's confirmation link (nothing typed, nothing pressed after it) and keep a screenshot."""
    from .removal import Evidence
    ev = Evidence(db.dir, f"mail{mail_id}", "link")
    ev.step(f"following the confirmation link on {urlsplit(url).netloc}{urlsplit(url).path}")
    open_page(url, lambda page: ev.step("the page after the link", page))
    return ev.dir


def poll(db, connect=None, open_page=None, now=None):
    """One look at the removal inbox. Returns how many new mails were sorted; 0 without connecting when nothing
    is waiting for an answer."""
    now = time.time() if now is None else now
    db.x("DELETE FROM mail WHERE seen < ?", (now - KEEP_DAYS * 86400,))
    reqs, rems = open_items(db, now)
    if not reqs and not rems:
        return 0
    box = requests.inbox(db)
    if not (box.get("address") and box.get("password")):
        return 0
    connect = connect or (lambda: imaplib.IMAP4_SSL(box.get("imap_host", "imap.gmail.com"), 993,
                                                    ssl_context=ssl.create_default_context(), timeout=60))
    if open_page is None:
        from .removal import browser_page
        open_page = browser_page(db)
    by_broker = {b["id"]: b for b in requests.brokers()}
    from .brokers import BROKERS
    for b in BROKERS:  # our scanned sites, for removal confirmations from sites without a request address
        by_broker.setdefault(b["id"], {"id": b["id"], "name": b["name"], "website": b["optout"], "optout": []})
    imap = ReadOnly(connect())
    sorted_ = 0
    try:
        imap.login(box["address"], box["password"])
        imap.select("INBOX", readonly=True)
        since = time.strftime("%d-%b-%Y", time.gmtime(min([r["sent"] for r in reqs] + [x["updated"] for x in rems])))
        typ, data = imap.search(None, f"SINCE {since}")
        for num in (data[0].split() if typ == "OK" and data and data[0] else []):
            typ, head = imap.fetch(num, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID FROM SUBJECT DATE)])")
            if typ != "OK" or not head or not head[0]:
                continue
            h = email.message_from_bytes(head[0][1])
            key = hashlib.sha256((h.get("Message-ID") or f"{h.get('From')}|{h.get('Date')}|{h.get('Subject')}")
                                 .encode("utf-8", "replace")).hexdigest()
            if db.q("SELECT 1 FROM mail WHERE msgid=?", (key,)):
                continue
            subject = decoded(h.get("Subject"))[:120]
            from_addr = parseaddr(decoded(h.get("From")))[1]
            from_domain = from_addr.split("@")[-1].lower()
            typ, full = imap.fetch(num, "(BODY.PEEK[])")
            msg = email.message_from_bytes(full[0][1]) if typ == "OK" and full and full[0] else None
            body = body_text(msg) if msg else ""
            if is_bounce(from_addr, subject):
                sorted_ += bounced(db, key, from_domain, subject, every_part(msg) if msg else "", now, by_broker)
                continue
            r, rem, broker = match(db, subject, from_domain, by_broker, reqs, rems)
            cls = classify(subject, body)
            if broker is None and not TAG.search(subject or "") and not any(
                    sent_by(from_domain, broker_domains(b)) for b in by_broker.values()):
                # nothing to do with any broker (an account notice, a newsletter): remembered so it's read once,
                # never shown
                db.x("INSERT INTO mail(msgid, from_domain, subject, date, cls, state, seen) VALUES(?,?,?,?,?,?,?)",
                     (key, from_domain, "", now, "unrelated", "ignored", now))
                continue
            try:
                date = parsedate_to_datetime(h.get("Date")).timestamp()
            except (TypeError, ValueError):
                date = now
            mid = db.x("INSERT INTO mail(msgid, from_domain, subject, date, cls, request, person, broker, state, seen) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (key, from_domain, subject, date, cls, r["id"] if r else None,
                        (r or rem or {}).get("person"), broker, "needs_you", now))
            sorted_ += 1
            if broker is None:
                continue  # unmatched: it waits in the queue for the owner
            act(db, mid, cls, r, rem, broker, by_broker[broker], from_domain, body, open_page)
    finally:
        try:
            imap.logout()
        except Exception:  # the connection may already be gone; nothing was written either way
            pass
    return sorted_


def bounced(db, key, from_domain, subject, body, now, by_broker):
    """A delivery failure: the request named in the returned mail's subject [GD-id] didn't arrive."""
    m = TAG.search(body or "") or TAG.search(subject or "")
    r = db.q("SELECT * FROM requests WHERE id=?", (int(m.group(1)),)) if m else []
    mid = db.x("INSERT INTO mail(msgid, from_domain, subject, date, cls, request, person, broker, state, seen) "
               "VALUES(?,?,?,?,?,?,?,?,?,?)", (key, from_domain, subject, now, "bounce", r[0]["id"] if r else None,
                                               r[0]["person"] if r else None, r[0]["broker"] if r else None,
                                               "done" if r else "needs_you", now))
    if r:
        r = dict(r[0])
        name = by_broker.get(r["broker"], {}).get("name", r["broker"])
        nxt = requests.bounced(db, r["id"])
        db.alert(r["person"], f"The removal email to {name} bounced: that address doesn't take mail."
                 + (f" {name} lists another address; it's ready on Remove from all, waiting for you to press Send."
                    if nxt else ""))
    return 1 if mid else 0


def act(db, mid, cls, r, rem, broker, info, from_domain, body, open_page):
    """What a matched answer changes: request/removal state, one plain alert, and a link followed when allowed."""
    pid = (r or rem)["person"]
    name = info.get("name", broker)
    if cls == "needs_verification":
        doms = broker_domains(info)
        theirs = [u for u in URL.findall(body) if on(urlsplit(u).netloc, doms)]
        # Spokeo's mail names the listing first and the confirmation link after it: take the link that confirms
        link = next((u for u in theirs if CONFIRM_LINK.search(urlsplit(u).path + "?" + urlsplit(u).query)),
                    theirs[0] if theirs else None)
        if link and on(from_domain, doms):
            try:
                follow(db, mid, link, open_page)
            except Exception as ex:
                db.alert(pid, f"{name} asked to confirm the request; Guard Dog couldn't open their link ({type(ex).__name__}). "
                              "Open the email in the removal inbox and click it.")
                return
            stored = urlsplit(link)
            db.x("UPDATE mail SET state='done', link=? WHERE id=?", (stored.netloc + stored.path, mid))
            if r:
                db.x("UPDATE requests SET reply='link followed' WHERE id=?", (r["id"],))
            db.alert(pid, f"{name} asked to confirm the request; Guard Dog clicked their link.")
            return
    if cls == "confirmed":
        db.x("UPDATE mail SET state='done' WHERE id=?", (mid,))
        if r:
            db.x("UPDATE requests SET state='confirmed', reply='confirmed' WHERE id=?", (r["id"],))
        if rem:
            from . import removal
            try:
                removal.move(db, rem["finding"], "confirmed", note=f"{name} emailed that it's done")
            except removal.BadMove:
                pass  # already further along
        db.alert(pid, f"{name} {WORDS['confirmed']}.")
        return
    if cls == "use_form":  # the email route is closed: done when the site's own form removal is under way
        if r:
            db.x("UPDATE requests SET reply='use_form' WHERE id=?", (r["id"],))
        under_way = rem or db.q("""SELECT 1 FROM removals m JOIN findings f ON f.id=m.finding WHERE f.person=? AND
                                   f.source=? AND m.state IN ('requested','confirmed','gone')""", (pid, broker))
        if under_way:
            db.x("UPDATE mail SET state='done' WHERE id=?", (mid,))
            return
        db.alert(pid, f"{name} {WORDS['use_form']}. If the scan finds a listing there, use the Remove button on its card.")
        return
    if cls == "acknowledged":  # "we got it": not news, and nothing for the owner to do
        db.x("UPDATE mail SET state='done' WHERE id=?", (mid,))
        if r:
            db.x("UPDATE requests SET reply='acknowledged' WHERE id=?", (r["id"],))
        return
    if r:
        db.x("UPDATE requests SET reply=? WHERE id=?", (cls, r["id"]))
    db.alert(pid, f"{name} {WORDS[cls]}: the email is in the removal inbox.")


def queue(db):
    """Mail waiting for the owner: newest first, sender domain and subject only."""
    return [dict(r) for r in db.q("SELECT * FROM mail WHERE state='needs_you' ORDER BY date DESC LIMIT 100")]

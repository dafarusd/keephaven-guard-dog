"""Removal requests by email: which brokers get one, the drafts, sending from the removal inbox, and the follow-up
clock. Nothing is sent until the owner presses Send on the preview page."""
import json
import os
import smtplib
import ssl
import time
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path

from . import letters

PEOPLE_SEARCH = ("people-search", "background-check")
# Which address a deletion request goes to when a broker prints several: the privacy office first, never support.
REQUEST_EMAIL_ORDER = (r"^privacy", r"^dpo|dataprotection", r"dsar|ccpa|cpra|optout|opt-out|remove|delete|rights|request",
                       r"^legal|^compliance")


def pick_address(addrs):
    """The address a request goes to, by REQUEST_EMAIL_ORDER; one address alone is used as is; several with none
    matching: none (don't guess customer support)."""
    import re as _re
    for pat in REQUEST_EMAIL_ORDER:
        for a in addrs:
            if _re.search(pat, a.split("@")[0]):
                return a
    return addrs[0] if len(addrs) == 1 else ""


def dead(db):
    return {r["address"] for r in db.q("SELECT address FROM bounced")}


def address_for(db, b):
    """This broker's request address, skipping any that bounced here before."""
    gone = dead(db)
    if b.get("request_email") and b["request_email"] not in gone:
        return b["request_email"]
    return pick_address([e["email"] for e in b.get("emails", []) if e["email"] not in gone])


DAILY_CAP = 250  # Gmail allows about 500 a day; half of it leaves room for everything else the inbox sends
SECRET = "removal-inbox.json"  # address, host and app password: its own file, readable by the owner only

class NotReady(ValueError):
    """Something the owner has to set first (removal inbox, consent)."""


def brokers():
    p = Path(__file__).parent / "data" / "brokers.json"
    return json.loads(p.read_text())["brokers"]


def inbox(db):
    f = db.dir / SECRET
    return json.loads(f.read_text()) if f.is_file() else {}


def save_inbox(db, address, password, host="smtp.gmail.com", port=465):
    f = db.dir / SECRET
    old = inbox(db)
    data = {"address": address.strip(), "host": host.strip() or "smtp.gmail.com", "port": int(port or 465),
            "password": password or old.get("password", "")}  # an empty box keeps the saved password
    f.write_text(json.dumps(data))
    os.chmod(f, 0o600)


def check_ready(db, person):
    box = inbox(db)
    if not (box.get("address") and box.get("password")):
        raise NotReady("Set up the removal inbox in Settings first (its address and app password).")
    if (person.get("label") or "").strip().lower() not in ("me", "myself", "i") and not (person.get("consent") or "").strip():
        raise NotReady("This person isn't you: add who agreed and when (Edit details > Who agreed) before Guard Dog "
                       "writes to anyone in their name.")


def targets(db, pid, everyone=False):
    """Brokers to write to: every broker the scan found listing them, every people-search broker, and with
    `everyone` the whole list. Only brokers with a request address published on their own site."""
    found = {r["source"]: dict(r) for r in db.q(
        "SELECT * FROM findings WHERE person=? AND status IN ('listed','possible') ORDER BY status='listed' DESC", (pid,))}
    out = []
    for b in brokers():
        if not address_for(db, b):
            continue
        if b["id"] in found or b["category"] in PEOPLE_SEARCH or everyone:
            out.append((b, found.get(b["id"])))
    return out


def plan(db, pid, everyone=False):
    """Drafts for every target that hasn't been written to yet; returns the drafts waiting to go."""
    person = db.person(pid)
    for b, finding in targets(db, pid, everyone):
        to = address_for(db, b)
        if db.q("SELECT 1 FROM requests WHERE person=? AND (broker=? OR to_addr=?)", (pid, b["id"], to)):
            continue  # already written to, or the same address already gets one (one company, several sites)
        rid = db.x("INSERT INTO requests(person, broker, finding, to_addr, state, created) VALUES(?,?,?,?,?,?)",
                   (pid, b["id"], finding["id"] if finding else None, to, "draft", time.time()))
        subject, body, kind = letters.write(person, b, inbox(db).get("address", "") or db.get("removal_email"), rid, finding)
        db.x("UPDATE requests SET subject=?, body=?, kind=? WHERE id=?", (subject, body, kind, rid))
    return drafts(db, pid)


def drafts(db, pid, state="draft"):
    return [dict(r) for r in db.q("SELECT * FROM requests WHERE person=? AND state=? ORDER BY broker", (pid, state))]


def approve(db, pid):
    """The owner pressed Send: every draft is approved to go (today up to the cap, the rest on the next days)."""
    check_ready(db, db.person(pid))
    return db.n("UPDATE requests SET state='approved' WHERE person=? AND state='draft'", (pid,))


def sent_today(db):
    return db.q("SELECT COUNT(*) n FROM requests WHERE sent > ?", (time.time() - 86400,))[0]["n"]


def send_one(db, r, smtp):
    box = inbox(db)
    msg = EmailMessage()
    msg["From"] = box["address"]
    msg["To"] = r["to_addr"]
    msg["Subject"] = r["subject"]
    msg["Message-ID"] = make_msgid(domain=box["address"].split("@")[-1])
    msg.set_content(r["body"])
    smtp.send_message(msg)


def send(db, pid, connect=None):
    """Send the approved requests for one person, up to the daily cap. Returns (sent, left for later, failed).
    Only ever sends what the owner approved on the preview page."""
    check_ready(db, db.person(pid))
    box = inbox(db)
    waiting = drafts(db, pid, "approved")
    room = max(0, DAILY_CAP - sent_today(db))
    now_batch, later = waiting[:room], waiting[room:]
    sent = failed = 0
    if not now_batch:
        return 0, len(later), 0
    connect = connect or (lambda: smtplib.SMTP_SSL(box["host"], box["port"], context=ssl.create_default_context(),
                                                   timeout=60))
    with connect() as smtp:
        smtp.login(box["address"], box["password"])
        for r in now_batch:
            try:
                send_one(db, r, smtp)
            except Exception as ex:
                db.x("UPDATE requests SET state='failed', note=? WHERE id=?", (f"{type(ex).__name__}: {str(ex)[:150]}", r["id"]))
                failed += 1
                continue
            now = time.time()
            db.x("UPDATE requests SET state='sent', sent=?, follow_up=? WHERE id=?",
                 (now, now + letters.FOLLOW_UP_DAYS[r["kind"]] * 86400, r["id"]))
            sent += 1
    return sent, len(later), failed


def due(db, pid, now=None):
    """Sent requests with no answer, or only "we got it" or an unclear one, whose follow-up day has come."""
    now = time.time() if now is None else now
    return [dict(r) for r in db.q("""SELECT * FROM requests WHERE person=? AND state='sent' AND (reply IS NULL OR reply IN ('','acknowledged','unknown'))
                                     AND follow_up <= ? ORDER BY follow_up""", (pid, now))]


def follow_up(db, rid):
    """Send the same request again as a follow-up draft (the owner presses Send for it like any other)."""
    r = dict(db.q("SELECT * FROM requests WHERE id=?", (rid,))[0])
    days = int((time.time() - (r["sent"] or time.time())) // 86400)
    body = (f"I sent the request below {days} days ago and haven't had an answer. Please confirm once my information "
            f"has been deleted.\n\n" + r["body"])
    db.x("UPDATE requests SET state='draft', reply=NULL, subject=?, body=?, note='follow-up' WHERE id=?",
         ("Follow-up: " + r["subject"].removeprefix("Follow-up: "), body, rid))


def send_waiting(db):
    """The scheduler's part: approved requests the cap held back yesterday go out today."""
    for pid in {r["person"] for r in db.q("SELECT person FROM requests WHERE state='approved'")}:
        if DAILY_CAP - sent_today(db) <= 0:
            return
        try:
            sent, later, failed = send(db, pid)
        except NotReady:
            continue
        except Exception as ex:  # e.g. the app password was revoked: say so, keep the requests for next time
            db.alert(pid, f"Couldn't send removal emails: {type(ex).__name__}: {str(ex)[:120]}")
            continue
        if sent:
            db.alert(pid, f"Sent {sent} more removal email{'s' if sent != 1 else ''}.")


def bounced(db, request_id):
    """The address of this request doesn't take mail: never write to it again from this install, and if the broker
    printed another address, make a draft to that one (sent only when the owner presses Send). Returns the new
    address or ''."""
    r = dict(db.q("SELECT * FROM requests WHERE id=?", (request_id,))[0])
    db.x("INSERT OR IGNORE INTO bounced(address, ts) VALUES(?,?)", (r["to_addr"], time.time()))
    b = next((x for x in brokers() if x["id"] == r["broker"]), None)
    nxt = address_for(db, b) if b else ""
    if nxt:
        db.x("UPDATE requests SET state='draft', to_addr=?, sent=NULL, follow_up=NULL, reply=NULL, "
             "note=? WHERE id=?", (nxt, f"Try {b['name']}'s other address", request_id))
    else:
        db.x("UPDATE requests SET state='failed', note='the address bounced' WHERE id=?", (request_id,))
    return nxt

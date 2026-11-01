"""SQLite store. One file in the data dir; page copies live next to it under copies/."""
import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS people(id INTEGER PRIMARY KEY, label TEXT, names TEXT, emails TEXT, phones TEXT,
    places TEXT, usernames TEXT, consent TEXT, created REAL, last_scan REAL, relatives TEXT, birth_year INTEGER,
    home_state TEXT, home_country TEXT);
CREATE TABLE IF NOT EXISTS bounced(address TEXT PRIMARY KEY, ts REAL);
CREATE TABLE IF NOT EXISTS ghosts(person INTEGER, platform TEXT, username TEXT, url TEXT, snapshots INTEGER,
    first TEXT, last TEXT, copy TEXT, found REAL, UNIQUE(person, url));
CREATE TABLE IF NOT EXISTS accounts(person INTEGER, site TEXT, username TEXT, url TEXT, tags TEXT, found REAL,
    UNIQUE(person, url));
CREATE TABLE IF NOT EXISTS mail(id INTEGER PRIMARY KEY, msgid TEXT UNIQUE, from_domain TEXT, subject TEXT, date REAL,
    cls TEXT, request INTEGER, person INTEGER, broker TEXT, state TEXT, link TEXT, seen REAL);
CREATE TABLE IF NOT EXISTS requests(id INTEGER PRIMARY KEY, person INTEGER, broker TEXT, finding INTEGER,
    to_addr TEXT, kind TEXT, subject TEXT, body TEXT, state TEXT, created REAL, sent REAL, follow_up REAL,
    reply TEXT, note TEXT, UNIQUE(person, broker));
CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS findings(id INTEGER PRIMARY KEY, person INTEGER, source TEXT, url TEXT, status TEXT,
    matched TEXT, copy TEXT, first_seen REAL, last_seen REAL, removal TEXT, removal_ts REAL,
    last_checked REAL, check_note TEXT, ever_listed INTEGER DEFAULT 0, kind TEXT DEFAULT 'broker',
    UNIQUE(person, source, url));
CREATE TABLE IF NOT EXISTS breaches(person INTEGER, email TEXT, breach TEXT, domain TEXT, date TEXT,
    exposed TEXT, password_risk TEXT, first_seen REAL, PRIMARY KEY(person, email, breach));
CREATE TABLE IF NOT EXISTS alerts(id INTEGER PRIMARY KEY, person INTEGER, ts REAL, text TEXT, seen INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS removals(finding INTEGER PRIMARY KEY, state TEXT, updated REAL, evidence TEXT,
    note TEXT, history TEXT, prefill TEXT);
CREATE TABLE IF NOT EXISTS scans(id INTEGER PRIMARY KEY, person INTEGER, started REAL, finished REAL,
    state TEXT, progress TEXT, stealth INTEGER);
"""

DEFAULTS = {"stealth": "0", "sweep": "1", "sweep_limit": "200", "workers": "6", "stealth_workers": "2", "rescan_hours": "6", "removal_email": "", "hibp_key": "", "proxy": "",
            "delay_min": "2", "delay_max": "6", "password_hash": "", "archive": "1", "accounts": "0", "network": "0"}

LIST_FIELDS = ("names", "emails", "phones", "places", "usernames", "relatives")


class DB:
    def __init__(self, data_dir: Path):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "copies").mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self.c = sqlite3.connect(self.dir / "guard-dog.sqlite", check_same_thread=False)
        self.c.row_factory = sqlite3.Row
        self.c.executescript(SCHEMA)
        have_people = {r[1] for r in self.c.execute("PRAGMA table_info(people)")}
        for col, typ in (("relatives", "TEXT"), ("birth_year", "INTEGER"), ("home_state", "TEXT"), ("home_country", "TEXT"),
                         ("archive_checked", "REAL"), ("accounts_checked", "REAL")):
            if col not in have_people:
                self.c.execute(f"ALTER TABLE people ADD COLUMN {col} {typ}")
        if "prefill" not in {r[1] for r in self.c.execute("PRAGMA table_info(removals)")}:
            self.c.execute("ALTER TABLE removals ADD COLUMN prefill TEXT")
        have = {r[1] for r in self.c.execute("PRAGMA table_info(findings)")}
        for col, typ in (("last_checked", "REAL"), ("check_note", "TEXT"), ("ever_listed", "INTEGER DEFAULT 0"),
                         ("kind", "TEXT DEFAULT 'broker'")):
            if col not in have:
                self.c.execute(f"ALTER TABLE findings ADD COLUMN {col} {typ}")
        # removal states used to live in findings.removal; the removals table is the only system now
        self.c.execute("""INSERT OR IGNORE INTO removals(finding, state, updated, evidence, note, history)
            SELECT id, removal, COALESCE(removal_ts, 0), '', 'carried over from an older version',
            json_array(json_object('from', 'found', 'to', removal, 'at', COALESCE(removal_ts, 0),
                                   'note', 'carried over from an older version', 'evidence', ''))
            FROM findings WHERE removal IN ('requested', 'gone')""")
        self.c.execute("UPDATE findings SET removal=NULL, removal_ts=NULL WHERE removal IS NOT NULL")
        # settings older versions used and nothing reads any more
        self.c.execute("DELETE FROM settings WHERE k IN ('rescan_days')")
        self.c.commit()

    def q(self, sql, args=()):
        with self.lock:
            return self.c.execute(sql, args).fetchall()

    def x(self, sql, args=()):
        with self.lock:
            cur = self.c.execute(sql, args)
            self.c.commit()
            return cur.lastrowid

    # settings
    def get(self, k):
        r = self.q("SELECT v FROM settings WHERE k=?", (k,))
        return r[0]["v"] if r else DEFAULTS.get(k, "")

    def set(self, k, v):
        self.x("INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))

    # people: list fields are stored as JSON arrays
    def person(self, pid):
        r = self.q("SELECT * FROM people WHERE id=?", (pid,))
        return self._p(r[0]) if r else None

    def people(self):
        return [self._p(r) for r in self.q("SELECT * FROM people ORDER BY id")]

    @staticmethod
    def _p(r):
        p = dict(r)
        for f in LIST_FIELDS:
            p[f] = json.loads(p[f] or "[]")
        return p

    def save_person(self, pid, label, consent, birth_year=None, home_state="", home_country="US", **lists):
        vals = [json.dumps(lists.get(f, [])) for f in LIST_FIELDS]
        if pid:
            old = self.person(pid)
            if old and (old["usernames"], old["emails"]) != (lists.get("usernames", []), lists.get("emails", [])):
                # new usernames: look again
                self.x("UPDATE people SET archive_checked=NULL, accounts_checked=NULL WHERE id=?", (pid,))
            self.x(f"UPDATE people SET label=?, consent=?, birth_year=?, home_state=?, home_country=?, "
                   f"{', '.join(f + '=?' for f in LIST_FIELDS)} WHERE id=?",
                   (label, consent, birth_year, home_state, home_country, *vals, pid))
            return pid
        cols = ("label", "consent", "birth_year", "home_state", "home_country", "created") + LIST_FIELDS
        return self.x(f"INSERT INTO people({', '.join(cols)}) VALUES({', '.join('?' * len(cols))})",
                      (label, consent, birth_year, home_state, home_country, time.time(), *vals))

    def delete_person(self, pid):
        self.x("DELETE FROM removals WHERE finding IN (SELECT id FROM findings WHERE person=?)", (pid,))
        for t in ("findings", "breaches", "alerts", "scans", "requests", "mail", "ghosts", "accounts"):
            self.x(f"DELETE FROM {t} WHERE person=?", (pid,))
        self.x("DELETE FROM people WHERE id=?", (pid,))

    def n(self, sql, args=()):
        """Run a write, return how many rows it changed."""
        with self.lock:
            cur = self.c.execute(sql, args)
            self.c.commit()
            return cur.rowcount

    def alert(self, pid, text):
        self.x("INSERT INTO alerts(person, ts, text) VALUES(?,?,?)", (pid, time.time(), text))

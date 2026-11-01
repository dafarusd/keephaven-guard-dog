"""Old copies of a person's profiles in the Internet Archive ("ghost profiles"): a profile deleted years ago can
still be read there. The Wayback CDX index is searched by address, not by content, so this asks whether known
profile-address shapes for each of their usernames were ever archived. Free, no key; each username goes to
web.archive.org. From incognito (MIT, backend/scanner/wayback.py): the 15 address shapes, the query and the pacing.

Removal: the Internet Archive takes exclusion requests at info@archive.org with the addresses, the time period to
exclude and when the account was theirs (help.archive.org, "How do I request to remove something from
archive.org?", read 2026-10-29)."""
import json
import time
import urllib.parse

from .breaches import _get

CDX = "https://web.archive.org/cdx/search/cdx"
PROFILES = (
    ("Twitter/X", "twitter.com/{u}"), ("Instagram", "instagram.com/{u}"), ("Facebook", "facebook.com/{u}"),
    ("Reddit", "reddit.com/user/{u}"), ("GitHub", "github.com/{u}"), ("LinkedIn", "linkedin.com/in/{u}"),
    ("TikTok", "tiktok.com/@{u}"), ("YouTube", "youtube.com/@{u}"), ("Medium", "medium.com/@{u}"),
    ("Pinterest", "pinterest.com/{u}"), ("Tumblr", "{u}.tumblr.com"), ("Keybase", "keybase.io/{u}"),
    ("Flickr", "flickr.com/people/{u}"), ("Mastodon", "mastodon.social/@{u}"), ("About.me", "about.me/{u}"),
)
PAUSE = 1.5  # the CDX server turns bursts away (429) per address
BACKOFF = 20
EVERY_DAYS = 7  # the archive changes slowly: once a week is plenty
REMOVE_TO = "info@archive.org"


def usernames(person):
    """Their usernames; without any, the part of their emails before the @ (what most people reuse)."""
    out = []
    for u in person.get("usernames") or [e.split("@")[0] for e in person.get("emails") or []]:
        u = u.strip().lstrip("@").lower()
        if len(u) >= 3 and u not in out and all(ch.isalnum() or ch in "._-" for ch in u):
            out.append(u)
    return out


def cdx(url, sleep=time.sleep):
    """Archived copies of one address: [(timestamp, original)], [] if never archived."""
    q = CDX + "?" + urllib.parse.urlencode({"url": url, "output": "json", "fl": "timestamp,original,statuscode",
                                            "filter": "statuscode:200", "collapse": "digest", "limit": "500"})
    st, body = _get(q)
    if st == 429:
        sleep(BACKOFF)
        st, body = _get(q)
    if st == 429:
        raise RateLimited()
    if st != 200:
        raise RuntimeError(f"the Internet Archive answered {st}")
    rows = json.loads(body or b"[]")
    return [(r[0], r[1]) for r in rows[1:]]


class RateLimited(RuntimeError):
    """The archive asked us to slow down twice in a row: stop for this scan."""


def check(person, sleep=time.sleep, cancelled=lambda: False):
    """[{platform, username, url, snapshots, first, last, copy}] and [problems]."""
    hits, problems = [], []
    checks = [(pl, u, shape.format(u=u)) for u in usernames(person) for pl, shape in PROFILES]
    for i, (platform, u, url) in enumerate(checks):
        if cancelled():
            break
        try:
            rows = cdx(url, sleep)
        except RateLimited:
            problems.append("the Internet Archive asked to slow down; the rest waits for the next scan")
            break
        except Exception as ex:
            problems.append(f"{platform} ({u}): {type(ex).__name__}: {str(ex)[:80]}")
            rows = []
        if rows:
            stamps = sorted(r[0] for r in rows)
            original = rows[-1][1]
            hits.append(dict(platform=platform, username=u, url=original, snapshots=len(rows), first=stamps[0][:8],
                             last=stamps[-1][:8], copy=f"https://web.archive.org/web/{stamps[-1]}/{original}"))
        if i + 1 < len(checks):
            sleep(PAUSE)
    return hits, problems


def due(db, person, now=None):
    now = time.time() if now is None else now
    last = person.get("archive_checked")
    return db.get("archive") != "0" and bool(usernames(person)) and (not last or now - last >= EVERY_DAYS * 86400)


def record(db, pid, hits, first_time, complete, now=None):
    """Keep the hits; a profile copy not seen before is news after the first look. Only a look that finished
    waits a week for the next; one cut short is tried again on the next scan."""
    now = time.time() if now is None else now
    for h in hits:
        new = db.n("""INSERT OR IGNORE INTO ghosts(person, platform, username, url, snapshots, first, last, copy, found)
                      VALUES(?,?,?,?,?,?,?,?,?)""", (pid, h["platform"], h["username"], h["url"], h["snapshots"],
                                                     h["first"], h["last"], h["copy"], now))
        if not new:
            db.x("UPDATE ghosts SET snapshots=?, last=?, copy=? WHERE person=? AND url=?",
                 (h["snapshots"], h["last"], h["copy"], pid, h["url"]))
        elif not first_time:
            db.alert(pid, f"The Internet Archive has an old copy of a {h['platform']} profile named {h['username']}.")
    if complete:
        db.x("UPDATE people SET archive_checked=? WHERE id=?", (now, pid))

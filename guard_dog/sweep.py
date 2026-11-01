"""The web sweep: ask search engines about every name, email, phone and username, then check every page
they return. This is what finds a person beyond the known people-search sites."""
import base64
import re
from urllib.parse import parse_qs, quote_plus, unquote, urlsplit

from .brokers import BROKERS, split_place

# Plain = answers a normal request (checked 2026-10-29). Stealth = only through the stealth browser.
ENGINES = [
    dict(id="brave", url="https://search.brave.com/search?q={q}", stealth=False),
    dict(id="bing", url="https://www.bing.com/search?q={q}&count=30", stealth=False),
    dict(id="mojeek", url="https://www.mojeek.com/search?q={q}", stealth=False),
    dict(id="duckduckgo", url="https://html.duckduckgo.com/html/?q={q}", stealth=True),
    dict(id="google", url="https://www.google.com/search?q={q}&num=30", stealth=True),
    dict(id="startpage", url="https://www.startpage.com/do/search?q={q}", stealth=True),
]
SKIP = re.compile(r"(^|\.)(brave\.com|bing\.com|bing\.net|microsoft\.com|msn\.com|mojeek\.com|duckduckgo\.com|google\.[a-z.]+|"
                  r"gstatic\.com|googleusercontent\.com|startpage\.com|youtube\.com/results|w3\.org|schema\.org)$")
PER_QUERY = 20
# An engine saying no: don't keep asking. DuckDuckGo refuses with 202; Playwright's own TimeoutError (not the builtin
# one) is a page that never loaded.
REFUSED = ("captcha or block page", "http 202", "http 403", "http 429", "http 503", "TimeoutError")
BROKER_HOSTS = {urlsplit(b["search"]("a", "b", "c", "OH")).netloc.removeprefix("www."): b for b in BROKERS}

GENERIC_HOW = ("Ask the site to take it down: look for Contact, Privacy or Report at the bottom of the page. "
               "To drop it from Google results, use Google's Results about you tool.")
GOOGLE_RAY = "https://myactivity.google.com/results-about-you"


def queries(person):
    """(query, name, place) for everything worth searching. name/place are what the query itself says,
    so the matcher doesn't count them as evidence."""
    out = []
    for n in person["names"]:
        if len(n.split()) < 2:
            continue
        out.append((f'"{n}"', n, ""))
        for p in person["places"]:
            c, s = split_place(p)
            if c:
                out.append((f'"{n}" "{c}"', n, p))
    lead = person["names"][0] if person["names"] else ""
    for e in person["emails"]:
        out.append((f'"{e}"', lead, ""))
    for ph in person["phones"]:
        d = re.sub(r"\D", "", ph)[-10:]
        if len(d) == 10:
            out.append((f'"{d[:3]}-{d[3:6]}-{d[6:]}"', lead, ""))
            out.append((f'"({d[:3]}) {d[3:6]}-{d[6:]}"', lead, ""))
    for u in person["usernames"]:
        if len(u) >= 4:
            out.append((f'"{u}"', lead, ""))
    return out


def unwrap(h):
    """The real destination of a search engine's result link."""
    if h.startswith("//"):
        h = "https:" + h
    u = urlsplit(h)
    qs = parse_qs(u.query)
    if "uddg" in qs:  # DuckDuckGo
        return unquote(qs["uddg"][0])
    if u.netloc.endswith("bing.com") and u.path.startswith("/ck/") and "u" in qs:
        v = qs["u"][0]
        if v.startswith("a1"):
            v = v[2:]
            try:
                return base64.urlsafe_b64decode(v + "=" * (-len(v) % 4)).decode()
            except (ValueError, UnicodeDecodeError):  # not a Bing-encoded link after all: drop it
                return ""
    if u.netloc.startswith("www.google.") and u.path == "/url" and "q" in qs:
        return qs["q"][0]
    return h


def needles(q):
    """What a result must mention to count: the first quoted phrase of the query (a name, email, phone or username)."""
    m = re.findall(r'"([^"]+)"', q)
    return m[0].lower() if m else q.lower()


def mentions(block, needle):
    if re.fullmatch(r"[\d()\s.+-]+", needle):  # a phone number: compare digits only
        d = re.sub(r"\D", "", needle)
        return len(d) >= 7 and d in re.sub(r"\D", "", block)
    return " ".join(needle.split()) in " ".join(block.lower().split())


def outbound(href):
    u = urlsplit(unwrap((href or "").strip()))
    return u.scheme in ("http", "https") and u.netloc and not SKIP.search(u.netloc.lower().removeprefix("www."))


def own_box(a, h):
    """The text of this result alone: climb from the link while the box holds no other result's link
    (title, address line and snippet; never the whole results list)."""
    block, node = a.get_all_text() + " " + h, a
    for _ in range(4):
        up = node.parent
        if up is None:
            break
        others = {unwrap(x.attrib.get("href") or "").split("#")[0] for x in up.css("a") if outbound(x.attrib.get("href"))}
        if others - {h}:
            break
        node = up
        block = node.get_all_text()[:2000] + " " + h
    return block


def results(page, q):
    """(relevant result urls, candidate count). A result counts only if its link or the text around it mentions
    what was searched; search engines that distrust a request answer with unrelated results instead of refusing."""
    want = needles(q)
    seen, out, candidates = set(), [], 0
    for a in page.css("a"):
        h = unwrap((a.attrib.get("href") or "").strip())
        u = urlsplit(h)
        if u.scheme not in ("http", "https") or not u.netloc:
            continue
        if SKIP.search(u.netloc.lower().removeprefix("www.")):
            continue
        h = h.split("#")[0]
        if h in seen:
            continue
        seen.add(h)
        candidates += 1
        if mentions(own_box(a, h), want):
            out.append(h)
    return out[:PER_QUERY], candidates


def search(db, person, prog, pause, cancelled, workers=6, browsers=None):
    """Every unique result URL across every engine and query, with the query that found it,
    plus {engine: [failure reasons]} so a sweep that was blocked never looks complete.
    Engines run side by side; each engine gets its queries one at a time with a pause between."""
    from .scanner import blocked_page, get, in_parallel
    import threading
    stealth = db.get("stealth") == "1"
    qs = queries(person)
    engines = [e for e in ENGINES if stealth or not e["stealth"]]
    found = {}  # (query index, engine index) -> result urls
    failed = {}
    lock = threading.Lock()
    total, done = len(qs) * len(engines), [0]
    refused = set()  # engines that blocked or hung once: the rest of their searches would only do the same

    def job(item):
        ei, qi = item
        e, (q, name, place) = engines[ei], qs[qi]
        with lock:
            done[0] += 1
            prog(f"web search {done[0]}/{total}: {e['id']}")
            if ei in refused:
                failed.setdefault(e["id"], []).append("skipped after a block")
                return
        why = None
        try:
            url = e["url"].format(q=quote_plus(q))
            page = get(url, db, e["stealth"])
            if page.status in (403, 429, 503) and stealth and not e["stealth"]:  # refused a plain request: try as a person
                page = get(url, db, True)
            text = page.get_all_text()
            title = (page.css("title::text").get() or "").lower()
            if page.status != 200:
                why = f"http {page.status}"
            elif blocked_page(200, text) or "captcha" in title:
                why = "captcha or block page"
            else:
                rs, candidates = results(page, q)
                if not rs and candidates >= 5:
                    why = "results unrelated to the search"
                with lock:
                    found[(qi, ei)] = rs
        except TimeoutError as ex:  # the pool's own limit: its words say what happened
            why = str(ex)
        except Exception as ex:
            why = type(ex).__name__
        if why:
            with lock:
                failed.setdefault(e["id"], []).append(why)
                if why in REFUSED or why.startswith(("the page didn't finish", "memory ran low")):
                    refused.add(ei)

    for it, why in in_parallel([[(ei, qi) for qi in range(len(qs))] for ei in range(len(engines))], job, workers, pause,
                               cancelled, browsers):
        name = engines[it[0]]["id"] if isinstance(it, tuple) else str(it)
        failed.setdefault(name, []).append(why)
    hits, seen = [], set()
    for qi, (q, name, place) in enumerate(qs):  # keep query order: names first, then emails, phones, usernames
        for ei, e in enumerate(engines):
            for r in found.get((qi, ei), []):
                if r not in seen:
                    seen.add(r)
                    hits.append(dict(url=r, domain=urlsplit(r).netloc.lower().removeprefix("www."),
                                     name=name, place=place, query=q, engine=e["id"]))
    return hits, failed


def source_for(hit):
    """Known people-search site → its own opt-out; anything else → generic takedown steps. A page found on a
    people-search site is "broker-web", not "broker": it isn't one of the search addresses supersede() retires."""
    b = BROKER_HOSTS.get(hit["domain"])
    if b:
        return dict(b, kind="broker-web", checked="sweep")
    return dict(id=hit["domain"], name=hit["domain"], kind="web", checked="sweep", blocks_bots=False,
                optout=GOOGLE_RAY, how=GENERIC_HOW)

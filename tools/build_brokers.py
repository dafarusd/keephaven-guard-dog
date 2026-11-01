"""Build guard_dog/data/brokers.json from each broker's OWN pages.

Leads (names, websites, opt-out addresses to try) come from drumandbytes/eraser's data/brokers.yaml, whose licence
can't be relied on, so nothing from it is copied into our file except as a pointer to where to look. For every lead
we fetch the broker's own site (plain requests only), follow its privacy / opt-out links, and record only what we
read there: the opt-out pages that answered, the email addresses printed on those pages, the page each came from,
and the date. Leads we can't verify stay out.

    .venv/bin/python -m tools.build_brokers LEADS.yaml LEAD_COMMIT [--only id,id] [--workers 8]
"""
import argparse
import datetime as dt
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin, urlsplit

OUT = Path(__file__).resolve().parent.parent / "guard_dog" / "data" / "brokers.json"
LINK_WORDS = re.compile(r"privacy|opt.?out|do.?not.?sell|remov|suppress|ccpa|your.?choices|data.?request|delete", re.I)
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
FIELDS = ("id", "name", "email", "website", "opt_out_url", "region", "category")


def leads(path):
    """The few fields we need from eraser's YAML (a list of flat "- id: …" blocks)."""
    return leads_from_text(Path(path).read_text())


def leads_from_text(text):
    out, cur = [], None
    for line in text.splitlines():
        m = re.match(r"^\s*-\s+id:\s*(.*)$", line)
        if m:
            cur = {"id": m.group(1).strip().strip('"')}
            out.append(cur)
            continue
        m = re.match(r"^\s{6}(\w+):\s*(.*)$", line)
        if cur is not None and m and m.group(1) in FIELDS:
            cur[m.group(1)] = m.group(2).strip().strip('"')
    return out


def site(url):
    return urlsplit(url).netloc.lower().removeprefix("www.")


def same_site(url, home):
    h, s = site(url), site(home)
    return h == s or h.endswith("." + s) or s.endswith("." + h)


def fetch(url):
    from scrapling.fetchers import Fetcher
    try:
        p = Fetcher.get(url, impersonate="chrome", timeout=25, follow_redirects=True, retries=1)
        return p.status, p
    except Exception as ex:
        return type(ex).__name__, None


def check(lead, today, pause):
    home = lead.get("website") or ""
    rec = {"id": lead["id"], "name": lead.get("name", lead["id"]), "website": home, "region": lead.get("region", ""),
           "category": lead.get("category", ""), "checked": today, "optout": [], "emails": [], "notes": []}
    if not home.startswith("http"):
        rec["notes"].append("no website in the lead")
        return rec
    st, page = fetch(home)
    if page is None or st != 200:
        rec["notes"].append(f"home page: {st}")
        if st in (401, 403, 429, 503):
            rec["blocks_bots"] = True
        targets = []
    else:
        targets = []
        for a in page.css("a"):
            href, text = a.attrib.get("href") or "", a.get_all_text()
            if LINK_WORDS.search(href + " " + text):
                u = urljoin(str(page.url), href).split("#")[0]
                if u.startswith("http") and same_site(u, home) and u not in targets:
                    targets.append(u)
    lead_optout = lead.get("opt_out_url") or ""
    if lead_optout.startswith("http") and same_site(lead_optout, home) and lead_optout not in targets:
        targets.insert(0, lead_optout)  # the lead's address is tried, and kept only if the site answers it
    for u in targets[:5]:
        pause()
        st, p = fetch(u)
        if p is None:
            rec["optout"].append({"url": u, "status": st})
            continue
        text = p.get_all_text()
        title = p.css("title::text").get() or ""
        # what the page IS (its address and title), not what it mentions: every privacy policy mentions opting out
        kind = "opt-out" if re.search(r"opt.?out|remov|suppress|do.?not.?sell|delete|your.?choices|control",
                                      str(p.url) + " " + title, re.I) else "privacy"
        rec["optout"].append({"url": str(p.url), "status": p.status, "kind": kind})
        if p.status == 200:
            mails = {str(m).lower().rstrip(".") for m in EMAIL.findall(str(text))}
            mails |= {str(h).split(":", 1)[1].split("?")[0].lower() for h in p.css("a::attr(href)").getall()
                      if str(h).startswith("mailto:")}
            for e in sorted(m for m in mails if EMAIL.fullmatch(m)):
                if same_site("https://" + e.split("@")[1], home) and not any(x["email"] == e for x in rec["emails"]):
                    rec["emails"].append({"email": e, "found_on": str(p.url)})
    return rec


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from guard_dog.requests import REQUEST_EMAIL_ORDER, pick_address  # noqa: E402  (one rule, shared with the app)


def request_email(emails):
    return pick_address([e["email"] for e in emails])


def ours():
    """Our own people-search sites as extra leads (the website is their search address's host)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from guard_dog.brokers import BROKERS
    return [{"id": b["id"], "name": b["name"], "website": "https://" + urlsplit(b["search"]("a", "b", "c", "OH")).netloc,
             "opt_out_url": b["optout"], "region": "us", "category": "people-search"} for b in BROKERS]


def verified(rec):
    return bool(rec["emails"]) or any(o.get("status") == 200 and o.get("kind") == "opt-out" for o in rec["optout"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("leads")
    ap.add_argument("lead_commit")
    ap.add_argument("--only", default="")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--pause", type=float, default=1.0)
    a = ap.parse_args()
    mine = ours()
    ids = {x["id"] for x in mine}
    ls = [x for x in leads(a.leads) if x.get("region", "us") in ("us", "") and x["id"] not in ids] + mine
    ls = [x for x in ls if not a.only or x["id"] in a.only.split(",")]
    today = dt.date.today().isoformat()
    done = []
    t0 = time.time()

    def one(lead):
        try:
            r = check(lead, today, lambda: time.sleep(a.pause))
        except Exception as ex:  # one broken site must not end the build; it's left out with the reason
            r = {"id": lead["id"], "name": lead.get("name", ""), "website": lead.get("website", ""), "region": "",
                 "category": lead.get("category", ""), "checked": today, "optout": [], "emails": [],
                 "notes": [f"check failed: {type(ex).__name__}: {str(ex)[:120]}"]}
        done.append(r)
        if len(done) % 25 == 0:
            print(f"{len(done)}/{len(ls)} in {int(time.time() - t0)} s", flush=True)
        return r
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        recs = list(ex.map(one, ls))
    for r in recs:
        r["verified"] = verified(r)
        r["request_email"] = request_email(r["emails"])
        r["ours"] = r["id"] in ids
    # ours stay even when their pages refused us: the scanner already uses them; the flags say what we couldn't check
    keep = sorted((r for r in recs if r["verified"] or r["ours"]), key=lambda r: r["id"])
    failed = [r for r in recs if any(n.startswith("check failed") for n in r["notes"])]
    for r in failed:
        print("check failed:", r["id"], r["notes"][0], flush=True)
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps({"built": today, "leads": f"drumandbytes/eraser@{a.lead_commit} data/brokers.yaml (leads only)",
                               "method": "each entry fetched from the broker's own site; only what those pages show is kept",
                               "request_email_rule": "first address matching, in order: " + " > ".join(REQUEST_EMAIL_ORDER)
                                                     + "; a single address is used as is; several with none matching: none",
                               "brokers": keep}, indent=1) + "\n")
    blocked = sum(1 for r in recs if r.get("blocks_bots"))
    print(f"leads {len(ls)} | kept {len(keep)} (with an email {sum(1 for r in keep if r['emails'])}, with an opt-out page "
          f"{sum(1 for r in keep if any(o.get('status') == 200 and o.get('kind') == 'opt-out' for o in r['optout']))}) | "
          f"left out {len(recs) - len(keep)} (home page refused bots: {blocked}) | {int(time.time() - t0)} s")


if __name__ == "__main__":
    sys.exit(main())

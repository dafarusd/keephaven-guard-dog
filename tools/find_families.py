"""Which people-search sites are one company: read each site's own privacy and terms pages (plain fetch) and record
the company each one names as its operator, with the page and the date. Sites naming the same operator are one
family: one removal request covers them, and a listing on one means the others likely list the person too.

    .venv/bin/python -m tools.find_families      # writes guard_dog/data/families.json
"""
import datetime as dt
import json
import re
import sys
from pathlib import Path
from urllib.parse import urljoin, urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from guard_dog.brokers import BROKERS  # noqa: E402

OUT = ROOT / "guard_dog" / "data" / "families.json"
PAGES = ("/privacy", "/privacy-policy", "/terms", "/terms-of-use", "/tos", "/about", "/legal", "/privacy.html")
COMPANY = re.compile(r"\b((?:[A-Z][\w&'.-]*\s){0,4}[A-Z][\w&'.-]*,?\s(?:LLC|L\.L\.C\.|Inc\.|Inc|Corp\.|Corporation|Ltd\.?|Co\.))")
NOISE = re.compile(r"^(Google|Apple|Microsoft|Amazon|Meta|Facebook|Stripe|PayPal|Cloudflare|Twitter|Experian|Equifax|"
                   r"TransUnion|The|This|Our|Such|Any|Each|All|If|In|By|For|To)\b")


def fetch(url):
    from scrapling.fetchers import Fetcher
    try:
        p = Fetcher.get(url, impersonate="chrome", timeout=25, follow_redirects=True, retries=1)
        return p.status, p
    except Exception as ex:
        return type(ex).__name__, None


def operators(text):
    """Company names a page gives, most frequent first ('operated by X', the © line, '... LLC' mentions)."""
    found = {}
    for m in COMPANY.finditer(" ".join(text.split())):
        name = re.sub(r"\s+", " ", m.group(1)).strip(" ,")
        if NOISE.match(name) or len(name) < 6:
            continue
        key = re.sub(r"[,.]", "", name).lower()
        found.setdefault(key, [name, 0])[1] += 1
    return [v[0] for v in sorted(found.values(), key=lambda v: -v[1])][:3]


def main():
    today = dt.date.today().isoformat()
    sites = []
    for b in BROKERS:
        home = "https://" + urlsplit(b["search"]("a", "b", "c", "OH")).netloc
        rec = {"id": b["id"], "name": b["name"], "home": home, "pages": [], "operators": [], "redirects_to": ""}
        st, p = fetch(b["optout"])
        if p is not None and urlsplit(str(p.url)).netloc.removeprefix("www.") != urlsplit(home).netloc.removeprefix("www."):
            rec["redirects_to"] = urlsplit(str(p.url)).netloc.removeprefix("www.")  # e.g. AnyWho's opt-out is Spokeo's
            rec["pages"].append({"url": b["optout"], "status": st, "landed": str(p.url), "fetched": today})
        for path in PAGES:
            st, p = fetch(urljoin(home, path))
            if p is None or st != 200:
                continue
            ops = operators(p.get_all_text())
            if ops:
                text = " ".join(p.get_all_text().split())
                quote = next((text[max(0, text.find(o) - 60):text.find(o) + len(o) + 20] for o in ops[:1] if o in text), "")
                rec["pages"].append({"url": str(p.url), "status": st, "operators": ops, "quote": quote[:160], "fetched": today})
                rec["operators"] = rec["operators"] or ops
            if len(rec["pages"]) >= 3:
                break
        sites.append(rec)
        print(f"{b['id']:26} {rec['operators'][:2]} {('→ ' + rec['redirects_to']) if rec['redirects_to'] else ''}", flush=True)
    OUT.write_text(json.dumps({"built": today, "method": "each site's own privacy/terms pages, plain fetch",
                               "sites": sites}, indent=1) + "\n")


if __name__ == "__main__":
    main()

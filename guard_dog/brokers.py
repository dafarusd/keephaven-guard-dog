"""People-search sites: how to search one for a person, and how to get off it.

Each entry builds search URLs from a name ("First Last") and a place ("City, ST").
`checked` is the date the search pattern answered with a real results page (plain fetch);
None means the pattern is unconfirmed and a miss there proves nothing.
`blocks_bots` means a plain fetch gets a 403 or a challenge: those need the stealth switch.
"""
from urllib.parse import quote, quote_plus

STATES = {"AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
          "CT": "Connecticut", "DE": "Delaware", "DC": "District-Of-Columbia", "FL": "Florida", "GA": "Georgia",
          "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
          "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts",
          "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
          "NE": "Nebraska", "NV": "Nevada", "NH": "New-Hampshire", "NJ": "New-Jersey", "NM": "New-Mexico",
          "NY": "New-York", "NC": "North-Carolina", "ND": "North-Dakota", "OH": "Ohio", "OK": "Oklahoma",
          "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode-Island", "SC": "South-Carolina", "SD": "South-Dakota",
          "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
          "WV": "West-Virginia", "WI": "Wisconsin", "WY": "Wyoming"}


def split_name(name):
    parts = name.split()
    return (parts[0], parts[-1]) if len(parts) >= 2 else (name, "")


BY_STATE_NAME = {v.replace("-", " ").lower(): k for k, v in STATES.items()}


def split_place(place):
    """(city, ST) from however people type a place: "Columbus, OH", "Columbus OH", "Columbus, Ohio",
    "Columbus Ohio", "New York New York", "OH", "Ohio", or just "Columbus"."""
    words = place.replace(",", " ").split()
    if not words:
        return "", ""
    for n in (3, 2, 1):  # the longest state at the end wins ("West Virginia" before "Virginia")
        if len(words) >= n:
            tail = " ".join(words[-n:])
            st = tail.upper() if n == 1 and tail.upper() in STATES else BY_STATE_NAME.get(tail.lower())
            if st:
                return " ".join(words[:-n]), st
    return " ".join(words), ""


def dash(s):
    return "-".join(s.split())


BROKERS = [
    dict(id="truepeoplesearch", name="TruePeopleSearch", checked="2026-10-29", blocks_bots=False,
         search=lambda f, l, c, s: f"https://www.truepeoplesearch.com/results?name={quote(f + ' ' + l)}"
                                   + (f"&citystatezip={quote(c + ', ' + s)}" if c and s else ""),
         optout="https://www.truepeoplesearch.com/removal",
         how="Find your listing, open it, press Remove This Record, confirm the email they send."),
    dict(id="spokeo", name="Spokeo", checked="2026-10-29", blocks_bots=False,
         search=lambda f, l, c, s: f"https://www.spokeo.com/{dash(f)}-{dash(l)}"
                                   + (f"/{STATES.get(s, s)}" if s else "") + (f"/{dash(c)}" if c and s else ""),
         optout="https://www.spokeo.com/optout",
         how="Copy your listing's URL, paste it into the opt-out form with your removal email, click the link they send."),
    dict(id="cyberbackgroundchecks", name="CyberBackgroundChecks", checked="2026-10-29", blocks_bots=False,
         search=lambda f, l, c, s: f"https://www.cyberbackgroundchecks.com/people/{dash(f).lower()}-{dash(l).lower()}"
                                   + (f"/{s.lower()}/{dash(c).lower()}" if c and s else ""),
         optout="https://www.cyberbackgroundchecks.com/removal",
         how="Search yourself on the removal page, pick your record, confirm by email."),
    dict(id="peoplefinders", name="PeopleFinders", checked="2026-10-29", blocks_bots=False,
         search=lambda f, l, c, s: f"https://www.peoplefinders.com/name/{dash(f).lower()}-{dash(l).lower()}"
                                   + (f"/{s.lower()}/{dash(c).lower()}" if c and s else ""),
         optout="https://www.peoplefinders.com/opt-out",
         how="Use the opt-out form, find your record, confirm by email."),
    dict(id="beenverified", name="BeenVerified", checked="2026-10-29", blocks_bots=False,
         search=lambda f, l, c, s: f"https://www.beenverified.com/people/{dash(f).lower()}-{dash(l).lower()}/"
                                   + (f"{s.lower()}/{dash(c).lower()}/" if c and s else ""),
         optout="https://www.beenverified.com/app/optout/search",
         how="Search your name on the opt-out page, pick your record, confirm by email."),
    dict(id="anywho", name="AnyWho", checked="2026-10-29", blocks_bots=False,
         search=lambda f, l, c, s: f"https://www.anywho.com/people/{quote_plus(f.lower() + ' ' + l.lower())}"
                                   + (f"/{quote_plus(STATES.get(s, s).replace('-', ' ').lower())}/{quote_plus(c.lower())}"
                                      if c and s else ""),
         optout="https://www.spokeo.com/optout",
         how="AnyWho's opt-out goes through Spokeo now: paste your AnyWho listing URL there, confirm by email."),
    dict(id="familytreenow", name="FamilyTreeNow", checked="2026-10-29", blocks_bots=False,
         search=lambda f, l, c, s: f"https://www.familytreenow.com/search/genealogy/results?first={quote_plus(f)}&last={quote_plus(l)}"
                                   + (f"&citystatezip={quote_plus(c + ', ' + s)}" if c and s else ""),
         optout="https://www.familytreenow.com/optout",
         how="Use the opt-out page, find your record, press Opt Out."),
    # Plain fetch is refused: these need the stealth switch on.
    dict(id="fastpeoplesearch", name="FastPeopleSearch", checked=None, blocks_bots=True,
         search=lambda f, l, c, s: f"https://www.fastpeoplesearch.com/name/{dash(f).lower()}-{dash(l).lower()}"
                                   + (f"_{dash(c).lower()}-{s.lower()}" if c and s else ""),
         optout="https://www.fastpeoplesearch.com/removal",
         how="Search yourself on the removal page, open your record, press Remove, confirm by email."),
    dict(id="whitepages", name="Whitepages", checked=None, blocks_bots=True,
         search=lambda f, l, c, s: f"https://www.whitepages.com/name/{dash(f)}-{dash(l)}"
                                   + (f"/{dash(c)}-{s}" if c and s else ""),
         optout="https://www.whitepages.com/suppression-requests",
         how="Paste your listing URL into the suppression form; they confirm by a phone call."),
    dict(id="mylife", name="MyLife", checked=None, blocks_bots=True,
         search=lambda f, l, c, s: f"https://www.mylife.com/{dash(f).lower()}-{dash(l).lower()}/",
         optout="https://www.mylife.com/ccpa/index.pub",
         how="Use the privacy request form (or email privacy@mylife.com) with your listing URL."),
    dict(id="411", name="411.com", checked=None, blocks_bots=True,
         search=lambda f, l, c, s: f"https://www.411.com/name/{dash(f)}-{dash(l)}" + (f"/{dash(c)}-{s}" if c and s else ""),
         optout="https://www.whitepages.com/suppression-requests",
         how="411.com has no removal page of its own; its privacy page names Whitepages, Inc. Try Whitepages' "
             "suppression request; the next scan shows whether 411 still lists you."),
    dict(id="advancedbackgroundchecks", name="AdvancedBackgroundChecks", checked=None, blocks_bots=True,
         search=lambda f, l, c, s: f"https://www.advancedbackgroundchecks.com/names/{dash(f).lower()}-{dash(l).lower()}"
                                   + (f"_{dash(c).lower()}-{s.lower()}" if c and s else ""),
         optout="https://www.advancedbackgroundchecks.com/",
         how="Their old removal page is gone (checked 2026-10-29): use the opt-out or privacy link at the bottom of the site."),
    dict(id="smartbackgroundchecks", name="SmartBackgroundChecks", checked=None, blocks_bots=True,
         search=lambda f, l, c, s: f"https://www.smartbackgroundchecks.com/people/{dash(f).lower()}-{dash(l).lower()}"
                                   + (f"/{dash(c).lower()}/{s.lower()}" if c and s else ""),
         optout="https://www.smartbackgroundchecks.com/remove",
         how="Search yourself on the opt-out page, pick your record, confirm by email."),
    dict(id="peoplesearchnow", name="PeopleSearchNow", checked=None, blocks_bots=True,
         search=lambda f, l, c, s: f"https://www.peoplesearchnow.com/person/{dash(f).lower()}-{dash(l).lower()}"
                                   + (f"_{dash(c).lower()}_{s.lower()}" if c and s else ""),
         optout="https://www.peoplesearchnow.com/opt-out",
         how="Search yourself on the opt-out page, pick your record, confirm by email."),
    dict(id="searchpeoplefree", name="SearchPeopleFree", checked=None, blocks_bots=True,
         search=lambda f, l, c, s: f"https://www.searchpeoplefree.com/find/{dash(f).lower()}-{dash(l).lower()}"
                                   + (f"/{s.lower()}/{dash(c).lower()}" if c and s else ""),
         optout="https://www.searchpeoplefree.com/opt-out",
         how="Search yourself on the opt-out page, pick your record, confirm by email."),
    # Answer a plain fetch but build the results in the browser: stealth (a real browser) reads them.
    dict(id="radaris", name="Radaris", checked=None, blocks_bots=True,
         search=lambda f, l, c, s: f"https://radaris.com/p/{dash(f)}/{dash(l)}/",
         optout="https://radaris.com/control/privacy",
         how="Open your profile, choose Control info, verify by email or phone, then remove."),
    dict(id="peekyou", name="PeekYou", checked=None, blocks_bots=True,
         search=lambda f, l, c, s: f"https://www.peekyou.com/{f.lower()}_{l.lower()}",
         optout="https://www.peekyou.com/about/contact/optout/",
         how="Fill the opt-out form with your profile URL, confirm by email."),
]


def search_urls(broker, person):
    """(url, name, place) for every search of a person on one site: each name in each place."""
    out, seen = [], set()
    places = person["places"] or [""]
    for name in person["names"]:
        f, l = split_name(name)
        if not l:
            continue
        for place in places:
            c, s = split_place(place) if place else ("", "")
            u = broker["search"](f, l, c, s)
            if u not in seen:
                seen.add(u)
                out.append((u, name, place))
    return out


def _families():
    import json
    from pathlib import Path
    return json.loads((Path(__file__).parent / "data" / "families.json").read_text())["families"]


FAMILIES = _families()
FAMILY_OF = {m: f for f in FAMILIES for m in f["members"]}


def sister_note(broker_id, sister_name, you="you"):
    """Why a site that couldn't be checked likely lists the person: worded by how strong the family evidence is."""
    f = FAMILY_OF.get(broker_id)
    if f and f.get("evidence_strength") == "named":
        return f"The same company runs {sister_name}, which lists {you}; this site couldn't be checked this time."
    return (f"This site appears to be run by the same company as {sister_name}, which lists {you} (the sites share the "
            "same terms of service); it couldn't be checked this time.")


def family_of(broker_id):
    """The family a site belongs to (sites run by one operator, from their own pages), or None."""
    return FAMILY_OF.get(broker_id)


def covered_by(broker_id):
    """The family member whose removal also removes this site (its own opt-out page sends you there), or None."""
    f = FAMILY_OF.get(broker_id)
    main = f and f.get("one_removal")
    return main if main and main != broker_id else None

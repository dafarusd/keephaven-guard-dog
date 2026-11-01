"""Removal-request letters, sent by email from the removal inbox.

The structure (a deletion + opt-out letter per broker, a reference tag in the subject so replies can be matched,
a California version citing the statute, a GDPR version) follows Scrubbr (github.com/jamessk22/Scrubbr, MIT).
The wording is ours, approved by Dafarus on 2026-10-29. Unlike Scrubbr, a letter carries only what the broker
needs to find the record and already holds: the name, the listing address and the city it shows. Never a birth
date, phone number, street address or relative.
"""
import re

FOLLOW_UP_DAYS = {"ccpa": 45, "gdpr": 30, "generic": 60}

GENERIC = """Hello {broker} privacy team,

I'm asking you to delete all personal information you hold about me, and to stop selling, sharing or licensing it. \
Please pass this request on to anyone you've shared my information with.

If the law where I live gives me a right to have my data deleted or to opt out of its sale, I'm using that right \
with this request. If it doesn't, please delete it anyway.

To find my records:
{identity}

Please reply to this email once it's done. I won't create an account for this. If you need anything else to find \
my records, just ask.

Thank you,
{name}
"""

CCPA = """Hello {broker} privacy team,

I live in California and I'm making this request under the California Consumer Privacy Act, for myself.

Please:
1. Delete all personal information you hold about me (Cal. Civ. Code § 1798.105).
2. Stop selling or sharing my personal information (§ 1798.120).
3. Pass this request on to the service providers and third parties you've shared it with.

To find my records:
{identity}

Under § 1798.130 you have 45 days to respond. I won't create an account for this. If you need anything else to \
find my records, just ask.

Thank you,
{name}
"""

GDPR = """Hello {broker} privacy team,

I'm asking you to erase all personal data you hold about me under Article 17 of the GDPR (the UK GDPR, if \
you're in the UK), and I object to any further processing of it for marketing or profiling under Article 21.

Please also tell everyone you've disclosed my data to, as Article 19 requires.

To find my records:
{identity}

Under Article 12(3) you have one month to respond. I won't create an account for this. If you need anything else \
to find my records, just ask.

Thank you,
{name}
"""

SUBJECTS = {"generic": "Request to delete my personal information",
            "ccpa": "CCPA request to delete my personal information",
            "gdpr": "Request to erase my personal data (GDPR Article 17)"}
BODIES = {"generic": GENERIC, "ccpa": CCPA, "gdpr": GDPR}
GDPR_COUNTRIES = {"AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT", "LV", "LT",
                  "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE", "IS", "LI", "NO", "GB", "UK"}


def kind_for(person, broker=None):
    """Which letter, from where the person lives NOW (their own setting, not the cities they've lived in: someone
    who once lived in California can't claim California law): California → CCPA; EU/EEA/UK → GDPR; else general."""
    country = (person.get("home_country") or "US").upper()
    if country in GDPR_COUNTRIES:
        return "gdpr"
    if country == "US" and (person.get("home_state") or "").upper() == "CA":
        return "ccpa"
    return "generic"


def shown_place(matched):
    """The city and state a listing showed, as the matcher recorded it ('city: Columbus, OH')."""
    m = re.search(r"city: ([^;]+)", matched or "")
    return m.group(1).strip() if m else ""


def write(person, broker, removal_email, request_id, finding=None):
    """(subject, body) for one broker. `finding` is that broker's listing of the person, if the scan found one."""
    kind = kind_for(person, broker)
    name = person["names"][0]
    lines = [f"Name: {name}"]
    if finding and finding.get("status") in ("listed", "possible"):
        lines.append(f"Where you list me: {finding['url']}")
        place = shown_place(finding.get("matched"))
        if place:
            lines.append(("City and country: " if kind == "gdpr" else "City and state: ") + place)
    lines.append(f"Email for this request: {removal_email}")
    body = BODIES[kind].format(broker=broker["name"], identity="\n".join(lines), name=name)
    return f"{SUBJECTS[kind]} [GD-{request_id}]", body, kind

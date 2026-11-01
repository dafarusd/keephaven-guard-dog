"""The broker-list builder keeps only what a broker's own pages show — no network (fetch is faked)."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scrapling.parser import Selector

from tools import build_brokers as bb

YAML = """brokers:
    - id: acme
      name: Acme People
      email: privacy@acme.example
      website: https://www.acme.example
      opt_out_url: https://www.acme.example/optout
      region: us
      category: people-search
      notes: copied text we must not keep
    - id: ghost
      name: Ghost
      email: ""
      website: ""
      region: us
"""


class Page:
    def __init__(self, url, html, status=200):
        self.url, self.status, self.sel = url, status, Selector(html)

    def css(self, q):
        return self.sel.css(q)

    def get_all_text(self):
        return self.sel.get_all_text()


SITE = {
    "https://www.acme.example": Page("https://www.acme.example/", '<a href="/privacy">Privacy policy</a><a href="https://evil.example/optout">x</a>'),
    "https://www.acme.example/optout": Page("https://www.acme.example/optout", "<title>Opt out</title>Remove your listing"),
    "https://www.acme.example/privacy": Page("https://www.acme.example/privacy",
                                             '<title>Privacy</title>Write to privacy@acme.example or <a href="mailto:dpo@acme.example?subject=x">us</a>. '
                                             'Our vendor is help@other.example. <a href="mailto:?subject=share">share</a>'),
}


class Builder(unittest.TestCase):
    def test_leads_parse_only_the_fields_we_use(self):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(YAML)
        ls = bb.leads(f.name)
        self.assertEqual([x["id"] for x in ls], ["acme", "ghost"])
        self.assertNotIn("notes", ls[0])
        Path(f.name).unlink()

    def test_only_what_their_own_pages_show(self):
        with mock.patch.object(bb, "fetch", lambda u: (SITE[u].status, SITE[u]) if u in SITE else (404, None)):
            rec = bb.check(bb.leads_from_text(YAML)[0], "2026-10-29", lambda: None)
        self.assertEqual([e["email"] for e in rec["emails"]], ["dpo@acme.example", "privacy@acme.example"])
        self.assertTrue(all(e["found_on"].endswith("/privacy") for e in rec["emails"]))
        self.assertEqual({o["url"]: o["kind"] for o in rec["optout"]},
                         {"https://www.acme.example/optout": "opt-out", "https://www.acme.example/privacy": "privacy"})
        self.assertTrue(bb.verified(rec))

    def test_request_email_rule(self):
        mk = lambda *a: [{"email": x} for x in a]
        self.assertEqual(bb.request_email(mk("customercare@s.com", "expungement@s.com", "privacy@s.com")), "privacy@s.com")
        self.assertEqual(bb.request_email(mk("support@s.com", "dpo@s.com")), "dpo@s.com")
        self.assertEqual(bb.request_email(mk("help@s.com", "ccpa-requests@s.com", "legal@s.com")), "ccpa-requests@s.com")
        self.assertEqual(bb.request_email(mk("info@s.com", "legal@s.com")), "legal@s.com")
        self.assertEqual(bb.request_email(mk("info@s.com")), "info@s.com")
        self.assertEqual(bb.request_email(mk("info@s.com", "support@s.com")), "")
        self.assertEqual(bb.request_email([]), "")

    def test_our_sites_become_leads(self):
        mine = bb.ours()
        self.assertEqual(len(mine), 17)
        self.assertTrue(all(m["website"].startswith("https://") for m in mine))
        self.assertIn("https://www.peoplefinders.com", {m["website"] for m in mine})

    def test_unverifiable_leads_stay_out(self):
        rec = bb.check(bb.leads_from_text(YAML)[1], "2026-10-29", lambda: None)
        self.assertFalse(bb.verified(rec))
        with mock.patch.object(bb, "fetch", return_value=(403, None)):
            blocked = bb.check(bb.leads_from_text(YAML)[0], "2026-10-29", lambda: None)
        self.assertTrue(blocked.get("blocks_bots"))


if __name__ == "__main__":
    unittest.main()

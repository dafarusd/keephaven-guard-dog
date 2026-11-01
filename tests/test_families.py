"""Broker families: sites one operator runs, recorded from their own pages."""
import tempfile
import threading
import time
import unittest
from collections import Counter
from pathlib import Path

from playwright.sync_api import sync_playwright

from guard_dog import brokers, requests, scanner, web
from guard_dog.db import DB


class Data(unittest.TestCase):
    def test_every_family_has_evidence_and_known_members(self):
        known = {b["id"] for b in brokers.BROKERS} | {b["id"] for b in requests.brokers()}
        import json
        data = json.loads((Path(brokers.__file__).parent / "data" / "families.json").read_text())
        for n in data["not_used"]:
            self.assertTrue(n["id"] and n["reason"] and n["url"] and n["fetched"] and n["quote"])
        for f in brokers.FAMILIES:
            self.assertIn(f["evidence_strength"], ("named", "inferred"), f["id"])
            self.assertTrue(f["evidence"], f["id"])
            self.assertTrue(all(e["url"] and e["fetched"] and e["quote"] for e in f["evidence"]), f["id"])
            self.assertGreaterEqual(len(f["members"]), 2, f["id"])
            self.assertTrue(set(f["members"]) & known, f["id"])
            if f["one_removal"]:
                self.assertIn(f["one_removal"], f["members"])

    def test_lookups(self):
        self.assertEqual(brokers.family_of("fastpeoplesearch")["id"], "truepeoplesearch")
        self.assertIsNone(brokers.family_of("mylife"))
        self.assertEqual(brokers.covered_by("anywho"), "spokeo")
        self.assertIsNone(brokers.covered_by("411"))  # nothing on 411's own pages says Whitepages' removal covers it
        self.assertIsNone(brokers.covered_by("spokeo"))
        self.assertIsNone(brokers.covered_by("fastpeoplesearch"))  # sister sites, but each has its own removal page

    def test_sister_wording_follows_the_evidence(self):
        self.assertEqual(brokers.sister_note("peoplelooker", "BeenVerified"),
                         "The same company runs BeenVerified, which lists you; this site couldn't be checked this time.")
        self.assertTrue(brokers.sister_note("fastpeoplesearch", "TruePeopleSearch").startswith(
            "This site appears to be run by the same company as TruePeopleSearch"))

    def test_summary_counts_companies(self):
        p = {"label": "Me", "names": ["J D"], "last_scan": time.time()}
        s = web.summary(p, Counter({"listed": 4}), 0, False, web.who(p),
                        ["truepeoplesearch", "fastpeoplesearch", "spokeo", "anywho"])
        self.assertEqual(s, "We found you on 4 sites that sell personal details (run by 2 companies).")
        s = web.summary(p, Counter({"listed": 2}), 0, False, web.who(p), ["spokeo", "mylife"])
        self.assertEqual(s, "We found you on 2 sites that sell personal details.")


class Emails(unittest.TestCase):
    def test_one_email_per_address(self):
        with tempfile.TemporaryDirectory() as d:
            db = DB(Path(d))
            pid = db.save_person(None, "Me", "", names=["Jane Doe"])
            fake = [{"id": "a", "name": "A", "category": "people-search", "request_email": "privacy@same.example", "region": "us"},
                    {"id": "b", "name": "B", "category": "people-search", "request_email": "privacy@same.example", "region": "us"},
                    {"id": "c", "name": "C", "category": "people-search", "request_email": "privacy@c.example", "region": "us"}]
            from unittest import mock
            with mock.patch.object(requests, "brokers", return_value=fake):
                got = requests.plan(db, pid)
            self.assertEqual(sorted(r["to_addr"] for r in got), ["privacy@c.example", "privacy@same.example"])


class Page(unittest.TestCase):
    def test_likely_listing_and_covered_removal_on_the_page(self):
        with tempfile.TemporaryDirectory() as d:
            db = DB(Path(d))
            pid = db.save_person(None, "Me", "", names=["Jane Doe"])
            db.x("UPDATE people SET last_scan=?", (time.time(),))
            rec = lambda src, st: scanner.record(db, pid, {"id": src, "name": src}, f"https://{src}.example/x", st,
                                                 ["name: Jane Doe", "age 52", "city: Columbus OH"], None)
            rec("truepeoplesearch", "listed")
            rec("fastpeoplesearch", "needs stealth")
            rec("spokeo", "listed")
            rec("anywho", "listed")
            httpd, _, _ = web.build(db, "127.0.0.1", 0)
            port = httpd.server_address[1]
            httpd.server_close()
            httpd, _, _ = web.build(db, "127.0.0.1", port)
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            try:
                with sync_playwright() as pw:
                    b = pw.chromium.launch(channel="chromium")  # the full browser, as the app uses (no headless shell needed)
                    pg = b.new_page()
                    pg.goto(f"http://127.0.0.1:{port}/person/1")
                    t = pg.inner_text("main")
                    b.close()
            finally:
                httpd.shutdown()
                httpd.server_close()
        self.assertIn("We found you on 3 sites that sell personal details (run by 2 companies)", t)
        self.assertIn("Likely listing you", t)
        self.assertIn("This site appears to be run by the same company as TruePeopleSearch, which lists you "
                      "(the sites share the same terms of service)", t)
        self.assertNotIn("The same company runs TruePeopleSearch", t)
        self.assertIn("Removing you from Spokeo also removes you here", t)
        anywho = t[t.index("AnyWho"):] if "AnyWho" in t else t[t.index("anywho"):]
        self.assertNotIn("Remove me", anywho.split("Details")[0])


if __name__ == "__main__":
    unittest.main()

"""The dashboard at phone width (390 px) in real Chromium: no sideways scrolling, plain status words, a summary
sentence that is true in every state, and counts that match the database."""
import os
import re
import tempfile
import threading
import time
import unittest
from collections import Counter
from pathlib import Path

from playwright.sync_api import sync_playwright

from guard_dog import removal, scanner, web
from guard_dog.db import DB

WORDS = ("Listing you", "Maybe you", "Not checked yet", "Removal requested", "Removed", "Needs you")
HIDDEN = ("possible", "unconfirmed", "needs stealth", "blocked", "http 4", "http 5", "sweep")


class Phone(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.chrome = cls.pw.chromium.launch(channel="chromium")  # the full browser, as the app uses (no headless shell needed)

    @classmethod
    def tearDownClass(cls):
        cls.chrome.close()
        cls.pw.stop()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.tmp.name))
        pid = self.db.save_person(None, "Me", "", names=["Jane Doe"], places=["Columbus OH"], emails=["j@example.com"])
        self.db.x("UPDATE people SET last_scan=?", (time.time() - 600,))
        rec = lambda src, st, m=(): scanner.record(self.db, pid, {"id": src, "name": src.title()}, f"https://{src}.example/x", st, list(m), None)
        rec("spokeo", "listed", ["name: Jane Doe", "city: Columbus OH", "age 52", "relatives"])
        rec("whitepages", "possible", ["name: Jane Doe", "city: Columbus OH"])
        rec("mylife", "needs stealth")
        rec("anywho", "not found")
        for src, path in (("peoplefinders", ["ready", "requested"]), ("radaris", ["ready", "requested", "gone"]),
                          ("peekyou", ["manual"])):
            rec(src, "listed", ["name: Jane Doe", "age 52", "city: Columbus OH"])
            fid = self.db.q("SELECT id FROM findings WHERE source=?", (src,))[0]["id"]
            for s in path:
                removal.move(self.db, fid, s, note="Peekyou wants a phone call" if s == "manual" else "")
        self.db.x("INSERT INTO breaches VALUES(1,'j@example.com','Big','big.example','2024','Emails, Passwords','plaintext',1)")
        self.db.alert(pid, "web search: brave failed 1 of 6 searches (http 429); the sweep missed what it would have found.")
        port = self.free_port()
        self.httpd, _, _ = web.build(self.db, "127.0.0.1", port)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{port}"
        self.ctx = self.chrome.new_context(viewport={"width": 390, "height": 844})
        self.page = self.ctx.new_page()

    def tearDown(self):
        self.ctx.close()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    @staticmethod
    def free_port():
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        p = s.getsockname()[1]
        s.close()
        return p

    def visible_text(self):
        """What a person reads without opening any Details."""
        return self.page.evaluate("""() => { const c = document.body.cloneNode(true);
            c.querySelectorAll('details').forEach(d => { const s = d.querySelector('summary');
                if (s && /Details/.test(s.textContent)) d.remove(); });
            return c.innerText; }""")

    def test_no_sideways_scrolling(self):
        for path in ("/", "/person/1", "/settings", "/person/1/edit", "/password"):
            self.page.goto(self.base + path)
            sw, iw = self.page.evaluate("[document.documentElement.scrollWidth, window.innerWidth]")
            self.assertLessEqual(sw, iw, path)

    def test_plain_words_only_and_counts_match(self):
        self.page.goto(self.base + "/person/1")
        for d in self.page.query_selector_all("details"):  # open the sections, not the Details
            if "Details" not in d.query_selector("summary").inner_text():
                d.evaluate("d => d.open = true")
        text = self.visible_text()
        for w in WORDS:
            self.assertIn(w, text)
        for h in HIDDEN:
            self.assertNotIn(h, text.lower(), h)
        self.assertNotIn("turned away", text)  # search-engine trouble isn't news: it sits under Not checked yet > Details
        self.assertIn("Brave turned away 1 of 6 web searches", self.page.content())
        listed = self.db.q("""SELECT COUNT(*) n FROM findings f WHERE status='listed' AND NOT EXISTS
            (SELECT 1 FROM removals r WHERE r.finding=f.id AND r.state IN ('requested','confirmed','gone','manual'))""")[0]["n"]
        self.assertEqual(listed, 1)
        self.assertEqual(text.count("Listing you"), listed)
        c = Counter(r["status"] for r in self.db.q("SELECT status FROM findings"))
        self.assertIn(f"We found you on {c['listed']} sites that sell personal details, and {c['possible']} more", text)
        self.assertIn("Your email was in 1 data breach", text)
        self.assertIn("change it at big.example", text)

    def test_home_at_phone_width(self):
        self.page.goto(self.base + "/")
        self.assertIn("We found you on", self.page.inner_text("main"))
        self.assertEqual(self.page.locator("a.btn:has-text('Open')").count(), 1)

    def test_settings_switch_reads_plainly(self):
        self.page.goto(self.base + "/settings")
        text = self.page.inner_text("main")
        self.assertIn("Get past bot walls (uses a hidden browser; some sites' terms forbid it)", text)
        self.assertIn('shown as "Not checked yet"', text)
        self.assertNotIn("needs stealth", text)


class Summary(unittest.TestCase):
    """The sentence at the top, in every state."""

    def s(self, c, nb=0, running=False, scanned=True, label="Me", stealth=False):
        p = {"label": label, "names": ["Jane Doe"], "last_scan": time.time() if scanned else None}
        return web.summary(p, Counter(c), nb, running, web.who(p), stealth=stealth)

    def test_every_state(self):
        self.assertEqual(self.s({}, scanned=False), "Not checked yet. Press Scan now to look for you.")
        self.assertEqual(self.s({}, running=True), "Checking sites for you now. Results appear as they come in.")
        self.assertEqual(self.s({"listed": 1}), "We found you on 1 site that sells personal details.")
        self.assertEqual(self.s({"listed": 3, "possible": 2}, nb=4),
                         "We found you on 3 sites that sell personal details, and 2 more that might be you. "
                         "Your email was in 4 data breaches.")
        self.assertEqual(self.s({"possible": 1}), "1 site might list you; check them below.")
        self.assertEqual(self.s({"needs stealth": 10, "unconfirmed": 4}),
                         "We couldn't check 14 sites yet — scan again with 'get past bot walls' on in Settings.")
        self.assertEqual(self.s({"not found": 17}, nb=1), "No site we checked lists you. Your email was in 1 data breach.")
        self.assertEqual(self.s({"listed": 2}, label="Mom"), "We found Mom on 2 sites that sell personal details.")

    def test_with_stealth_on_it_never_asks_for_stealth(self):
        """VM gate 2026-10-31: stealth on, and the headline still said to turn it on."""
        self.assertEqual(self.s({"not found": 7, "not here": 7}, stealth=True),
                         "No site we checked lists you. 7 sites can't be checked in this version.")
        self.assertEqual(self.s({"not found": 7, "blocked": 2}, stealth=True),
                         "No site we checked lists you. 2 sites blocked the check; the next scan tries them again.")
        self.assertEqual(self.s({"not found": 7, "unconfirmed": 1}, stealth=True),
                         "No site we checked lists you. 1 site gave no clear answer; the next scan tries again.")
        self.assertEqual(self.s({"not here": 1, "blocked": 1}, stealth=True),
                         "1 site can't be checked in this version. 1 site blocked the check; the next scan tries it again.")
        self.assertNotIn("bot walls", self.s({"not found": 7, "not here": 7, "blocked": 2, "unconfirmed": 1}, stealth=True))
        self.assertIn("'get past bot walls' on", self.s({"not found": 7, "needs stealth": 2}))  # off: the advice stays

    def test_tally_sets_unproven_search_pages_apart(self):
        unproven = sorted(web.SEARCH_UNPROVEN)[0]
        proven = next(b["id"] for b in web.BROKERS if b.get("checked"))
        c = web.tally([{"source": unproven, "status": "unconfirmed", "check_note": ""},
                       {"source": proven, "status": "unconfirmed", "check_note": ""},
                       {"source": proven, "status": "listed", "check_note": scanner.UNSEEN}])
        self.assertEqual(c, Counter({"not here": 1, "unconfirmed": 1, "not seen": 1}))

    def test_who_and_shows(self):
        self.assertEqual(web.who({"label": "Me", "names": ["A B"]})["your"], "your")
        self.assertEqual(web.who({"label": "", "names": ["Ann Lee"]})["you"], "Ann Lee")
        self.assertEqual(web.shows("name: X; city: Columbus; age 52; relatives; street address"),
                         "home address, age, city and relatives")
        self.assertEqual(web.shows("name: X"), "")
        self.assertEqual(web.plain_alert("people-search check failed (Spokeo): Timeout"),
                         "Couldn't check Spokeo this time; it's tried again on the next scan.")
        self.assertEqual(web.plain_alert("Spokeo: new listing found."), "Spokeo: new listing found.")


if __name__ == "__main__":
    unittest.main()


class Times(unittest.TestCase):
    def test_when_reads_like_a_person(self):
        now = time.time()
        self.assertEqual(web.when(None), "never")
        self.assertTrue(web.when(now).startswith("today "))
        self.assertTrue(web.when(now - 86400).startswith("yesterday "))
        self.assertTrue(web.when(now + 86400).startswith("tomorrow "))
        self.assertRegex(web.when(now - 10 * 86400), r"^[A-Z][a-z]{2} \d{1,2}$")


class ClockChange(unittest.TestCase):
    """The day the clocks change is 23 or 25 hours long; 'tomorrow' and 'yesterday' count calendar days."""

    def setUp(self):
        old = os.environ.get("TZ")
        os.environ["TZ"] = "America/New_York"
        time.tzset()

        def restore():
            if old is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old
            time.tzset()
        self.addCleanup(restore)

    @staticmethod
    def at(y, m, d, h):
        return time.mktime((y, m, d, h, 0, 0, 0, 0, -1))

    def test_fall_back(self):
        self.assertTrue(web.when(self.at(2026, 11, 2, 9), now=self.at(2026, 11, 1, 12)).startswith("tomorrow "))
        self.assertTrue(web.when(self.at(2026, 11, 1, 9), now=self.at(2026, 11, 2, 12)).startswith("yesterday "))

    def test_spring_forward(self):
        self.assertTrue(web.when(self.at(2026, 3, 9, 9), now=self.at(2026, 3, 8, 12)).startswith("tomorrow "))
        self.assertTrue(web.when(self.at(2026, 3, 8, 9), now=self.at(2026, 3, 9, 12)).startswith("yesterday "))

    def test_a_plain_day(self):
        now = self.at(2026, 10, 14, 12)
        self.assertEqual(web.when(self.at(2026, 10, 14, 9), now=now), "today 9:00 am")
        self.assertEqual(web.when(self.at(2026, 10, 15, 9), now=now), "tomorrow 9:00 am")
        self.assertEqual(web.when(self.at(2026, 10, 13, 21), now=now), "yesterday 9:00 pm")
        self.assertEqual(web.when(self.at(2026, 10, 4, 9), now=now), "Oct 4")

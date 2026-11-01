"""Removal states, what may be typed, evidence, and the form runner against a local fake opt-out form in real
Chromium. Nothing here touches a real site."""
import json
import tempfile
import threading
import unittest
from pathlib import Path

from playwright.sync_api import sync_playwright

from guard_dog import removal, scanner, web
from guard_dog.db import DB
from guard_dog.removal import BadMove


class States(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_whole_path_and_history(self):
        for to in ("ready", "requested", "confirmed", "gone", "back", "ready"):
            removal.move(self.db, 7, to, note=to)
        hist = json.loads(self.db.q("SELECT history FROM removals WHERE finding=7")[0]["history"])
        self.assertEqual([h["to"] for h in hist], ["ready", "requested", "confirmed", "gone", "back", "ready"])
        self.assertEqual(hist[0]["from"], "found")

    def test_refused_moves(self):
        with self.assertRaises(BadMove):
            removal.move(self.db, 1, "requested")  # submit without a dry run
        with self.assertRaises(BadMove):
            removal.move(self.db, 1, "gone")  # gone is only proven by a re-scan after a request
        with self.assertRaises(BadMove):
            removal.move(self.db, 1, "nonsense")
        removal.move(self.db, 1, "ready")
        removal.move(self.db, 1, "requested")
        with self.assertRaises(BadMove):
            removal.move(self.db, 1, "requested")  # no re-submit unless the owner asks
        removal.move(self.db, 1, "requested", force=True)
        self.assertEqual(removal.state(self.db, 1), "requested")

    def test_evidence_kept_when_a_later_move_has_none(self):
        removal.move(self.db, 2, "ready", evidence="/x/1")
        removal.move(self.db, 2, "requested")
        self.assertEqual(self.db.q("SELECT evidence FROM removals WHERE finding=2")[0]["evidence"], "/x/1")

    def test_deleting_a_person_removes_their_removals(self):
        pid = self.db.save_person(None, "T", "", names=["Jane Doe"])
        scanner.record(self.db, pid, {"id": "s", "name": "S"}, "https://s/1", "listed", ["name: Jane Doe"], None)
        fid = self.db.q("SELECT id FROM findings")[0]["id"]
        removal.move(self.db, fid, "ready")
        self.db.delete_person(pid)
        self.assertEqual(self.db.q("SELECT COUNT(*) n FROM removals")[0]["n"], 0)


class Script(removal.OptOut):
    site = "fake"
    fields = ("listing_url", "removal_email")
    confirm = "email"
    domains = ("fake.example",)

    def fill(self, page, data, pick, ev):
        page.fill("#url", data["listing_url"])
        page.fill("#email", data["removal_email"])

    def press(self, page, ev):
        page.click("#go")
        page.wait_for_selector("text=Request received")


RESULTS = b"""<html><body><h1>Find your record</h1>
<div class=card><b>Jane Doe</b>, 61, Miami FL. Relatives: Bob Smith <a href="/remove?id=111">Remove</a></div>
<div class=card><b>Jane Doe</b>, 52, Columbus OH. Relatives: John Doe <a href="/remove?id=222">Remove</a></div>
</body></html>"""
REMOVE = b"""<html><body><form method=post action=/done><input type=hidden name=record value="RID">
<input id=email name=email><button id=go>Remove</button></form></body></html>"""


class PickScript(removal.OptOut):
    """Like a real people-search opt-out: search results with several namesakes, pick theirs, then confirm."""
    site = "fake"
    fields = ("removal_email",)
    confirm = "email"

    def fill(self, page, data, pick, ev):
        for card in page.query_selector_all(".card"):
            text = card.inner_text()
            if any(c in text for c in pick["cities"]) or any(r in text for r in pick["relatives"]):
                card.query_selector("a").click()
                break
        else:
            raise LookupError("their record isn't on this page")
        page.wait_for_selector("#email")
        page.fill("#email", data["removal_email"])

    def press(self, page, ev):
        page.click("#go")
        page.wait_for_selector("text=Request received")


FORM = b"""<html><body><h1>Opt out</h1><form id=f action=/done method=post>
<input id=url name=url><input id=email name=email><input id=phone name=phone>
<button id=go>Remove my listing</button></form></body></html>"""


class Runner(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.posts = []
        posts = cls.posts

        class Fake(web.http.server.BaseHTTPRequestHandler):
            def do_GET(s):
                s.send_response(200)
                s.send_header("Content-Type", "text/html")
                s.end_headers()
                if s.path.startswith("/results"):
                    s.wfile.write(RESULTS)
                elif s.path.startswith("/remove"):
                    s.wfile.write(REMOVE.replace(b"RID", s.path.split("=")[1].encode()))
                else:
                    s.wfile.write(FORM)

            def do_POST(s):
                posts.append(s.rfile.read(int(s.headers["Content-Length"])).decode())
                s.send_response(200)
                s.send_header("Content-Type", "text/html")
                s.end_headers()
                s.wfile.write(b"<p>Request received</p>")

            def log_message(s, *a):
                pass
        cls.srv = web.Server(("127.0.0.1", 0), Fake)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start()
        cls.chrome = cls.pw.chromium.launch(channel="chromium")  # the full browser, as the app uses (no headless shell needed)

    @classmethod
    def tearDownClass(cls):
        cls.chrome.close()
        cls.pw.stop()
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.tmp.name))
        self.db.set("removal_email", "removals@example.com")
        pid = self.db.save_person(None, "T", "", names=["Jane Doe"], phones=["330-555-0142"], relatives=["John Doe"],
                                  places=["Columbus Ohio"], birth_year=1974)
        scanner.record(self.db, pid, {"id": "fake", "name": "Fake"}, "https://fake.example/jane-doe", "listed",
                       ["name: Jane Doe", "phone: 330-555-0142"], None)
        self.row = dict(self.db.q("SELECT * FROM findings")[0])
        self.script = Script()
        self.script.start_url = f"http://127.0.0.1:{self.srv.server_address[1]}/optout"
        self.ctx = self.chrome.new_context()
        Runner.posts.clear()

    def tearDown(self):
        self.ctx.close()
        self.tmp.cleanup()

    def open_page(self, url, steps):
        p = self.ctx.new_page()
        p.goto(url)
        steps(p)

    def test_dry_run_fills_but_never_submits(self):
        r = removal.run(self.db, self.row, self.script, "dry_run", self.open_page)
        self.assertEqual(r["state"], "ready")
        self.assertEqual(r["typed"], ["listing_url", "removal_email"])
        self.assertEqual(self.posts, [])
        ev = Path(self.db.q("SELECT evidence FROM removals")[0]["evidence"])
        self.assertTrue((ev / "steps.log").is_file())
        self.assertEqual(len(list(ev.glob("*.png"))), 2)  # opened, filled
        self.assertEqual(json.loads((ev / "result.json").read_text())["state"], "ready")

    def test_submit_after_dry_run_sends_only_the_declared_fields(self):
        removal.run(self.db, self.row, self.script, "dry_run", self.open_page)
        r = removal.run(self.db, self.row, self.script, "submit", self.open_page)
        self.assertEqual(r["state"], "requested")
        self.assertEqual(len(self.posts), 1)
        sent = self.posts[0]
        self.assertIn("removals%40example.com", sent)
        self.assertIn("jane-doe", sent)
        self.assertIn("phone=&", sent + "&")  # the phone box on the form stays empty
        self.assertNotIn("555", sent)
        self.assertNotIn("John", sent)

    def test_submit_without_a_dry_run_is_refused(self):
        with self.assertRaises(BadMove):
            removal.run(self.db, self.row, self.script, "submit", self.open_page)
        self.assertEqual(self.posts, [])

    def test_a_script_cannot_ask_for_more_than_the_typeable_fields(self):
        greedy = Script()
        greedy.fields = ("listing_url", "removal_email", "phone", "relatives")
        self.assertEqual(sorted(removal.what_to_type(self.db, self.row, greedy)), ["listing_url", "removal_email"])

    def test_name_as_shown_comes_from_the_listing(self):
        named = Script()
        named.fields = ("name_as_shown",)
        self.assertEqual(removal.what_to_type(self.db, self.row, named), {"name_as_shown": "Jane Doe"})

    def test_no_removal_email_no_run(self):
        self.db.set("removal_email", "")
        with self.assertRaises(BadMove):
            removal.run(self.db, self.row, self.script, "dry_run", self.open_page)

    def test_phone_or_captcha_sites_go_manual(self):
        phone = Script()
        phone.confirm = "phone"
        self.assertEqual(removal.run(self.db, self.row, phone, "dry_run", self.open_page), {"state": "manual"})
        self.assertEqual(removal.state(self.db, self.row["id"]), "manual")

    def test_a_broken_form_is_failed_with_evidence(self):
        broken = Script()
        broken.start_url = self.script.start_url
        broken.fill = lambda page, data, pick, ev: page.fill("#nothing-here", "x", timeout=500)
        r = removal.run(self.db, self.row, broken, "dry_run", self.open_page)
        self.assertEqual(r["state"], "failed")
        ev = Path(self.db.q("SELECT evidence FROM removals")[0]["evidence"])
        self.assertIn("stopped:", (ev / "steps.log").read_text())

    def test_their_record_is_picked_among_namesakes_and_nothing_identifying_is_sent(self):
        script = PickScript()
        script.start_url = f"http://127.0.0.1:{self.srv.server_address[1]}/results"
        self.assertEqual(removal.run(self.db, self.row, script, "dry_run", self.open_page)["state"], "ready")
        self.assertEqual(self.posts, [])
        r = removal.run(self.db, self.row, script, "submit", self.open_page)
        self.assertEqual((r["state"], r["typed"]), ("requested", ["removal_email"]))
        self.assertEqual(len(self.posts), 1)
        sent = self.posts[0]
        self.assertIn("record=222", sent)  # the Columbus card, not the Miami namesake
        for secret in ("Columbus", "John", "1974", "555", "Jane"):
            self.assertNotIn(secret, sent)

    def test_pick_is_read_only_and_never_typeable(self):
        pick = removal.pick_for(self.db, self.row)
        self.assertEqual((pick["cities"], pick["relatives"], pick["birth_year"]), (("Columbus",), ("John Doe",), 1974))
        with self.assertRaises(TypeError):
            pick["cities"] = ("Elsewhere",)
        self.assertFalse(set(pick) & set(removal.TYPEABLE))

    def test_their_record_missing_fails_with_evidence(self):
        script = PickScript()
        script.start_url = f"http://127.0.0.1:{self.srv.server_address[1]}/results"
        self.db.save_person(self.row["person"], "T", "", names=["Jane Doe"], places=["Tulsa OK"])
        r = removal.run(self.db, self.row, script, "dry_run", self.open_page)
        self.assertEqual(r["state"], "failed")
        self.assertIn("isn't on this page", r["error"])

    def test_a_stale_dry_run_must_be_redone(self):
        removal.run(self.db, self.row, self.script, "dry_run", self.open_page)
        self.db.x("UPDATE removals SET updated=updated-25*3600")
        with self.assertRaises(BadMove):
            removal.run(self.db, self.row, self.script, "submit", self.open_page)
        self.assertEqual(self.posts, [])
        removal.run(self.db, self.row, self.script, "dry_run", self.open_page)
        self.assertEqual(removal.run(self.db, self.row, self.script, "submit", self.open_page)["state"], "requested")

    def test_form_shape_on_a_page_that_is_gone(self):
        ev = removal.Evidence(self.db.dir, 1, "dry_run")

        class Gone:
            def evaluate(self, js):
                raise RuntimeError("Target page, context or browser has been closed")
        ev.form_shape(Gone())
        self.assertIn("has been closed", (ev.dir / "form-shape.json").read_text())

    def test_unknown_mode(self):
        with self.assertRaises(BadMove):
            removal.run(self.db, self.row, self.script, "yolo", self.open_page)

    def test_browser_page_stealth_hands_the_page_over_and_reraises(self):
        from unittest import mock
        seen = []

        def fake_fetch(url, page_action=None, **kw):
            seen.append((url, kw["solve_cloudflare"]))
            page_action("PAGE")
        self.db.set("stealth", "1")
        with mock.patch("scrapling.fetchers.StealthyFetcher.fetch", side_effect=fake_fetch):
            wp = removal.browser_page(self.db)
            got = []
            wp("https://x/optout", got.append)
            self.assertEqual((got, seen), (["PAGE"], [("https://x/optout", True)]))
            with self.assertRaises(KeyError):
                wp("https://x/optout", lambda page: {}["boom"])

    def test_browser_page_plain_runs_a_real_chromium(self):
        self.db.set("stealth", "0")
        got = []
        # its own thread, as in the dashboard (this test thread already runs the test class's Playwright)
        t = threading.Thread(target=lambda: removal.browser_page(self.db)(
            self.script.start_url, lambda page: got.append(page.locator("h1").inner_text())))
        t.start()
        t.join(60)
        self.assertEqual(got, ["Opt out"])
        self.assertEqual(self.posts, [])

    def test_packaged_edition_without_its_browser_says_where_to_get_it(self):
        from unittest import mock
        from guard_dog import browser
        with mock.patch.object(browser, "pin", return_value={"dir": "chromium-1248", "subdir": "chrome-linux64"}), \
                mock.patch.object(browser, "installed", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "this needs the hidden browser: Settings, Download it"):
                removal.browser_page(self.db)("https://x/optout", lambda page: None)

    def test_the_base_class_must_be_filled_in(self):
        with self.assertRaises(NotImplementedError):
            removal.OptOut().fill(None, {}, {}, None)
        with self.assertRaises(NotImplementedError):
            removal.OptOut().press(None, None)


if __name__ == "__main__":
    unittest.main()

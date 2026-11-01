"""Per-site opt-out scripts against captured or synthetic pages served locally, in real Chromium. Nothing is
submitted anywhere; spokeo-optout.html is Spokeo's real opt-out page, captured 2026-10-29 by a plain fetch."""
import tempfile
import threading
import time
import unittest
from pathlib import Path

from playwright.sync_api import sync_playwright

from guard_dog import removal, scanner, web
from guard_dog.db import DB
from guard_dog.optouts import SCRIPTS

FIX = Path(__file__).parent / "fixtures" / "optout"
CAPTCHA = b'<iframe title="reCAPTCHA" src="https://www.google.com/recaptcha/api2/anchor?k=x"></iframe>'


class Served(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pages = {"/search": (FIX / "spokeo-search.html").read_bytes(),
                 "/optout": (FIX / "spokeo-optout.html").read_bytes(),
                 "/optout-captcha": (FIX / "spokeo-optout.html").read_bytes().replace(b"</body>", CAPTCHA + b"</body>")}

        class H(web.http.server.BaseHTTPRequestHandler):
            def do_GET(s):
                body = pages.get(s.path.split("?")[0], b"<p>nothing here</p>")
                s.send_response(200 if s.path.split("?")[0] in pages else 404)
                s.send_header("Content-Type", "text/html")
                s.end_headers()
                s.wfile.write(body)

            def do_POST(s):
                raise AssertionError("a test submitted a form")

            def log_message(s, *a):
                pass
        cls.srv = web.Server(("127.0.0.1", 0), H)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
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
        pid = self.db.save_person(None, "Me", "", names=["Jane Doe"], places=["Columbus Ohio"], relatives=["John Doe"],
                                  birth_year=time.gmtime().tm_year - 52, phones=["330-555-0142"])
        scanner.record(self.db, pid, {"id": "spokeo", "name": "Spokeo"}, self.base + "/search", "listed",
                       ["name: Jane Doe"], None)
        self.row = dict(self.db.q("SELECT * FROM findings")[0])
        self.ctx = self.chrome.new_context()
        # the captured page asks for Spokeo's scripts and images: refused, so the test stays offline and nothing
        # reaches spokeo.com
        self.ctx.route("**/*", lambda r: r.continue_() if r.request.url.startswith(self.base) else r.abort())
        self.last = None
        # point the script's own address at the local copy of the form for the test
        self.script = SCRIPTS["spokeo"]
        self.real_optout = self.script.optout_url
        self.script.optout_url = self.base + "/optout"

    def tearDown(self):
        self.script.optout_url = self.real_optout
        self.ctx.close()
        self.tmp.cleanup()

    def with_page(self, url, steps):
        p = self.ctx.new_page()
        p.goto(url)
        steps(p)
        self.last = p


class Spokeo(Served):
    def test_dry_run_picks_their_profile_and_fills_the_real_form(self):
        r = removal.run(self.db, self.row, self.script, "dry_run", self.with_page)
        self.assertEqual((r["state"], r["typed"]), ("ready", ["removal_email"]))
        form = self.last.locator("form:has(input[name=email])")
        self.assertEqual(form.locator("input[name=url]").input_value(), self.base + "/Jane-Doe/Ohio/Columbus/p2222222222")
        self.assertEqual(form.locator("input[name=email]").input_value(), "removals@example.com")
        log = (Path(self.db.q("SELECT evidence FROM removals")[0]["evidence"]) / "steps.log").read_text()
        self.assertIn("picked their profile out of 2", log)

    def test_a_picture_captcha_hands_it_to_a_person(self):
        self.script.optout_url = self.base + "/optout-captcha"
        r = removal.run(self.db, self.row, self.script, "dry_run", self.with_page)
        self.assertEqual(r["state"], "manual")
        self.assertIn("not a robot", r["reason"])
        import json
        fill = json.loads(self.db.q("SELECT prefill FROM removals")[0]["prefill"])
        self.assertEqual(fill, {"Your Spokeo profile address": self.base + "/Jane-Doe/Ohio/Columbus/p2222222222",
                                "Your removal email": "removals@example.com"})

    def test_details_fetched_again_for_a_site_already_waiting_on_the_person(self):
        import json
        self.script.optout_url = self.base + "/optout-captcha"
        removal.move(self.db, self.row["id"], "manual", note="older run, nothing to paste")
        r = removal.run(self.db, self.row, self.script, "dry_run", self.with_page)
        self.assertEqual(r["state"], "manual")
        fill = json.loads(self.db.q("SELECT prefill FROM removals")[0]["prefill"])
        self.assertIn("Your Spokeo profile address", fill)

    def test_no_card_of_theirs_fails(self):
        self.db.save_person(self.row["person"], "Me", "", names=["Jane Doe"], places=["Tulsa OK"])
        r = removal.run(self.db, self.row, self.script, "dry_run", self.with_page)
        self.assertEqual(r["state"], "failed")
        self.assertIn("isn't among the 2", r["error"])

    def test_a_field_the_page_wipes_is_caught(self):
        # like a page that re-renders after load: the field is wiped half a second after it's filled
        wipe = (b"<script>document.addEventListener('input',e=>{if(e.target.name==='url')"
                b"setTimeout(()=>{e.target.value=''},500)},true)</script></body>")
        self.ctx.route(self.base + "/optout", lambda r: r.fulfill(
            status=200, content_type="text/html",
            body=(FIX / "spokeo-optout.html").read_bytes().replace(b"</body>", wipe)))
        r = removal.run(self.db, self.row, self.script, "dry_run", self.with_page)
        self.assertEqual(r["state"], "failed")
        self.assertIn("didn't keep the url field after 3 tries", r["error"])
        ev = Path(self.db.q("SELECT evidence FROM removals")[0]["evidence"])
        shape = (ev / "form-shape.json").read_text()
        self.assertIn('"name": "url"', shape)
        self.assertNotIn("removals@example.com", shape)  # field values are never written down
        self.assertNotIn("p2222222222", shape)
        self.assertTrue(list(ev.glob("*the-page-when-it-stopped*.png")))

    def test_submit_presses_their_button_and_waits_for_the_confirmation(self):
        # the captured form, with a stand-in for Spokeo's own script: on submit it says "check your email" (no POST)
        ok = b"<script>document.addEventListener('submit',e=>{e.preventDefault();document.body.innerHTML+='<p>Check your email</p>'},true)</script></body>"
        self.ctx.route(self.base + "/optout", lambda r: r.fulfill(
            status=200, content_type="text/html", body=(FIX / "spokeo-optout.html").read_bytes().replace(b"</body>", ok)))
        removal.run(self.db, self.row, self.script, "dry_run", self.with_page)
        r = removal.run(self.db, self.row, self.script, "submit", self.with_page)
        self.assertEqual(r["state"], "requested")
        self.assertIn("Check your email", self.last.content())

    def test_a_wiped_email_field_is_caught_too(self):
        wipe = (b"<script>document.addEventListener('input',e=>{if(e.target.name==='email')"
                b"setTimeout(()=>{e.target.value=''},500)},true)</script></body>")
        self.ctx.route(self.base + "/optout", lambda r: r.fulfill(
            status=200, content_type="text/html", body=(FIX / "spokeo-optout.html").read_bytes().replace(b"</body>", wipe)))
        self.assertIn("didn't keep the email field after 3 tries",
                      removal.run(self.db, self.row, self.script, "dry_run", self.with_page)["error"])

    def serve_optout(self, extra):
        self.ctx.route(self.base + "/optout", lambda r: r.fulfill(
            status=200, content_type="text/html", body=(FIX / "spokeo-optout.html").read_bytes().replace(b"</body>", extra + b"</body>")))

    def steps_log(self):
        return (Path(self.db.q("SELECT evidence FROM removals")[0]["evidence"]) / "steps.log").read_text()

    def test_a_page_that_wipes_the_fields_once_after_load_gets_typed_again(self):
        # like React taking over the form late: once both fields are filled it empties every field, once. Tied to the
        # typing, not to a clock: on a slow machine (arm64 emulation) a timer fired before the typing had finished.
        self.serve_optout(b"<script>let done=false;document.addEventListener('input',()=>{if(done)return;"
                          b"const f=[...document.querySelectorAll('input[name=url],input[name=email]')];if(f.length&&f.every(i=>i.value)){done=true;"
                          b"setTimeout(()=>f.forEach(i=>i.value=''),100)}})</script>")
        r = removal.run(self.db, self.row, self.script, "dry_run", self.with_page)
        self.assertEqual(r["state"], "ready")
        log = self.steps_log()
        self.assertIn("attempt 1: wiped email, url", log)
        self.assertIn("attempt 2: kept", log)
        self.assertEqual(self.last.locator("input[name=url]").input_value(), self.base + "/Jane-Doe/Ohio/Columbus/p2222222222")

    def test_consent_banner_is_declined_never_accepted(self):
        self.serve_optout(b"""<div class="osano-cm-dialog" style="position:fixed;top:0;left:0;right:0;height:200px;background:#fff">
            <button class=osano-cm-accept-all onclick="document.title='ACCEPTED'">Accept all</button>
            <button class=osano-cm-deny onclick="this.parentElement.remove();document.title='DECLINED'">Deny</button></div>""")
        r = removal.run(self.db, self.row, self.script, "dry_run", self.with_page)
        self.assertEqual(r["state"], "ready")
        self.assertEqual(self.last.title(), "DECLINED")
        self.assertIn("cookie banner: pressed 'Deny'", self.steps_log())

    def test_a_button_shown_twice_is_looked_at_once(self):
        self.serve_optout(b"""<div class="osano-cm-dialog osano-x" style="position:fixed;top:0;height:60px;z-index:9">
            <button style="display:none">Reject</button>
            <span class=osano-cm-buttons><button class=osano-cm-deny onclick="this.closest('div').remove()">Deny</button></span></div>""")
        removal.run(self.db, self.row, self.script, "dry_run", self.with_page)
        self.assertEqual(self.steps_log().count("cookie banner: pressed 'Deny'"), 1)

    def test_banner_without_a_decline_button_is_left_alone_and_noted(self):
        self.serve_optout(b"""<div id="osano-x" style="position:fixed;bottom:0;height:40px;background:#fff">
            We use cookies <button onclick="document.title='ACCEPTED'">OK</button></div>""")
        removal.run(self.db, self.row, self.script, "dry_run", self.with_page)
        self.assertNotEqual(self.last.title(), "ACCEPTED")
        self.assertIn("no reject or decline button", self.steps_log())

    def test_form_shape_says_whether_a_banner_was_showing(self):
        self.serve_optout(b"""<div class="osano-cm-window" style="position:fixed;top:0;height:30px">Cookies</div>
            <script>document.addEventListener('input',e=>{if(e.target.name==='url')setTimeout(()=>{e.target.value=''},500)},true)</script>""")
        removal.run(self.db, self.row, self.script, "dry_run", self.with_page)
        ev = Path(self.db.q("SELECT evidence FROM removals")[0]["evidence"])
        self.assertIn('"consent_banner_visible": true', (ev / "form-shape.json").read_text())

    def test_declares_only_the_removal_email(self):
        self.assertEqual(self.script.fields, ("removal_email",))
        self.assertEqual((self.script.confirm, self.script.domains), ("email", ("spokeo.com",)))


class PickRecord(unittest.TestCase):
    def test_listed_beats_possible_and_nothing_raises(self):
        pick = {"names": ("Jane Doe",), "cities": ("Columbus",), "relatives": ("John Doe",), "birth_year": None}
        cards = [("a", "Jane Doe Age 40 Lives in Miami"), ("b", "Jane Doe, Columbus"),
                 ("c", "Jane Doe Age 52 Lives in Columbus Related to John Doe")]
        self.assertEqual(removal.pick_record(cards, pick), "c")
        self.assertEqual(removal.pick_record(cards[:2] + [("d", "We found 2 records for Jane Doe in Columbus")], pick), "d")
        with self.assertRaises(LookupError):
            removal.pick_record([("a", "Jane Doe Age 40 Lives in Miami")], pick)


if __name__ == "__main__":
    unittest.main()

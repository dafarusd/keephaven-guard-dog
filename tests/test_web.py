"""The dashboard in a real browser (Playwright Chromium) plus raw requests for header-level checks — no network:
scanner fetches, the sweep and breach checks are patched."""
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from playwright.sync_api import sync_playwright

from guard_dog import removal, scanner, web
from guard_dog.db import DB


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


RAW = urllib.request.build_opener(NoRedirect)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class WebServer(unittest.TestCase):
    """One network-mode server per test (password required), on 127.0.0.1."""

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
        self.port = free_port()
        web.STARTED = time.time()
        web.FAILS.clear()
        self.httpd, self.code, local = web.build(self.db, "127.0.0.1", self.port, network=True)
        self.assertFalse(local)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.port}"
        self.ctx = self.chrome.new_context()
        self.page = self.ctx.new_page()

    def tearDown(self):
        self.ctx.close()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    # helpers
    def raw(self, path, data=None, headers=None):
        req = urllib.request.Request(self.base + path, data=data.encode() if data is not None else None,
                                     headers=headers or {})
        try:
            with RAW.open(req, timeout=10) as r:
                return r.status, dict(r.headers), r.read().decode(errors="replace")
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read().decode(errors="replace")

    def submit(self, label="Save"):
        """Press a form button and wait for the page it leads to."""
        with self.page.expect_navigation():
            self.page.click(f'button:has-text("{label}")')

    def set_password(self, pw="testpass99"):
        self.page.goto(self.base + "/")
        self.page.fill("input[name=pw]", pw)
        self.page.fill("input[name=pw2]", pw)
        self.submit("Continue")



class Dashboard(WebServer):
    # first password and sign-in
    def test_first_password_inside_the_window_needs_no_code(self):
        self.page.goto(self.base + "/")
        self.assertEqual(self.page.locator("input[name=code]").count(), 0)
        self.assertIn("isn't encrypted", self.page.content())
        self.assertNotIn(str(self.db.dir), self.page.content())
        self.set_password()
        self.assertEqual(self.page.locator("h1").inner_text(), "People")
        self.assertTrue(self.db.get("password_hash"))
        self.assertFalse((self.db.dir / "setup-code.txt").exists())

    def test_after_the_window_the_code_is_needed(self):
        web.STARTED -= web.SETUP_WINDOW + 1
        self.page.goto(self.base + "/")
        self.assertEqual(self.page.locator("input[name=code]").count(), 1)
        self.page.fill("input[name=code]", "0000-0000-0000")
        self.page.fill("input[name=pw]", "testpass99")
        self.page.fill("input[name=pw2]", "testpass99")
        self.submit("Continue")
        self.assertIn("setup code isn't right", self.page.content())
        self.assertFalse(self.db.get("password_hash"))
        self.page.fill("input[name=code]", self.code)
        self.page.fill("input[name=pw]", "testpass99")
        self.page.fill("input[name=pw2]", "testpass99")
        self.submit("Continue")
        self.assertEqual(self.page.locator("h1").inner_text(), "People")

    def test_mismatched_passwords(self):
        self.page.goto(self.base + "/")
        self.page.fill("input[name=pw]", "testpass99")
        self.page.fill("input[name=pw2]", "different99")
        self.submit("Continue")
        self.assertIn("must match", self.page.content())

    def test_sign_in_and_lockout(self):
        self.set_password()
        fresh = self.chrome.new_context().new_page()
        fresh.goto(self.base + "/settings")
        self.assertEqual(fresh.locator("h1").inner_text(), "Sign in")
        o = {"Origin": self.base, "Content-Type": "application/x-www-form-urlencoded"}
        codes = [self.raw("/login", "pw=wrongwrong", o)[0] for _ in range(6)]
        self.assertEqual(codes[:5], [401] * 5)
        self.assertEqual(codes[5], 429)
        self.assertEqual(self.raw("/login", "pw=testpass99", o)[0], 429)  # locked even with the right one
        web.FAILS.clear()
        fresh.fill("input[name=pw]", "testpass99")
        with fresh.expect_navigation():
            fresh.click("button:has-text('Continue')")
        self.assertEqual(fresh.locator("h1").inner_text(), "People")
        fresh.context.close()

    # request checks
    def test_unknown_host_names_are_refused(self):
        self.assertEqual(self.raw("/", headers={"Host": f"evil.example:{self.port}"})[0], 421)
        self.assertEqual(self.raw("/login", "pw=x&pw2=x", {"Host": f"evil.example:{self.port}",
                                                         "Origin": f"http://evil.example:{self.port}"})[0], 421)

    def test_cross_site_form_is_refused_even_with_the_cookie(self):
        self.set_password()
        evil_port = free_port()
        attack = (f"<form id=f method=post action='{self.base}/settings'><input name=stealth value=1>"
                  "<input name=proxy value='http://attacker:8080'></form><script>f.submit()</script>").encode()

        class Evil(web.http.server.BaseHTTPRequestHandler):
            def do_GET(s):
                s.send_response(200)
                s.send_header("Content-Type", "text/html")
                s.end_headers()
                s.wfile.write(attack)

            def log_message(s, *a):
                pass
        evil = web.Server(("127.0.0.1", evil_port), Evil)
        threading.Thread(target=evil.serve_forever, daemon=True).start()
        try:
            self.page.goto(f"http://localhost:{evil_port}/")
            self.page.wait_for_load_state()
            self.page.wait_for_timeout(500)
            self.assertIn("cross-site request refused", self.page.content())
        finally:
            evil.shutdown()
            evil.server_close()
        self.assertEqual(self.db.get("stealth"), "0")
        self.assertEqual(self.db.get("proxy"), "")

    def test_origin_rules(self):
        h = {"Content-Type": "application/x-www-form-urlencoded"}
        self.assertEqual(self.raw("/login", "pw=testpass99&pw2=testpass99", h)[0], 403)  # no origin at all
        self.assertEqual(self.raw("/login", "pw=testpass99&pw2=testpass99",
                                  dict(h, Origin="null", **{"Sec-Fetch-Site": "same-site"}))[0], 403)
        self.assertEqual(self.raw("/login", "pw=testpass99&pw2=testpass99",
                                  dict(h, Origin="http://evil.example", **{"Sec-Fetch-Site": "same-origin"}))[0], 403)
        st, _, body = self.raw("/login", "pw=testpass99&pw2=testpass99", dict(h, Origin="null", **{"Sec-Fetch-Site": "same-origin"}))
        self.assertEqual(st, 303, body[-600:])

    # pages
    def test_people_add_edit_delete(self):
        self.set_password()
        p = self.page
        p.goto(self.base + "/person/new")
        self.submit("Save")
        self.assertIn("At least one name", p.content())
        p.goto(self.base + "/person/new")
        p.fill("input[name=label]", "Me")
        p.fill("textarea[name=names]", "Jane Doe\nJane Roe")
        p.fill("textarea[name=places]", "Columbus, OH")
        p.fill("input[name=consent]", "it's me")
        p.fill("textarea[name=relatives]", "John Doe\nMary Doe")
        p.fill("input[name=birth_year]", "1974")
        self.submit("Save")
        self.assertTrue(p.url.endswith("/person/1"))
        me = self.db.person(1)
        self.assertEqual((me["names"], me["relatives"], me["birth_year"]), (["Jane Doe", "Jane Roe"], ["John Doe", "Mary Doe"], 1974))
        p.goto(self.base + "/person/1/edit")
        self.assertEqual(p.input_value("input[name=birth_year]"), "1974")
        cookie = "; ".join(f"{c['name']}={c['value']}" for c in self.ctx.cookies())
        self.raw("/person/save", "id=1&label=Me&names=Jane+Doe&birth_year=17", {"Origin": self.base, "Cookie": cookie,
                                                                          "Content-Type": "application/x-www-form-urlencoded"})
        self.assertIsNone(self.db.person(1)["birth_year"])  # nonsense years are dropped, not stored
        p.goto(self.base + "/person/1/edit")
        p.fill("textarea[name=phones]", "330-555-0142")
        self.submit("Save")
        self.assertEqual(self.db.person(1)["phones"], ["330-555-0142"])
        p.goto(self.base + "/")
        self.assertIn("Me", p.content())
        p.goto(self.base + "/person/1/edit")
        p.check("input[name=confirm]")
        self.submit("Delete")
        self.assertIsNone(self.db.person(1))

    def test_settings_round_trip_and_bounds(self):
        self.set_password()
        p = self.page
        p.goto(self.base + "/settings")
        p.check("input[name=stealth]")
        p.fill("input[name=rescan_hours]", "12")
        p.fill("input[name=workers]", "3")
        p.fill("input[name=stealth_workers]", "3")
        p.fill("input[name=removal_email]", "removals@example.com")
        self.submit("Save")
        self.assertIn("Saved.", p.content())
        self.assertEqual([self.db.get(k) for k in ("stealth", "rescan_hours", "workers", "stealth_workers",
                                                   "removal_email")], ["1", "12", "3", "3", "removals@example.com"])
        p.goto(self.base + "/settings")
        self.assertTrue(p.is_checked("input[name=stealth]"))
        o = {"Origin": self.base, "Content-Type": "application/x-www-form-urlencoded",
             "Cookie": "; ".join(f"{c['name']}={c['value']}" for c in self.ctx.cookies())}
        self.raw("/settings", "rescan_hours=inf&delay_min=nan&workers=0&sweep_limit=-5&stealth_workers=99", o)
        self.assertEqual(self.db.get("rescan_hours"), "12")
        self.raw("/settings", "rescan_hours=0.25", o)
        self.assertEqual(self.db.get("rescan_hours"), "1")
        # half up to each field's step, no trailing ".0" (the browser refuses a value off the step)
        for form, want in (("rescan_hours=1.5&delay_min=0.3&delay_max=2.5", ("2", "0.5", "2.5")),
                           ("rescan_hours=2.5&delay_min=0&delay_max=6", ("3", "0", "6")),
                           ("rescan_hours=0&delay_min=1.25&delay_max=120", ("0", "1.5", "120")),
                           ("rescan_hours=720", ("720", "1.5", "120"))):
            self.raw("/settings", form, o)
            self.assertEqual([self.db.get(k) for k in ("rescan_hours", "delay_min", "delay_max")], list(want), form)
        for k, v in (("rescan_hours", "6.0"), ("delay_min", "2.0"), ("delay_max", "2.5")):
            self.db.set(k, v)  # what 0.1.0 saved
        p.goto(self.base + "/settings")
        self.assertEqual([p.input_value(f"input[name={k}]") for k in ("rescan_hours", "delay_min", "delay_max")],
                         ["6", "2", "2.5"])
        self.assertEqual(self.db.get("workers"), "1")
        self.assertEqual(self.db.get("sweep_limit"), "0")
        self.assertEqual(self.db.get("stealth_workers"), "4")
        self.assertEqual(self.db.get("stealth"), "0")  # an unchecked box saves as off

    def test_scan_button_runs_a_scan_and_shows_the_watch_line(self):
        self.set_password()
        self.db.save_person(None, "Me", "", names=["Jane Doe"], places=["Columbus, OH"])
        self.db.set("delay_min", "0")
        self.db.set("delay_max", "0")
        page_text = "Jane Doe, age 52, Columbus OH. Phone 330-555-0142. " + "x " * 3000
        with mock.patch.object(scanner, "fetch", lambda *a, **k: (200, page_text, page_text, "fake", "")), \
                mock.patch.object(scanner.sweep, "search", return_value=([], {})), \
                mock.patch.object(scanner.breaches, "xposedornot", return_value=[]):
            self.db.x("UPDATE people SET phones=? WHERE id=1", ('["330-555-0142"]',))
            self.page.goto(self.base + "/person/1")
            self.page.click("button:has-text('Scan now')")
            self.page.wait_for_selector("text=Next check", timeout=30000)
        listed = self.db.q("SELECT COUNT(*) n FROM findings WHERE status='listed'")[0]["n"]
        self.assertIn(f"We found you on {listed} sites that sell personal details", self.page.content())
        self.assertGreater(self.db.q("SELECT COUNT(*) n FROM findings WHERE status='listed'")[0]["n"], 0)

    def test_status_json_and_missing_things(self):
        self.set_password()
        cookie = "; ".join(f"{c['name']}={c['value']}" for c in self.ctx.cookies())
        self.db.save_person(None, "Me", "", names=["Jane Doe"])
        st, _, body = self.raw("/person/1/status", headers={"Cookie": cookie})
        self.assertEqual(st, 200)
        self.assertIn('"found": 0', body)
        self.assertEqual(self.raw("/person/99", headers={"Cookie": cookie})[0], 404)
        o = {"Origin": self.base, "Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"}
        self.assertEqual(self.raw("/person/99/scan", "x=1", o)[0], 404)
        self.assertEqual(self.raw("/person/1/removal", "fid=abc", o)[0], 404)
        self.assertEqual(self.raw("/nope", headers={"Cookie": cookie})[0], 404)

    def test_copy_viewer_is_sandboxed(self):
        self.set_password()
        cookie = "; ".join(f"{c['name']}={c['value']}" for c in self.ctx.cookies())
        (self.db.dir / "copies" / "1-site-x.html").write_text("<script>alert(1)</script><p>listing</p>")
        st, h, _ = self.raw("/copy/1-site-x.html", headers={"Cookie": cookie})
        self.assertEqual(st, 200)
        self.assertIn("sandbox", h["Content-Security-Policy"])
        self.assertIn("default-src 'none'", h["Content-Security-Policy"])
        self.assertEqual(h["X-Frame-Options"], "DENY")
        self.assertEqual(self.raw("/copy/..%2fguard-dog.sqlite", headers={"Cookie": cookie})[0], 400)
        self.assertEqual(self.raw("/copy/missing.html", headers={"Cookie": cookie})[0], 404)

    def test_password_check_page(self):
        self.set_password()
        with mock.patch.object(web.breaches, "password_seen", side_effect=[2266543, 0, RuntimeError("down")]):
            for want in ("Seen 2,266,543 times", "Not found in any known breach", "Couldn't check"):
                self.page.goto(self.base + "/password")
                self.page.fill("input[name=pw]", "whatever")
                self.submit("Check")
                self.assertIn(want, self.page.content())

    def test_removal_mark_and_alerts_seen(self):
        self.set_password()
        self.db.save_person(None, "Me", "", names=["Jane Doe"])
        scanner.record(self.db, 1, {"id": "whitepages", "name": "Whitepages"}, "https://www.whitepages.com/x", "listed",
                       ["name: Jane Doe", "phone: 1"], None)
        self.page.goto(self.base + "/person/1?show=listed")
        self.assertIn("new listing found", self.page.content())
        self.page.click("summary:has-text('How to remove')")
        self.submit("I've done it")
        self.assertEqual(removal.state(self.db, 1), "requested")
        self.assertIn("requested", self.page.content())
        self.assertEqual(self.db.q("SELECT removal FROM findings")[0]["removal"], None)  # the old column isn't written
        self.page.goto(self.base + "/person/1")
        self.submit("Got it")
        self.assertEqual(self.db.q("SELECT COUNT(*) n FROM alerts WHERE seen=0")[0]["n"], 0)


    def test_report_shows_breaches_web_pages_and_failed_checks(self):
        self.set_password()
        self.db.save_person(None, "Me", "", names=["Jane Doe"])
        self.db.x("INSERT INTO breaches VALUES(1,'j@x.com','Big','big.com','2024','Emails, Passwords','plaintext',1)")
        scanner.record(self.db, 1, {"id": "blog.example", "name": "blog.example", "kind": "web"}, "https://blog.example/p",
                       "possible", ["name: Jane Doe"], None)
        scanner.record(self.db, 1, {"id": "spokeo", "name": "Spokeo"}, "https://www.spokeo.com/x", "listed", ["phone: 1"], None)
        scanner.record(self.db, 1, {"id": "spokeo", "name": "Spokeo"}, "https://www.spokeo.com/x", "blocked", ["http 403"], None)
        self.page.goto(self.base + "/person/1")
        c = self.page.content()
        self.assertIn("password leaked", c)
        self.assertIn("<h3>blog.example</h3>", c)
        self.assertIn("How to remove", c)
        self.assertIn("last try", c)  # the listing stays listed; the failed re-check is shown in Details
        self.assertIn("Your email was in 1 data breach", c)

    def test_unauthenticated_post_and_oversized_form(self):
        self.set_password()
        o = {"Origin": self.base, "Content-Type": "application/x-www-form-urlencoded"}
        st, h, _ = self.raw("/person/save", "names=A B", o)
        self.assertEqual((st, h.get("Location")), (303, "/"))
        cookie = "; ".join(f"{c['name']}={c['value']}" for c in self.ctx.cookies())
        big = "names=" + "a" * 1_000_001
        self.assertEqual(self.raw("/person/save", big, dict(o, Cookie=cookie))[0], 400)  # body ignored -> no name

    def test_an_oversized_form_sent_slowly_still_gets_its_answer(self):
        """arm64 build, 2026-10-30: on a slow machine the server answered before reading the body, closed, and the
        sender got a broken connection instead of the 400."""
        import http.client
        import time as _t
        self.set_password()
        cookie = "; ".join(f"{c['name']}={c['value']}" for c in self.ctx.cookies())
        host, port = self.base.split("//")[1].split(":")
        conn = http.client.HTTPConnection(host, int(port), timeout=20)
        body = b"names=" + b"a" * 1_200_000
        conn.putrequest("POST", "/person/save")
        for k, v in {"Origin": self.base, "Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie,
                     "Content-Length": str(len(body))}.items():
            conn.putheader(k, v)
        conn.endheaders()
        for i in range(0, len(body), 200_000):  # a slow sender: the server has its answer ready long before the end
            conn.send(body[i:i + 200_000])
            _t.sleep(0.05)
        self.assertEqual(conn.getresponse().status, 400)
        conn.close()

    def test_thin_profile_gets_a_hint(self):
        self.set_password()
        self.db.save_person(None, "Me", "", names=["Jane Doe"], places=["Columbus, OH"])
        self.page.goto(self.base + "/person/1")
        self.assertIn("Add every city you've lived in", self.page.content())
        self.db.save_person(1, "Me", "", names=["Jane Doe"], places=["Columbus, OH"], relatives=["John Doe"])
        self.page.goto(self.base + "/person/1")
        self.assertNotIn("Add every city", self.page.content())

    def test_removal_mark_on_someone_elses_finding_does_nothing(self):
        self.set_password()
        self.db.save_person(None, "Me", "", names=["Jane Doe"])
        other = self.db.save_person(None, "Mom", "", names=["Mary Doe"])
        scanner.record(self.db, other, {"id": "s", "name": "S"}, "https://s/1", "listed", [], None)
        fid = self.db.q("SELECT id FROM findings")[0]["id"]
        cookie = "; ".join(f"{c['name']}={c['value']}" for c in self.ctx.cookies())
        st, _, _ = self.raw("/person/1/removal", f"fid={fid}", {"Origin": self.base, "Cookie": cookie,
                                                              "Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(st, 303)
        self.assertEqual(removal.state(self.db, fid), "found")

    def test_unknown_person_action_is_404(self):
        self.set_password()
        self.db.save_person(None, "Me", "", names=["Jane Doe"])
        cookie = "; ".join(f"{c['name']}={c['value']}" for c in self.ctx.cookies())
        o = {"Origin": self.base, "Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"}
        self.assertEqual(self.raw("/person/1/explode", "x=1", o)[0], 404)
        self.assertEqual(self.raw("/somewhere", "x=1", o)[0], 404)
        self.assertEqual(self.raw("/person/1/delete", "x=1", o)[0], 404)  # no confirm box ticked: nothing deleted
        self.assertIsNotNone(self.db.person(1))


    def test_removal_preview_then_submit_from_the_dashboard(self):
        from guard_dog import removal as rm
        self.set_password()
        self.db.set("removal_email", "removals@example.com")
        self.db.save_person(None, "Me", "", names=["Jane Doe"], places=["Columbus OH"])
        scanner.record(self.db, 1, {"id": "spokeo", "name": "Spokeo"}, "https://www.spokeo.com/s", "listed",
                       ["name: Jane Doe"], None)
        calls = []

        class FakePage:  # stands in for the browser page: records what the script did, takes a PNG "screenshot"
            def screenshot(self, path, full_page):
                Path(path).write_bytes(b"\x89PNG\r\n\x1a\nfake")

        def fake_with_page(db):
            def with_page(url, steps):
                calls.append(url)
                steps(FakePage())
            return with_page
        script = web.SCRIPTS["spokeo"]
        with mock.patch.object(rm, "browser_page", fake_with_page), \
                mock.patch.object(script, "fill", lambda page, data, pick, ev: calls.append(("fill", sorted(data)))), \
                mock.patch.object(script, "press", lambda page, ev: calls.append("press")):
            self.page.goto(self.base + "/person/1?show=listed")
            self.assertIn("fills in Spokeo's removal form and stops", self.page.content())
            self.submit("Remove me")
            for _ in range(50):
                if rm.state(self.db, 1) == "ready" and 1 not in web.RUNNING:
                    break
                time.sleep(0.1)
            self.page.goto(self.base + "/person/1?show=listed")
            c = self.page.content()
            self.assertIn("Here's what we'll send Spokeo: your Spokeo profile address and your removal email", c)
            self.assertNotIn("press", calls)  # a preview never presses the site's button
            shot = self.page.locator("a:has-text('See the filled-in form')").get_attribute("href")
            st, h, _ = self.raw(shot, headers={"Cookie": "; ".join(f"{x['name']}={x['value']}" for x in self.ctx.cookies())})
            self.assertEqual((st, h["Content-Type"]), (200, "image/png"))
            self.submit("Send it")
            for _ in range(50):
                if rm.state(self.db, 1) == "requested" and 1 not in web.RUNNING:
                    break
                time.sleep(0.1)
        self.assertEqual(rm.state(self.db, 1), "requested")
        self.assertEqual(calls.count("press"), 1)
        self.assertEqual([c for c in calls if isinstance(c, tuple)], [("fill", ["removal_email"])] * 2)
        self.page.goto(self.base + "/person/1?show=listed")
        self.assertIn("Removal sent", self.page.content())
        self.assertIn("Removal requested", self.page.content())

    def test_removal_states_shown_and_evidence_paths_checked(self):
        from guard_dog import removal as rm
        self.set_password()
        cookie = {"Cookie": "; ".join(f"{x['name']}={x['value']}" for x in self.ctx.cookies())}
        self.db.save_person(None, "Me", "", names=["Jane Doe"])
        scanner.record(self.db, 1, {"id": "spokeo", "name": "Spokeo"}, "https://www.spokeo.com/s", "listed", [], None)
        rm.move(self.db, 1, "manual", note="Spokeo shows an 'I'm not a robot' box")
        self.page.goto(self.base + "/person/1?show=listed")
        self.assertIn("This one needs you: Spokeo shows an", self.page.content())
        self.assertIn(">Needs you<", self.page.content())
        rm.move(self.db, 1, "ready")
        self.db.x("UPDATE removals SET updated=updated-90000")
        self.page.goto(self.base + "/person/1?show=listed")
        self.assertIn("was more than a day ago", self.page.content())
        rm.move(self.db, 1, "failed", note="Spokeo's form didn't keep the url field")
        self.page.goto(self.base + "/person/1?show=listed")
        self.assertIn("That didn't work: Spokeo", self.page.content())
        web.RUNNING.add(1)
        self.page.goto(self.base + "/person/1?show=listed")
        self.assertIn("Working on it", self.page.content())
        web.RUNNING.discard(1)
        rm.move(self.db, 1, "ready")
        rm.move(self.db, 1, "requested")
        rm.move(self.db, 1, "gone")
        self.page.goto(self.base + "/person/1?show=listed")
        self.assertIn(">Removed<", self.page.content())
        for bad in ("/evidence/x/20261029-120000-dry_run/a.png", "/evidence/1/../../a.png", "/evidence/1/20261029-120000-yolo/a.png",
                    "/evidence/1/20261029-120000-dry_run/a.sh"):
            self.assertIn(self.raw(bad, headers=cookie)[0], (400, 404), bad)
        self.assertEqual(self.raw("/evidence/1/20261029-120000-dry_run/missing.png", headers=cookie)[0], 404)
        o = dict(cookie, Origin=self.base, **{"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(self.raw("/person/1/remove", "fid=1&mode=yolo", o)[0], 404)
        scanner.record(self.db, 1, {"id": "whitepages", "name": "Whitepages"}, "https://w/1", "listed", [], None)
        wp = self.db.q("SELECT id FROM findings WHERE source='whitepages'")[0]["id"]
        self.assertEqual(self.raw("/person/1/remove", f"fid={wp}&mode=dry_run", o)[0], 303)  # no script for it: nothing runs
        self.assertNotIn(wp, web.RUNNING)
        self.assertEqual(rm.state(self.db, wp), "found")

    def test_a_site_that_needs_a_person_gets_steps_and_what_to_paste(self):
        from guard_dog import removal as rm
        self.set_password()
        self.db.save_person(None, "Me", "", names=["Jane Doe"])
        scanner.record(self.db, 1, {"id": "spokeo", "name": "Spokeo"}, "https://www.spokeo.com/s", "listed", [], None)
        rm.move(self.db, 1, "manual", note="Spokeo asks you to tick 'I'm not a robot'")
        self.db.x("UPDATE removals SET prefill=? WHERE finding=1",
                  ('{"Your Spokeo profile address": "https://www.spokeo.com/Jane-Doe/Ohio/Columbus/p1", '
                   '"Your removal email": "removals@example.com"}',))
        self.page.goto(self.base + "/person/1")
        c = self.page.inner_text("main")
        self.assertIn("Tick 'I'm not a robot' and press the button.", c)
        self.assertIn("Open the email Spokeo sends and click its link.", c)
        self.assertEqual(self.page.input_value("input[value*='p1']"), "https://www.spokeo.com/Jane-Doe/Ohio/Columbus/p1")
        self.assertEqual(self.page.input_value("input[value*='removals@']"), "removals@example.com")

    def test_a_waiting_site_without_details_offers_to_fetch_them(self):
        from guard_dog import removal as rm
        self.set_password()
        self.db.save_person(None, "Me", "", names=["Jane Doe"])
        scanner.record(self.db, 1, {"id": "spokeo", "name": "Spokeo"}, "https://www.spokeo.com/s", "listed", [], None)
        rm.move(self.db, 1, "manual", note="Spokeo asks you to tick 'I'm not a robot'")
        self.page.goto(self.base + "/person/1")
        self.assertIn("Get the details again", self.page.inner_text("main"))
        self.db.x("UPDATE removals SET prefill='{\"Your removal email\": \"r@example.com\"}' WHERE finding=1")
        self.page.goto(self.base + "/person/1")
        self.assertNotIn("Get the details again", self.page.inner_text("main"))

    def test_a_listing_not_seen_once_says_its_waiting_for_the_next_scan(self):
        self.set_password()
        self.db.save_person(None, "Me", "", names=["Jane Doe"])
        b = {"id": "truepeoplesearch", "name": "TruePeopleSearch"}
        scanner.record(self.db, 1, b, "https://www.truepeoplesearch.com/s", "listed", [], None)
        scanner.record(self.db, 1, b, "https://www.truepeoplesearch.com/s", "not found", [], None)
        self.page.goto(self.base + "/person/1")
        c = self.page.inner_text("main")
        self.assertIn("Not seen on the last scan. Guard Dog counts it as gone when the next scan agrees", c)
        self.assertIn("TruePeopleSearch no longer shows the listing; proven when the next scan agrees.", c)
        self.assertIn("1 site that listed you didn't show it this scan; the next scan checks again before calling it gone.", c)
        self.assertNotIn("No site we checked lists you", c)  # never an all-clear on one miss
        self.assertIn("Not seen this time", c)
        scanner.record(self.db, 1, {"id": "spokeo", "name": "Spokeo"}, "https://www.spokeo.com/s", "listed", [], None)
        self.page.goto(self.base + "/person/1")
        self.assertIn("We found you on 1 site that sells personal details. 1 more that listed you didn't show it this "
                      "scan; the next scan checks again.", self.page.inner_text("main"))
        scanner.record(self.db, 1, b, "https://www.truepeoplesearch.com/s", "not found", [], None)
        self.page.goto(self.base + "/person/1")
        self.assertNotIn("Not seen on the last scan", self.page.inner_text("main"))

    def test_whats_new_only_has_news(self):
        self.set_password()
        self.db.save_person(None, "Me", "", names=["Jane Doe"])
        for t in ("web search: bing failed 5 of 6 searches (results unrelated to the search); the sweep missed it.",
                  "people-search check failed (Spokeo): Timeout", "breach check for j@x.com failed: 500",
                  "Spokeo: new listing found."):
            self.db.alert(1, t)
        self.page.goto(self.base + "/person/1")
        news = self.page.inner_text("main")
        self.assertIn("Spokeo: new listing found.", news)
        for t in ("bing", "Timeout", "breach check", "failed"):
            self.assertNotIn(t, news.split("Not checked yet")[0])

    def test_a_removal_run_that_is_refused_becomes_an_alert(self):
        from guard_dog import removal as rm
        self.db.save_person(None, "Me", "", names=["Jane Doe"])
        scanner.record(self.db, 1, {"id": "spokeo", "name": "Spokeo"}, "https://www.spokeo.com/s", "listed", [], None)
        row = dict(self.db.q("SELECT * FROM findings")[0])
        self.assertTrue(web.start_removal(self.db, row, "submit"))  # no dry run yet → refused, as an alert
        for _ in range(50):
            if 1 not in web.RUNNING:
                break
            time.sleep(0.05)
        self.assertTrue(any("submit needs a dry run first" in a["text"] for a in self.db.q("SELECT text FROM alerts")))
        web.RUNNING.add(1)
        self.assertFalse(web.start_removal(self.db, row, "dry_run"))  # one run per listing at a time
        web.RUNNING.discard(1)
        self.assertEqual(rm.state(self.db, 1), "found")


    def test_remove_me_from_all_preview_then_send(self):
        from guard_dog import requests as rq
        self.set_password()
        self.db.save_person(None, "Me", "", names=["Jane Doe"], places=["Columbus OH"], home_state="OH")
        scanner.record(self.db, 1, {"id": "spokeo", "name": "Spokeo"}, "https://www.spokeo.com/x", "listed",
                       ["name: Jane Doe", "city: Columbus, OH"], None)
        p = self.page
        p.goto(self.base + "/person/1")
        p.click("a:has-text('Remove you from all')")
        self.assertIn("Set up the removal inbox in Settings first", p.inner_text("main"))
        # set the inbox: refused without the removal-only box, saved with it
        p.goto(self.base + "/settings")
        p.fill("input[name=removal_email]", "removals@example.com")
        p.fill("input[name=app_password]", "abcd efgh ijkl mnop")
        self.submit("Save")
        self.assertIn("Not saved: tick 'This address is used only for removals'", p.inner_text("main"))
        self.assertFalse(rq.inbox(self.db).get("password"))
        p.fill("input[name=app_password]", "abcd efgh ijkl mnop")
        p.check("input[name=removal_only]")
        self.submit("Save")
        self.assertEqual(rq.inbox(self.db)["password"], "abcdefghijklmnop")
        self.assertNotIn("abcdefghijklmnop", p.content())  # never shown back
        self.assertIn("A password is saved", p.inner_text("main"))
        p.fill("input[name=removal_email]", "removals2@example.com")
        self.submit("Save")  # a new address with the password box empty keeps the saved password
        self.assertEqual(rq.inbox(self.db), {"address": "removals2@example.com", "host": "smtp.gmail.com", "port": 465,
                                              "password": "abcdefghijklmnop"})
        p.goto(self.base + "/person/1/remove-all")
        self.submit("Prepare the emails")
        text = p.inner_text("main")
        n = len(rq.drafts(self.db, 1))
        self.assertIn(f"Guard Dog will email {n} companies", text)
        self.assertIn("Here's the email to Spokeo", text)
        self.assertIn("Where you list me: https://www.spokeo.com/x", text)
        self.assertIn("Also the other", text)
        smtp = mock.MagicMock()
        smtp.__enter__.return_value = smtp
        with mock.patch.object(rq.smtplib, "SMTP_SSL", return_value=smtp):
            self.submit(f"Send {n} emails")
            for _ in range(50):
                if 1 not in web.SENDING:
                    break
                time.sleep(0.1)
        self.assertEqual(smtp.send_message.call_count, n)
        p.goto(self.base + "/person/1")
        self.assertIn(f"Removal emails: {n} sent", p.inner_text("main"))
        self.assertIn(f"Sent {n} removal emails.", p.inner_text("main"))
        p.goto(self.base + "/person/1/remove-all")
        self.assertIn(f"{n} removal emails already sent", p.inner_text("main"))
        self.submit("Add them too")
        self.assertGreater(len(rq.drafts(self.db, 1)), 300)

    def test_follow_up_shows_when_due_and_someone_elses_request_is_ignored(self):
        from guard_dog import requests as rq
        self.set_password()
        self.db.save_person(None, "Me", "", names=["Jane Doe"])
        rq.save_inbox(self.db, "removals@example.com", "pw")
        rq.plan(self.db, 1)
        self.db.x("UPDATE requests SET state='sent', sent=?, follow_up=?, kind='generic'", (time.time() - 70 * 86400, time.time() - 86400))
        self.page.goto(self.base + "/person/1/remove-all")
        self.assertIn("No answer after 70 days", self.page.inner_text("main"))
        self.page.locator("button:has-text('Send a follow-up')").first.click()
        self.page.wait_for_load_state()
        self.assertGreater(len(rq.drafts(self.db, 1)), 0)
        other = self.db.save_person(None, "Mom", "she asked", names=["Mary Doe"])
        cookie = "; ".join(f"{c['name']}={c['value']}" for c in self.ctx.cookies())
        rid = self.db.q("SELECT id FROM requests WHERE state='sent' LIMIT 1")[0]["id"]
        self.raw(f"/person/{other}/followup", f"rid={rid}", {"Origin": self.base, "Cookie": cookie,
                                                            "Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(self.db.q("SELECT state FROM requests WHERE id=?", (rid,))[0]["state"], "sent")

    def test_each_site_card_says_where_its_removal_email_stands(self):
        from guard_dog import requests as rq
        self.set_password()
        self.db.save_person(None, "Me", "", names=["Jane Doe"], places=["Columbus OH"])
        scanner.record(self.db, 1, {"id": "spokeo", "name": "Spokeo"}, "https://www.spokeo.com/x", "listed",
                       ["name: Jane Doe", "city: Columbus, OH"], None)
        rq.save_inbox(self.db, "removals@example.com", "pw")
        rq.plan(self.db, 1)
        rid = self.db.q("SELECT id FROM requests WHERE broker='spokeo'")[0]["id"]
        now = time.time()

        def card(**cols):
            if cols:
                self.db.x(f"UPDATE requests SET {', '.join(k + '=?' for k in cols)} WHERE id=?", (*cols.values(), rid))
            self.page.goto(self.base + "/person/1")
            return " ".join(self.page.locator(".site", has_text="Spokeo").first.inner_text().split())
        self.assertIn("A removal email to Spokeo is ready on Remove you from all.", card())
        self.assertIn("A removal email to Spokeo goes out soon.", card(state="approved"))
        self.assertIn("No answer yet; Guard Dog reminds you in 10 days if none comes.",
                      card(state="sent", sent=now - 50 * 86400, follow_up=now + 9.5 * 86400))
        self.assertIn("Spokeo says it received the request.", card(reply="acknowledged"))
        self.assertIn("Spokeo says to use its own removal form instead of email.", card(reply="use_form"))
        self.assertIn("Spokeo turned the request down — check it: the email is in the removal inbox.", card(reply="rejected"))
        self.assertIn("No answer after 70 days — send a follow-up.",
                      card(reply="acknowledged", sent=now - 70 * 86400, follow_up=now - 86400))
        self.page.locator(".site", has_text="Spokeo").first.locator("button:has-text('Send a follow-up')").click()
        self.page.wait_for_load_state()
        self.assertIn("Remove you from all", self.page.inner_text("h1"))  # where the follow-up waits for Send
        self.assertIn("A follow-up email to Spokeo is ready", card())
        self.assertIn("The removal email to Spokeo bounced. One to their other address is waiting",
                      card(state="draft", note="Try Spokeo's other address"))
        self.assertIn("Spokeo says it's done; the next scan checks it's gone.", card(state="confirmed"))
        self.assertIn("couldn't be sent: OSError: mailbox unavailable", card(state="failed", note="OSError: mailbox unavailable"))

    def test_old_profile_copies_and_their_switch(self):
        self.set_password()
        self.db.save_person(None, "Mom", "she asked", names=["Mary Doe"], usernames=["maryd"])
        self.db.x("INSERT INTO ghosts(person, platform, username, url, snapshots, first, last, copy, found) "
                  "VALUES(1,'GitHub','maryd','https://github.com/maryd',3,'20150102','20190607',"
                  "'https://web.archive.org/web/20190607080910/https://github.com/maryd',?)", (time.time(),))
        self.page.goto(self.base + "/person/1")
        self.page.click("summary:has-text('Old profile copies in the Internet Archive (1)')")
        t = " ".join(self.page.inner_text("main").split())
        self.assertIn("GitHub: maryd", t)
        self.assertIn("3 copies, 2015 to 2019", t)
        self.assertIn("If one was Mom's, the Internet Archive removes copies on request: email info@archive.org", t)
        self.assertIn("when the account was theirs", t)
        a = self.page.locator("a:has-text('See the newest copy')")
        self.assertEqual((a.get_attribute("href"), a.get_attribute("rel")),
                         ("https://web.archive.org/web/20190607080910/https://github.com/maryd", "noreferrer"))
        self.page.goto(self.base + "/settings")
        box = self.page.locator("input[name=archive]")
        self.assertTrue(box.is_checked())
        box.uncheck()
        self.submit("Save")
        self.assertEqual(self.db.get("archive"), "0")
        self.page.goto(self.base + "/settings")
        self.page.locator("input[name=archive]").check()
        self.submit("Save")
        self.assertEqual(self.db.get("archive"), "1")

    def test_accounts_check_is_added_with_a_button_and_off_until_turned_on(self):
        from guard_dog import accounts
        p = mock.patch.object(accounts, "packaged", return_value=False)  # the install.sh edition, even in the .deb build
        p.start()
        self.addCleanup(p.stop)
        self.set_password()
        accounts.INSTALL.update(state="", note="")
        self.page.goto(self.base + "/settings")
        self.assertTrue(self.page.locator("input[name=accounts]").is_disabled())
        self.assertIn("Not added yet", self.page.inner_text("main"))
        self.assertIn("popular sites for an account", self.page.inner_text("main"))
        self.assertNotIn("pip install", self.page.inner_text("main"))  # nobody has to type a command
        with mock.patch.object(accounts, "install", side_effect=lambda: accounts.INSTALL.update(state="installing")) as inst:
            self.submit("Add it")
        inst.assert_called_once()
        self.assertIn("Adding it…", self.page.inner_text("main"))
        accounts.INSTALL.update(state="failed", note="ERROR: no space left")
        self.page.goto(self.base + "/settings")
        self.assertIn("That didn't work: ERROR: no space left", self.page.inner_text("main"))
        self.page.locator("input[name=archive]").check()
        self.submit("Save")
        self.assertEqual(self.db.get("accounts"), "0")  # not installed: can't be on
        with mock.patch.object(accounts, "installed", return_value=True):
            self.page.goto(self.base + "/settings")
            self.assertFalse(self.page.locator("button:has-text('Add it')").count())
            box = self.page.locator("input[name=accounts]")
            self.assertFalse(box.is_checked())  # off by default
            box.check()
            self.submit("Save")
            self.assertEqual(self.db.get("accounts"), "1")
            self.raw("/settings/add-accounts", "x=1", {"Origin": self.base, "Content-Type": "application/x-www-form-urlencoded",
                                                       "Cookie": "; ".join(f"{c['name']}={c['value']}" for c in self.ctx.cookies())})
        accounts.INSTALL.update(state="", note="")

    def test_accounts_with_their_usernames(self):
        self.set_password()
        self.db.save_person(None, "Me", "", names=["Jane Doe"], usernames=["janed"])
        self.db.x("INSERT INTO accounts(person, site, username, url, tags, found) VALUES(1,'Medium','janed',"
                  "'https://medium.com/@janed','blog, writing',?)", (time.time(),))
        self.page.goto(self.base + "/person/1")
        self.page.click("summary:has-text('Accounts with your usernames (1)')")
        t = " ".join(self.page.inner_text("main").split())
        self.assertIn("Medium: janed", t)
        self.assertIn("Someone else may use the same name.", t)
        a = self.page.locator("a:has-text('Open it')")
        self.assertEqual((a.get_attribute("href"), a.get_attribute("rel")), ("https://medium.com/@janed", "noreferrer"))

    def test_send_refused_when_not_ready_and_a_failure_is_an_alert(self):
        from guard_dog import requests as rq
        self.set_password()
        self.db.save_person(None, "Mom", "", names=["Mary Doe"])
        rq.plan(self.db, 1)
        cookie = "; ".join(f"{c['name']}={c['value']}" for c in self.ctx.cookies())
        o = {"Origin": self.base, "Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"}
        st, h, _ = self.raw("/person/1/send", "x=1", o)
        self.assertIn("problem=", h["Location"])
        rq.save_inbox(self.db, "removals@example.com", "pw")
        self.db.save_person(1, "Mom", "she asked 2026-10-29", names=["Mary Doe"])
        with mock.patch.object(rq.smtplib, "SMTP_SSL", side_effect=OSError("535 not accepted")):
            self.raw("/person/1/send", "x=1", o)
            for _ in range(50):
                if 1 not in web.SENDING:
                    break
                time.sleep(0.1)
        self.assertTrue(any("Couldn't send removal emails" in a["text"] for a in self.db.q("SELECT text FROM alerts")))
        web.SENDING.add(1)
        self.assertFalse(web.start_sending(self.db, 1))
        web.SENDING.discard(1)

    def test_where_they_live_now_is_saved(self):
        self.set_password()
        p = self.page
        p.goto(self.base + "/person/new")
        p.fill("textarea[name=names]", "Jane Doe")
        p.fill("input[name=home_state]", "ca")
        p.fill("input[name=home_country]", "us")
        self.submit("Save")
        me = self.db.person(1)
        self.assertEqual((me["home_state"], me["home_country"]), ("CA", "US"))


    def test_emails_that_need_you(self):
        self.set_password()
        self.db.save_person(None, "Me", "", names=["Jane Doe"])
        self.page.goto(self.base + "/inbox")
        self.assertIn("Nothing waiting", self.page.inner_text("main"))
        self.db.x("INSERT INTO mail(msgid, from_domain, subject, date, cls, broker, state, seen) VALUES(?,?,?,?,?,?,?,?)",
                  ("m1", "spokeo.com", "Your request", time.time(), "rejected", "spokeo", "needs_you", time.time()))
        self.page.goto(self.base + "/")
        self.assertIn("1 email in the removal inbox needs you.", self.page.inner_text("main"))
        self.page.goto(self.base + "/inbox")
        t = self.page.inner_text("main")
        self.assertIn("Spokeo", t)
        self.assertIn("turned the request down", t)
        self.submit("Done")
        self.assertEqual(self.db.q("SELECT state FROM mail")[0]["state"], "done")
        self.assertIn("Nothing waiting", self.page.inner_text("main"))
        self.assertEqual(self.page.locator("details").count(), 0)  # nothing ignored yet: no Details
        for i, d in enumerate(("accounts.google.com", "accounts.google.com", "news.example.org")):
            self.db.x("INSERT INTO mail(msgid, from_domain, subject, cls, state, seen) VALUES(?,?,'','unrelated','ignored',?)",
                      (f"i{i}", d, time.time()))
        self.page.goto(self.base + "/inbox")
        self.assertFalse(self.page.locator("details").first.evaluate("d => d.open"))  # folded away
        self.page.click("summary")
        self.assertIn("3 other emails ignored (not from a broker): accounts.google.com (2), news.example.org (1)",
                      self.page.inner_text("main"))


class LocalMode(unittest.TestCase):
    def test_local_only_needs_no_password(self):
        tmp = tempfile.TemporaryDirectory()
        db = DB(Path(tmp.name))
        port = free_port()
        httpd, code, local = web.build(db, "localhost", port)
        self.assertTrue(local)
        self.assertEqual(code, "")
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/settings", timeout=10) as r:
                self.assertEqual(r.status, 200)
        finally:
            httpd.shutdown()
            httpd.server_close()
            tmp.cleanup()




class LocalPassword(unittest.TestCase):
    """The packaged edition: 127.0.0.1 only, but other accounts on the same computer can open it, so it asks for a
    password too (--require-password). No setup code: its file isn't readable from a desktop account."""

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
        web.STARTED = time.time()
        web.FAILS.clear()
        self.start()

    def start(self):
        self.port = free_port()
        self.httpd, self.code, local = web.build(self.db, "127.0.0.1", self.port, require_password=True)
        self.assertTrue(local)
        self.assertEqual(self.code, "")
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.port}"
        self.ctx = self.chrome.new_context()
        self.page = self.ctx.new_page()

    def stop(self):
        self.ctx.close()
        self.httpd.shutdown()
        self.httpd.server_close()

    def tearDown(self):
        self.stop()
        web.SIGN_OUT = False
        self.tmp.cleanup()

    def set_password(self, pw="testpass99"):
        self.page.goto(self.base + "/")
        self.page.fill("input[name=pw]", pw)
        self.page.fill("input[name=pw2]", pw)
        with self.page.expect_navigation():
            self.page.click("button:has-text('Continue')")

    def test_a_first_open_asks_for_a_password_then_signs_in(self):
        r = self.page.goto(self.base + "/")
        t = self.page.inner_text("main")
        self.assertEqual(r.status, 401)
        self.assertIn("Set a password", t)
        self.assertIn("Anyone who uses this computer could open this page, so pick a password.", t)
        self.assertEqual(self.page.locator("input[name=code]").count(), 0)
        self.assertNotIn("isn't encrypted", t)
        self.assertNotIn("Sign out", self.page.inner_text("body"))  # no nav before signing in
        self.set_password()
        self.assertIn("People", self.page.inner_text("main"))
        self.assertTrue(self.db.get("password_hash"))
        other = self.chrome.new_context().new_page()  # another account on this computer, its own browser
        self.assertEqual(other.goto(self.base + "/").status, 401)
        self.assertIn("Sign in", other.inner_text("main"))
        other.fill("input[name=pw]", "wrongpass1")
        with other.expect_navigation():
            other.click("button:has-text('Continue')")
        self.assertIn("Wrong password.", other.inner_text("main"))
        web.FAILS["127.0.0.1"] = (5, time.time() + 60)
        other.fill("input[name=pw]", "testpass99")
        with other.expect_navigation():
            other.click("button:has-text('Continue')")
        self.assertRegex(other.inner_text("main"), r"Too many wrong tries\. Try again in \d+ seconds\.")
        other.context.close()

    def test_b_after_the_window_there_is_no_form_and_no_way_in(self):
        web.STARTED = time.time() - web.SETUP_WINDOW - 1  # no waiting: the window is over
        r = self.page.goto(self.base + "/")
        t = self.page.inner_text("main")
        self.assertEqual(r.status, 401)
        self.assertIn("Use Reset Guard Dog password in your apps menu", t)
        self.assertLess(t.index("Reset Guard Dog password"), t.index("restart this computer"))  # the easy way first
        self.assertEqual(self.page.locator("form").count(), 0)
        st = urllib.request.build_opener(NoRedirect)
        req = urllib.request.Request(self.base + "/login", data=b"pw=testpass99&pw2=testpass99",
                                     headers={"Origin": self.base, "Content-Type": "application/x-www-form-urlencoded"})
        try:
            code = st.open(req, timeout=10).status
        except urllib.error.HTTPError as ex:
            code = ex.code
        self.assertEqual(code, 401)
        self.assertEqual(self.db.get("password_hash") or "", "")

    def test_c_an_admin_reset_clears_only_the_password(self):
        from guard_dog import __main__ as entry
        self.set_password()
        pid = self.db.save_person(None, "Me", "", names=["Jane Doe"])
        (self.db.dir / "reset-password").write_text("")
        self.stop()
        self.assertTrue(entry.password_reset(self.db))
        self.assertFalse((self.db.dir / "reset-password").exists())
        self.assertEqual(self.db.get("password_hash"), "")
        self.assertEqual(self.db.person(pid)["names"], ["Jane Doe"])
        web.STARTED = time.time()  # the restart opens a fresh window
        self.start()
        self.page.goto(self.base + "/")
        self.assertIn("Set a password", self.page.inner_text("main"))
        self.set_password("newpass1234")
        self.assertIn("Me", self.page.inner_text("main"))  # the person is still there, under their label

    def test_d_a_restart_alone_never_resets_a_password(self):
        self.set_password()
        self.stop()
        web.STARTED = time.time()  # rebooted: the window is open again, but a password exists
        self.start()
        self.page.goto(self.base + "/")
        t = self.page.inner_text("main")
        self.assertIn("Sign in", t)
        self.assertNotIn("Set a password", t)
        self.assertEqual(self.page.locator("input[name=pw2]").count(), 0)

    def test_e_without_the_flag_local_mode_is_open_as_before(self):
        self.stop()
        self.port = free_port()
        self.httpd, _, _ = web.build(self.db, "127.0.0.1", self.port)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.ctx = self.chrome.new_context()
        self.page = self.ctx.new_page()
        r = self.page.goto(f"http://127.0.0.1:{self.port}/")
        self.assertEqual(r.status, 200)
        self.assertIn("People", self.page.inner_text("main"))
        self.assertNotIn("Sign out", self.page.inner_text("body"))

    def test_f_sign_out_ends_the_session(self):
        self.set_password()
        self.assertIn("Sign out", self.page.inner_text("header"))
        with self.page.expect_navigation():
            self.page.click("button:has-text('Sign out')")
        self.assertIn("Sign in", self.page.inner_text("main"))
        self.assertEqual(self.page.goto(self.base + "/").status, 401)
        self.assertFalse(any(c["name"] == "gd" and c["value"] for c in self.ctx.cookies()))


class BrowserDownloadSettings(WebServer):
    """The packaged edition's Settings: stealth stays off until its browser is downloaded and checked."""

    def setUp(self):
        super().setUp()
        import hashlib
        import os
        from guard_dog import browser
        from tests.test_browser import Server, fake_zip
        self.browser = browser
        self.zip = fake_zip()
        self.srv = Server({"/z.zip": self.zip})
        self.pinfile = Path(self.tmp.name) / "browser.pin"
        self.patches = [mock.patch.object(browser, "PIN", self.pinfile),
                        mock.patch.dict(os.environ, {"PLAYWRIGHT_BROWSERS_PATH": str(Path(self.tmp.name) / "browsers")})]
        for p in self.patches:
            p.start()
        browser.INSTALL.update(state="", note="", got=0, size=0)
        self.sha = hashlib.sha256(self.zip).hexdigest()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.srv.close()
        super().tearDown()

    def pin(self, sha):
        self.pinfile.write_text(f"{self.srv.base}/z.zip {sha} {len(self.zip)} 1248 156.0.8078.4 chromium-1248 chrome-linux64\n")

    def press_download(self):
        self.page.goto(self.base + "/settings")
        with self.page.expect_navigation():
            self.page.click("button:has-text('Download it'), button:has-text('Try again')")
        for _ in range(100):
            if self.browser.INSTALL["state"] != "downloading":
                break
            time.sleep(0.05)
        self.page.goto(self.base + "/settings")
        return self.page.inner_text("main")

    def test_download_then_the_switch_unlocks(self):
        self.pin(self.sha)
        self.set_password()
        self.page.goto(self.base + "/settings")
        t = self.page.inner_text("main")
        self.assertIn("Needs its hidden browser first", t)
        self.assertIn("from Google's Chrome for Testing", t)
        self.assertTrue(self.page.locator("input[name=stealth]").is_disabled())
        t = self.press_download()
        self.assertIn("Hidden browser: Chromium 156.0.8078.4, checked against its fingerprint.", t)
        self.assertFalse(self.page.locator("input[name=stealth]").is_disabled())

    def test_a_bad_download_says_so_and_offers_another_try(self):
        self.pin("0" * 64)
        self.set_password()
        t = self.press_download()
        self.assertIn("That didn't work: the download doesn't match the browser this Guard Dog was built with", t)
        self.assertEqual(self.page.locator("button:has-text('Try again')").count(), 1)
        self.assertTrue(self.page.locator("input[name=stealth]").is_disabled())

    def test_no_add_it_for_accounts_in_this_edition(self):
        from guard_dog import accounts
        self.pin(self.sha)
        self.set_password()
        accounts.INSTALL.update(state="", note="")
        with mock.patch.object(accounts, "installed", return_value=False):
            self.page.goto(self.base + "/settings")
            t = self.page.inner_text("main")
            self.assertIn("Not in this version yet: looking for accounts by username comes in a later update.", t)
            self.assertNotIn("popular sites for an account", t)  # nothing here does that, so nothing says it does
            self.assertEqual(self.page.locator("button:has-text('Add it')").count(), 0)
            self.assertTrue(self.page.locator("input[name=accounts]").is_disabled())
            cookie = "; ".join(f"{c['name']}={c['value']}" for c in self.ctx.cookies())
            with mock.patch.object(accounts, "install") as inst:
                self.raw("/settings/add-accounts", "x=1", {"Origin": self.base, "Cookie": cookie,
                                                           "Content-Type": "application/x-www-form-urlencoded"})
            inst.assert_not_called()
        self.assertEqual(accounts.INSTALL["state"], "")

    def test_sizes_are_decimal_like_files_and_app_center_show_them(self):
        real = dict(url="u", sha256="0" * 64, size=200_345_244, revision="1248", version="156.0.8078.4",
                    dir="chromium-1248", subdir="chrome-linux64")
        with mock.patch.object(self.browser, "pin", return_value=real), \
                mock.patch.object(self.browser, "installed", return_value=False):
            self.assertIn("a 200 MB download (about 420 MB on disk)", web.browser_install())
            self.browser.INSTALL.update(state="downloading", got=100_000_000)
            self.assertIn("Downloading the hidden browser… 100 of 200 MB.", web.browser_install())
        self.browser.INSTALL.update(state="", got=0)

    def test_stealth_cant_be_switched_on_before_the_browser_is_there(self):
        self.pin(self.sha)
        self.set_password()
        cookie = "; ".join(f"{c['name']}={c['value']}" for c in self.ctx.cookies())
        self.raw("/settings", "stealth=1", {"Origin": self.base, "Cookie": cookie,
                                             "Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(self.db.get("stealth"), "0")


def lan_address():
    return next((a for a in sorted(web.my_addresses()) if a.count(".") == 3 and not a.startswith("127.")), None)


class NetworkSwitch(unittest.TestCase):
    """The packaged edition's Settings switch (--network-switch): open to the network and back, no restart."""

    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.chrome = cls.pw.chromium.launch(channel="chromium")

    @classmethod
    def tearDownClass(cls):
        cls.chrome.close()
        cls.pw.stop()

    def setUp(self):
        self.lan = lan_address()
        if not self.lan:
            self.skipTest("this machine has no network address to open to")
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.tmp.name))
        self.db.set("password_hash", web.pw_hash("testpass99"))
        web.STARTED = time.time()
        web.FAILS.clear()
        self.port = free_port()
        self.started = []
        self.side = threading.Event()
        worker = threading.Thread(target=self.side.wait, daemon=True)  # stands in for a scan in progress
        self.thread = threading.Thread(target=web.serve, args=(self.db, "127.0.0.1", self.port),
                                       kwargs=dict(ready=lambda: (self.started.append(1), worker.start()),
                                                   require_password=True, switch=True), daemon=True)
        self.thread.start()
        self.worker = worker
        self.wait_for("127.0.0.1")
        self.base = f"http://127.0.0.1:{self.port}"
        self.ctx = self.chrome.new_context()
        self.page = self.ctx.new_page()

    def tearDown(self):
        self.side.set()
        self.ctx.close()
        self.tmp.cleanup()

    def reachable(self, host):
        try:
            socket.create_connection((host, self.port), timeout=1).close()
            return True
        except OSError:
            return False

    def wait_for(self, host, up=True):
        for _ in range(100):
            if self.reachable(host) == up:
                return
            time.sleep(0.05)
        self.fail(f"{host}:{self.port} {'never opened' if up else 'never closed'}")

    def get(self, host, header):
        req = urllib.request.Request(f"http://{host}:{self.port}/", headers={"Host": header})
        try:
            with RAW.open(req, timeout=5) as r:
                return r.status, r.read().decode(errors="replace")
        except urllib.error.HTTPError as ex:
            return ex.code, ex.read().decode(errors="replace")

    def toggle(self, on):
        self.page.goto(self.base + "/settings")
        box = self.page.locator("input[name=network]")
        box.check() if on else box.uncheck()
        with self.page.expect_navigation():
            self.page.click('button:has-text("Save")')
        self.assertTrue(self.page.url.endswith("/settings?switching=1"))  # a redirect: no POST left to resend
        self.assertIn("Switching over", self.page.inner_text("main"))

    def sign_in(self):
        self.page.goto(self.base + "/")
        self.page.fill("input[name=pw]", "testpass99")
        with self.page.expect_navigation():
            self.page.click("form button")

    def test_open_to_the_network_and_back(self):
        self.assertFalse(self.reachable(self.lan))
        self.sign_in()
        self.page.goto(self.base + "/settings")
        self.assertIn("Anyone on the same network as this computer", self.page.inner_text("main"))
        self.toggle(True)
        self.wait_for(self.lan)
        self.page.wait_for_url("**/settings", timeout=10000)  # the page comes back by itself
        self.page.wait_for_selector("input[name=network]:checked", timeout=10000)
        t = self.page.inner_text("main")
        self.assertIn(f"http://{self.lan}:{self.port}", t)
        self.assertIn("may not work on every phone; the IP address always does.", t)
        self.assertIn("Dashboard opened to the network", t)
        status, body = self.get(self.lan, f"{self.lan}:{self.port}")
        self.assertEqual(status, 401)
        self.assertIn("name=pw", body)  # the sign-in page, not the dashboard
        self.assertEqual(self.get(self.lan, f"evil.example:{self.port}")[0], 421)  # the allowlist was rebuilt
        self.assertTrue(self.reachable("127.0.0.1"))
        self.toggle(False)
        self.wait_for(self.lan, up=False)
        self.wait_for("127.0.0.1")
        self.assertEqual(self.db.get("network"), "0")
        self.assertEqual([a["text"] for a in self.db.q("SELECT text FROM alerts WHERE person IS NULL ORDER BY id")],
                         ["Dashboard opened to the network", "Dashboard closed to the network again"])
        self.page.wait_for_url("**/settings", timeout=10000)
        seen = []
        self.page.on("request", lambda r: seen.append(r.method))
        self.page.reload()
        self.assertEqual(seen, ["GET"])  # F5 after a switch-over asks nothing and re-sends nothing
        self.assertIn("Other devices", self.page.inner_text("main"))
        self.assertEqual(self.started, [1])  # scans started once, and the one running carried on through both
        self.assertTrue(self.worker.is_alive())
        self.assertTrue(self.thread.is_alive())


    def test_a_post_whose_redirect_nobody_follows_still_switches(self):
        self.sign_in()
        cookie = "; ".join(f"{c['name']}={c['value']}" for c in self.ctx.cookies())
        req = urllib.request.Request(self.base + "/settings", data=b"network=1", headers={
            "Origin": self.base, "Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"})
        try:
            RAW.open(req, timeout=5)
            self.fail("expected a 303 to the switch-over page")
        except urllib.error.HTTPError as ex:
            self.assertEqual(ex.code, 303)
            self.assertEqual(ex.headers["Location"], "/settings?switching=1")
        self.wait_for(self.lan)  # the 3 s fallback stopped the old server
        self.assertEqual(self.db.get("network"), "1")


class SwitchOver(unittest.TestCase):
    def test_only_once_however_many_times_it_is_asked(self):
        httpd = web.Server(("127.0.0.1", 0), web.http.server.BaseHTTPRequestHandler)
        try:
            with mock.patch.object(httpd, "shutdown") as stop:
                for _ in range(3):  # the served page, F5 on it, the fallback timer
                    httpd.switch_over()
                for _ in range(50):
                    if stop.called:
                        break
                    time.sleep(0.01)
                time.sleep(0.1)
            stop.assert_called_once()
        finally:
            httpd.server_close()


class ServeAddress(unittest.TestCase):
    """Which address serve() binds, and what happens when it can't."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.tmp.name))
        self.db.set("password_hash", "salt$hash")
        self.db.set("network", "1")

    def tearDown(self):
        self.tmp.cleanup()

    def hosts(self, side_effect=None, **kw):
        done = mock.Mock(serve_forever=lambda: None, server_close=lambda: None, rebind=False)
        with mock.patch.object(web, "build", side_effect=side_effect or (lambda *a, **k: (done, "", a[1] != web.EVERYWHERE))) as b, \
                mock.patch("builtins.print"):
            try:
                web.serve(self.db, "127.0.0.1", 8642, **kw)
            finally:
                self.calls = [c.args[1] for c in b.call_args_list]
        return self.calls

    def test_the_switch_decides_only_with_the_flag(self):
        self.assertEqual(self.hosts(), ["127.0.0.1"])  # install.sh edition: --host, whatever the setting says
        self.assertEqual(self.hosts(switch=True), ["0.0.0.0"])
        self.db.set("network", "0")
        self.assertEqual(self.hosts(switch=True), ["127.0.0.1"])

    def test_never_open_without_a_password(self):
        self.db.set("password_hash", "")
        self.assertEqual(self.hosts(switch=True), ["127.0.0.1"])

    def test_cant_open_falls_back_to_this_computer(self):
        done = mock.Mock(serve_forever=lambda: None, server_close=lambda: None, rebind=False)

        def build(db, host, *a, **k):
            if host == web.EVERYWHERE:
                raise OSError(98, "Address already in use")
            return done, "", True
        self.assertEqual(self.hosts(build, switch=True), ["0.0.0.0", "127.0.0.1"])
        self.assertEqual(self.db.get("network"), "0")
        self.assertEqual(self.db.q("SELECT text FROM alerts WHERE person IS NULL")[0]["text"],
                         "Couldn't open the dashboard to the network (Address already in use); it's on this computer only")

    def test_if_this_computer_fails_too_it_stops_once(self):
        def build(*a, **k):
            raise OSError(98, "Address already in use")
        with self.assertRaises(OSError):
            self.hosts(build, switch=True)
        self.assertEqual(self.calls, ["0.0.0.0", "127.0.0.1"])  # one try each, then __main__'s one-line exit


if __name__ == "__main__":
    unittest.main()

"""check_page branches, the browser pool, in_parallel, the scheduler and a whole scan — no network."""
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from guard_dog import scanner
from guard_dog.db import DB

PERSON = {"names": ["Jane Doe"], "emails": ["jane@example.com"], "phones": ["(330) 555-0142"],
          "places": ["Columbus, OH"], "usernames": []}
CHECKED = {"id": "site", "name": "Site", "checked": "2026-10-29", "blocks_bots": False}
UNCHECKED = dict(CHECKED, checked=None)
BOTTY = dict(CHECKED, blocks_bots=True)
LISTING = "Jane Doe, 52, Columbus OH. Phone 330-555-0142. " + "More about her. " * 300
NOBODY = "Search results: no results found for that name. " + "Try again. " * 300
CHALLENGE = "Just a moment... verifying you are human"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.tmp.name))
        self.pid = self.db.save_person(None, "T", "", names=PERSON["names"], emails=PERSON["emails"],
                                       phones=PERSON["phones"], places=PERSON["places"])

    def tearDown(self):
        self.tmp.cleanup()

    def rows(self):
        return [dict(r) for r in self.db.q("SELECT * FROM findings")]


class CheckPage(Base):
    def run_with(self, answers, b=CHECKED, stealth=False):
        """answers: {(stealth, solve): (status, text)} or an exception; returns (result, fetch calls)."""
        calls = []

        def fake(url, db, st=None, solve=False):
            calls.append((bool(st), solve))
            a = answers[(bool(st), solve)]
            if isinstance(a, Exception):
                raise a
            return a[0], f"<html>{a[1]}</html>", a[1], "fake", a[2] if len(a) > 2 else ""
        with mock.patch.object(scanner, "fetch", fake):
            r = scanner.check_page(self.db, self.pid, PERSON, b, "https://x/1", "Jane Doe", "Columbus, OH", stealth)
        return r, calls

    def test_plain_listing_saves_a_copy(self):
        r, calls = self.run_with({(False, False): (200, LISTING)})
        self.assertEqual((r, calls), ("listed", [(False, False)]))
        self.assertTrue(self.rows()[0]["copy"])

    # Shaped like the captcha TruePeopleSearch and FamilyTreeNow served PiCloud on 2026-10-30 (no one's details in it)
    CAPTCHA = ("Captcha True PeopleSearch Name Phone Address Email Neighbors Search Automatic submission failed. "
               "Please click the button below to try again. Submit Loading content, please wait... Find people fast "
               "with the most comprehensive public records search available. About Us Help Center Terms Privacy")

    def test_the_automatic_submission_captcha_is_a_block_not_a_miss(self):
        r, calls = self.run_with({(False, False): (200, self.CAPTCHA), (True, False): (200, self.CAPTCHA),
                                  (True, True): (200, self.CAPTCHA)}, stealth=True)
        self.assertEqual((r, calls), ("blocked", [(False, False), (True, False), (True, True)]))
        self.assertEqual(self.run_with({(False, False): (200, self.CAPTCHA)})[0], "needs stealth")

    def test_a_short_page_titled_captcha_is_a_block(self):
        self.assertTrue(scanner.blocked_page(200, "Captcha\nOne moment while we check your request."))
        self.assertFalse(scanner.blocked_page(200, "Results for Jane Doe. " + "captcha " + "x" * scanner.SHORT_PAGE))

    def test_a_listing_that_seems_gone_keeps_the_page_as_evidence(self):
        self.run_with({(False, False): (200, LISTING)})
        first = self.rows()[0]["copy"]
        r, _ = self.run_with({(False, False): (200, NOBODY)})
        row = self.rows()[0]
        self.assertEqual((r, row["status"], row["check_note"]), ("not found", "not found", scanner.UNSEEN))
        self.assertTrue(row["copy"] and row["copy"] != first)  # what the site showed when it seemed gone

    def test_a_miss_on_a_never_listed_site_keeps_no_copy(self):
        self.assertEqual(self.run_with({(False, False): (200, NOBODY)})[0], "not found")
        self.assertFalse(self.rows()[0]["copy"])

    def test_block_page_with_200_and_stealth_off(self):
        self.assertEqual(self.run_with({(False, False): (200, CHALLENGE)})[0], "needs stealth")

    def test_403_stealth_off(self):
        self.assertEqual(self.run_with({(False, False): (403, "Forbidden")})[0], "needs stealth")

    def test_plain_blocked_then_stealth_gets_through(self):
        r, calls = self.run_with({(False, False): (403, "no"), (True, False): (200, LISTING)}, stealth=True)
        self.assertEqual((r, calls), ("listed", [(False, False), (True, False)]))

    def test_bot_blocking_site_goes_straight_to_stealth(self):
        r, calls = self.run_with({(True, False): (200, LISTING)}, b=BOTTY, stealth=True)
        self.assertEqual((r, calls), ("listed", [(True, False)]))

    def test_solver_only_after_stealth_is_blocked(self):
        r, calls = self.run_with({(True, False): (200, CHALLENGE), (True, True): (200, LISTING)}, b=BOTTY, stealth=True)
        self.assertEqual((r, calls), ("listed", [(True, False), (True, True)]))

    def test_blocked_even_with_solver(self):
        r, _ = self.run_with({(True, False): (403, "x"), (True, True): (403, "x")}, b=BOTTY, stealth=True)
        self.assertEqual(r, "blocked")

    def test_404_checked_vs_unconfirmed(self):
        self.assertEqual(self.run_with({(False, False): (404, "gone")})[0], "not found")
        self.db.x("DELETE FROM findings")
        r, _ = self.run_with({(False, False): (404, "gone")}, b=UNCHECKED)
        self.assertEqual(r, "unconfirmed")
        self.assertIn("isn't confirmed", self.rows()[0]["check_note"])

    def test_results_page_without_them_checked_vs_unconfirmed(self):
        self.assertEqual(self.run_with({(False, False): (200, NOBODY)})[0], "not found")
        self.db.x("DELETE FROM findings")
        self.assertEqual(self.run_with({(False, False): (200, NOBODY)}, b=UNCHECKED)[0], "unconfirmed")

    def test_other_status_is_an_error(self):
        self.assertEqual(self.run_with({(False, False): (500, "oops " * 1000)})[0], "error")

    def test_exception_is_an_error_with_reason(self):
        r, _ = self.run_with({(False, False): ConnectionError("reset")})
        self.assertEqual(r, "error")
        self.assertIn("reset", self.rows()[0]["check_note"])


class FakeSession:
    live, made, closed = 0, 0, 0
    lock = threading.Lock()

    def __init__(self, solve):
        with FakeSession.lock:
            FakeSession.made += 1
        self.solve = solve
        self.busy = False
        self.owner = threading.get_ident()  # like Playwright: a browser belongs to the thread that started it

    def fetch(self, url):
        assert threading.get_ident() == self.owner, "a browser was used from a thread that didn't start it"
        assert not self.busy, "a browser was lent to two threads at once"
        self.busy = True
        with FakeSession.lock:
            FakeSession.live += 1
            self.peak = FakeSession.live
            BrowsersTest.peaks.append(FakeSession.live)
        time.sleep(0.02)
        with FakeSession.lock:
            FakeSession.live -= 1
        self.busy = False
        if url == "boom":
            raise RuntimeError("page crashed")
        return url

    def close(self):
        assert threading.get_ident() == self.owner, "a browser was closed from a thread that didn't start it"
        with FakeSession.lock:
            FakeSession.closed += 1
        if self.solve == "bad-close":
            raise RuntimeError("won't close")


class BrowsersTest(Base):
    peaks = []

    def setUp(self):
        super().setUp()
        FakeSession.live = FakeSession.made = FakeSession.closed = 0
        BrowsersTest.peaks = []

    def test_pool_size_respected_and_everything_closed(self):
        pool = scanner.Browsers(self.db, 2, factory=FakeSession)
        groups = [[f"u{g}-{i}" for i in range(4)] for g in range(6)]
        errs = scanner.in_parallel(groups, lambda u: pool.fetch(u, False), 6, lambda: None, lambda: False, pool)
        self.assertEqual(errs, [])
        self.assertLessEqual(FakeSession.made, 2)
        self.assertLessEqual(max(self.peaks), 2)
        self.assertEqual(FakeSession.closed, FakeSession.made)

    def test_solver_pool_is_one_browser(self):
        pool = scanner.Browsers(self.db, 4, factory=FakeSession)
        scanner.in_parallel([[f"a{i}"] for i in range(5)], lambda u: pool.fetch(u, True), 5, lambda: None,
                            lambda: False, pool)
        self.assertEqual(FakeSession.made, 1)

    def test_errors_are_returned_and_browsers_still_closed(self):
        pool = scanner.Browsers(self.db, 2, factory=FakeSession)
        errs = scanner.in_parallel([["ok", "boom"], ["ok2"]], lambda u: pool.fetch(u, False), 2, lambda: None,
                                   lambda: False, pool)
        self.assertEqual([e[0] for e in errs], ["boom"])
        self.assertIn("page crashed", errs[0][1])
        self.assertEqual(FakeSession.closed, FakeSession.made)

    def test_failed_browser_start_frees_its_slot(self):
        n = [0]

        def flaky(solve):
            n[0] += 1
            if n[0] == 1:
                raise RuntimeError("chromium missing")
            return FakeSession(solve)
        pool = scanner.Browsers(self.db, 1, factory=flaky)
        with self.assertRaises(RuntimeError):
            pool.fetch("a", False)
        self.assertEqual(pool.fetch("b", False), "b")  # would hang forever if the slot leaked
        self.assertEqual(pool.close(), [])

    def test_a_browser_whose_page_crashed_is_replaced(self):
        made = []

        def factory(solve):
            made.append(FakeSession(solve))
            return made[-1]
        pool = scanner.Browsers(self.db, 1, factory=factory)
        with self.assertRaises(RuntimeError):
            pool.fetch("boom", False)
        self.assertEqual(pool.fetch("ok", False), "ok")
        self.assertEqual(len(made), 2)  # a fresh browser, not the crashed one
        self.assertEqual(FakeSession.closed, 1)  # the crashed one was closed at once
        self.assertEqual(pool.close(), [])

    def test_crashed_browser_that_wont_close_is_reported(self):
        pool = scanner.Browsers(self.db, 1, factory=lambda solve: FakeSession("bad-close"))
        with self.assertRaises(RuntimeError):
            pool.fetch("boom", False)
        self.assertEqual(pool.close()[0][0], "closing a browser")

    def test_close_error_is_reported(self):
        pool = scanner.Browsers(self.db, 1, factory=lambda solve: FakeSession("bad-close"))
        pool.fetch("a", False)
        self.assertEqual(pool.close()[0][0], "closing a browser")

    def test_a_page_that_never_finishes_is_stopped_and_replaced(self):
        """PiCloud 2026-10-29: one Google page ran 2 hours and took the Pi's memory with it."""
        made, killed = [], []

        class Hangs(FakeSession):
            def __init__(self, solve):
                super().__init__(solve)
                self.gone = threading.Event()

            def fetch(self, url):
                if url == "hang":
                    self.gone.wait(10)
                    raise RuntimeError("Playwright connection closed")
                return url

            def close(self):
                super().close()
                if self.gone.is_set():
                    raise RuntimeError("Playwright connection closed")  # killed on purpose: not news

        def factory(solve):
            made.append(Hangs(solve))
            return made[-1]

        def killer(sess):
            killed.append(sess)
            sess.gone.set()
            return True
        pool = scanner.Browsers(self.db, 1, factory=factory, killer=killer)
        with mock.patch.object(scanner, "CHECK_EVERY", 0.01), mock.patch.object(scanner, "FETCH_LIMIT", 0.05):
            with self.assertRaisesRegex(TimeoutError, "didn't finish within"):
                pool.fetch("hang", False)
            self.assertEqual(killed, [made[0]])
            self.assertEqual(pool.fetch("next", False), "next")
        self.assertEqual(len(made), 2)  # a fresh browser for the next page
        self.assertEqual(pool.close(), [])

    def test_low_memory_stops_the_page(self):
        stop = threading.Event()

        class Hangs(FakeSession):
            def fetch(self, url):
                stop.wait(10)
                raise RuntimeError("gone")
        pool = scanner.Browsers(self.db, 1, factory=Hangs, killer=lambda sess: stop.set() or True)
        with mock.patch.object(scanner, "CHECK_EVERY", 0.01), mock.patch.object(scanner, "free_memory_mb", return_value=150):
            with self.assertRaisesRegex(TimeoutError, r"memory ran low \(150 MB free\)"):
                pool.fetch("x", False)
        self.assertEqual(pool.close(), [])

    def test_low_memory_stops_one_page_per_check_the_longest_first(self):
        """On a small machine every page can be legitimately working when memory runs low: stop one, not all."""
        gates = {"older": threading.Event(), "newer": threading.Event()}
        free, killed, out = [150], [], {}

        class Hangs(FakeSession):
            def fetch(self, url):
                self.url = url
                if gates[url].wait(10) and url in killed:
                    raise RuntimeError("gone")
                return url

        def kill(sess):
            killed.append(sess.url)
            free[0] = 2000  # stopping one page freed enough
            gates[sess.url].set()
            return True

        def run(url):
            try:
                out[url] = pool.fetch(url, False)
            except TimeoutError as ex:
                out[url] = str(ex)
        pool = scanner.Browsers(self.db, 2, factory=Hangs, killer=kill)
        with mock.patch.object(scanner, "CHECK_EVERY", 0.02), \
                mock.patch.object(scanner, "free_memory_mb", side_effect=lambda: free[0]):
            with mock.patch.object(scanner, "free_memory_mb", return_value=2000):  # both pages start while memory is fine
                a = threading.Thread(target=run, args=("older",))
                a.start()
                time.sleep(0.1)
                b = threading.Thread(target=run, args=("newer",))
                b.start()
                time.sleep(0.1)
            a.join(5)  # memory is low now: the older page goes first
            time.sleep(0.2)  # and memory is fine again, so the newer page is left alone
            self.assertNotIn("newer", out)
            gates["newer"].set()
            b.join(5)
        self.assertEqual(killed, ["older"])
        self.assertEqual(out, {"older": "memory ran low (150 MB free)", "newer": "newer"})
        self.assertEqual(pool.close(), [])

    def test_a_browser_whose_process_cant_be_found_is_reported(self):
        stop = threading.Event()

        class Hangs(FakeSession):
            def fetch(self, url):
                stop.wait(10)
                return url
        pool = scanner.Browsers(self.db, 1, factory=Hangs, killer=lambda sess: False)
        with mock.patch.object(scanner, "CHECK_EVERY", 0.01), mock.patch.object(scanner, "FETCH_LIMIT", 0.02):
            with self.assertRaises(TimeoutError):
                pool.fetch("x", False)
        stop.set()
        self.assertEqual(pool.close(), [("stopping a page", "couldn't find the browser's process")])

    def test_a_browser_that_wont_let_go_is_reported(self):
        stop = threading.Event()

        class Stuck(FakeSession):
            def fetch(self, url):
                stop.wait(10)
                return url
        pool = scanner.Browsers(self.db, 1, factory=Stuck, killer=lambda sess: True)
        with mock.patch.object(scanner, "CHECK_EVERY", 0.01), mock.patch.object(scanner, "FETCH_LIMIT", 0.02), \
                mock.patch.object(scanner, "LET_GO", 0.05):
            with self.assertRaises(TimeoutError):
                pool.fetch("x", False)
        stop.set()
        self.assertEqual(pool.errors[0][0], "stopping a page")

    def test_waiting_in_line_doesnt_count_against_the_limit(self):
        release = threading.Event()

        class Slow(FakeSession):
            def fetch(self, url):
                if url == "first":
                    release.wait(10)
                return url
        pool = scanner.Browsers(self.db, 1, factory=Slow, killer=lambda sess: self.fail("nothing should be stopped"))
        out = []
        with mock.patch.object(scanner, "CHECK_EVERY", 0.01), mock.patch.object(scanner, "FETCH_LIMIT", 0.3):
            t = threading.Thread(target=lambda: out.append(pool.fetch("second", False)))
            first = threading.Thread(target=lambda: out.append(pool.fetch("first", False)))
            first.start()
            time.sleep(0.05)
            t.start()
            time.sleep(0.2)
            release.set()  # first took 0.25 s; second waited behind it and then ran at once
            first.join(5)
            t.join(5)
        self.assertEqual(sorted(out), ["first", "second"])
        pool.close()

    def test_kill_browser_kills_the_driver_and_everything_under_it(self):
        import subprocess
        proc = subprocess.Popen(["sh", "-c", "sleep 60 & sleep 60 & wait"])
        time.sleep(0.2)
        sess = mock.Mock()
        sess.playwright._impl_obj._connection._transport._proc.pid = proc.pid
        kids = [int(d.name) for d in Path("/proc").glob("[0-9]*")
                if (d / "stat").exists() and f") S {proc.pid} " in (d / "stat").read_text()]
        self.assertEqual(len(kids), 2)
        self.assertTrue(scanner.kill_browser(sess))
        proc.wait(5)
        time.sleep(0.2)
        for k in kids:
            self.assertFalse(Path(f"/proc/{k}").exists() and "Z" not in Path(f"/proc/{k}/stat").read_text().split()[2])
        self.assertFalse(scanner.kill_browser(object()))  # not a Playwright session: nothing to kill

    def test_free_memory(self):
        with mock.patch.object(scanner.Path, "read_text", return_value="MemTotal: 8000000 kB\nMemAvailable: 1048576 kB\n"):
            self.assertEqual(scanner.free_memory_mb(), 1024)
        with mock.patch.object(scanner.Path, "read_text", side_effect=OSError):
            self.assertIsNone(scanner.free_memory_mb())
        with mock.patch.object(scanner.Path, "read_text", return_value="MemTotal: 8000000 kB\n"):
            self.assertIsNone(scanner.free_memory_mb())

    def test_real_chromium_from_many_scan_threads_and_nothing_left_running(self):
        """The bug PiCloud found: real Playwright browsers only work from the thread that started them."""
        import http.server
        import subprocess

        from playwright.sync_api import sync_playwright

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(s):
                s.send_response(200)
                s.send_header("Content-Type", "text/html")
                s.end_headers()
                s.wfile.write(f"<title>{s.path}</title>".encode())

            def log_message(s, *a):
                pass
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"

        class Real:  # a real Chromium, held the way StealthySession holds one
            def __init__(self, solve):
                self.pw = sync_playwright().start()
                self.browser = self.pw.chromium.launch(channel="chromium")  # the full browser, as the app uses (no headless shell needed)
                self.page = self.browser.new_page()

            def fetch(self, url):
                self.page.goto(url)
                return self.page.title()

            def close(self):
                self.browser.close()
                self.pw.stop()

        def chromiums():
            out = subprocess.run(["pgrep", "-c", "-P", str(__import__("os").getpid())], capture_output=True, text=True)
            return int(out.stdout.strip() or 0)
        before = chromiums()
        pool = scanner.Browsers(self.db, 2, factory=Real)
        got = []
        errs = scanner.in_parallel([[f"/{g}-{i}" for i in range(3)] for g in range(6)],
                                   lambda u: got.append(pool.fetch(base + u, False)), 6, lambda: None, lambda: False, pool)
        srv.shutdown()
        srv.server_close()
        self.assertEqual(errs, [])
        self.assertEqual(sorted(got), sorted(f"/{g}-{i}" for g in range(6) for i in range(3)))
        time.sleep(0.5)
        self.assertLessEqual(chromiums(), before)  # every browser this run started is gone

    def test_real_stealth_browser_on_a_page_that_never_stops(self):
        """The production path end to end: a real Scrapling browser stuck in while(true){} is stopped at the limit,
        the next page works in a fresh browser and nothing is left running."""
        import http.server

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(s):
                s.send_response(200)
                s.send_header("Content-Type", "text/html")
                s.end_headers()
                s.wfile.write(b"<p>spin</p><script>while(true){}</script>" if s.path == "/hang" else b"<title>ok</title>fine")

            def log_message(s, *a):
                pass

        def under_me():
            kids = {}
            for d in Path("/proc").glob("[0-9]*"):
                try:
                    kids.setdefault(int((d / "stat").read_text().rsplit(")", 1)[1].split()[1]), []).append(int(d.name))
                except (OSError, ValueError, IndexError):
                    pass
            out, todo = [], [__import__("os").getpid()]
            while todo:
                for k in kids.get(todo.pop(), []):
                    out.append(k)
                    todo.append(k)
            return out
        pool = scanner.Browsers(self.db, 1)
        try:
            pool._make(False).close()
        except Exception as ex:  # no Chromium installed here (scrapling install not run): nothing to prove with
            self.skipTest(f"no stealth browser: {type(ex).__name__}")
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        before = len(under_me())
        try:
            with mock.patch.object(scanner, "FETCH_LIMIT", 15):
                t0 = time.monotonic()
                with self.assertRaisesRegex(TimeoutError, "didn't finish within 15 s"):
                    pool.fetch(base + "/hang", False)
                self.assertLess(time.monotonic() - t0, 15 + 2 * scanner.CHECK_EVERY + 5)
                page = pool.fetch(base + "/ok", False)
                self.assertEqual((page.status, page.css("title::text").get()), (200, "ok"))
        finally:
            errors = pool.close()
            srv.shutdown()
            srv.server_close()
        self.assertEqual(errors, [])
        time.sleep(1)
        self.assertLessEqual(len(under_me()), before)

    def test_pause_only_between_pages_of_one_site_and_cancel_stops(self):
        log = []
        scanner.in_parallel([["a1", "a2", "a3"]], log.append, 1, lambda: log.append("pause"), lambda: False)
        self.assertEqual(log, ["a1", "pause", "a2", "pause", "a3"])
        log.clear()
        scanner.in_parallel([["a1", "a2"]], log.append, 1, lambda: None, lambda: "a1" in log)
        self.assertEqual(log, ["a1"])

    def test_stealth_outside_a_scan_uses_a_one_off_browser(self):
        scanner._tls.browsers = None
        with mock.patch("scrapling.fetchers.StealthyFetcher.fetch", return_value="one-off") as f, \
                mock.patch.object(scanner, "NO_NETWORK", False):
            self.assertEqual(scanner.get("https://x", self.db, True), "one-off")
            f.assert_called_once()

    def test_no_network_guard(self):
        with mock.patch.object(scanner, "NO_NETWORK", True):
            with self.assertRaises(RuntimeError):
                scanner.get("https://x", self.db, False)


class Schedule(Base):
    def test_never_first_scan_and_longest_waiting_first(self):
        now = time.time()
        self.assertIsNone(scanner.due(self.db, now))  # never scanned
        a = self.db.save_person(None, "A", "", names=["A B"])
        b = self.db.save_person(None, "B", "", names=["C D"])
        self.db.x("UPDATE people SET last_scan=? WHERE id=?", (now - 10 * 3600, a))
        self.db.x("UPDATE people SET last_scan=? WHERE id=?", (now - 20 * 3600, b))
        self.assertEqual(scanner.due(self.db, now), b)
        self.db.set("rescan_hours", "0")
        self.assertIsNone(scanner.due(self.db, now))
        self.db.set("rescan_hours", "48")
        self.assertIsNone(scanner.due(self.db, now))

    def test_number_bounds(self):
        for v, want in (("inf", 6), ("nan", 6), ("-3", 0.0), ("99999", 720), ("junk", 6), ("12", 12.0)):
            self.db.set("rescan_hours", v)
            self.assertEqual(scanner.number(self.db, "rescan_hours", 6, 720), want, v)


class Copies(Base):
    def test_odd_source_names_stay_inside_copies(self):
        for src in ("exa mple.com:8443", "пример.рф", "../../etc", "a/b\\c"):
            name = scanner.save_copy(self.db, self.pid, src, "https://x", "<p>hi</p>")
            self.assertNotIn("/", name)
            self.assertNotIn("\\", name)
            self.assertTrue((self.db.dir / "copies" / name).is_file())


class WholeScan(Base):
    def test_scan_end_to_end(self):
        self.db.set("delay_min", "0")
        self.db.set("delay_max", "0")
        self.db.set("stealth", "0")
        hits = [dict(url="https://blog.example/jane", domain="blog.example", name="Jane Doe", place="", query='"x"',
                     engine="bing")]
        pages = {"https://blog.example/jane": (200, LISTING)}

        def fake_fetch(url, db, st=None, solve=False):
            s, t = pages.get(url, (200, NOBODY))
            return s, t, t, "fake", ""
        with mock.patch.object(scanner, "fetch", fake_fetch), \
                mock.patch.object(scanner.sweep, "search", return_value=(hits, {"brave": ["http 429", "http 429"]})), \
                mock.patch.object(scanner.breaches, "xposedornot",
                                  return_value=[dict(breach="B1", domain="b.com", date="2024", exposed="Passwords",
                                                     password_risk="plaintext")]):
            scanner.run(self.db, self.pid)
        st = {(r["kind"], r["status"]) for r in self.rows()}
        self.assertIn(("web", "listed"), st)
        self.assertIn(("broker", "needs stealth"), st)
        self.assertEqual(self.db.q("SELECT state FROM scans")[0]["state"], "done")
        texts = [a["text"] for a in self.db.q("SELECT text FROM alerts")]
        self.assertTrue(any(t.startswith("web search: brave failed 2") for t in texts), texts)
        self.assertTrue(any("new listing" in t for t in texts))
        self.assertFalse(any("new breach" in t for t in texts))  # first scan: breaches aren't "new"
        self.assertEqual(self.db.q("SELECT COUNT(*) n FROM breaches")[0]["n"], 1)

    def test_breach_check_failure_is_an_alert(self):
        self.db.set("sweep", "0")
        self.db.set("delay_max", "0")
        with mock.patch.object(scanner, "fetch", lambda *a, **k: (404, "", "", "fake", "")), \
                mock.patch.object(scanner.breaches, "xposedornot", side_effect=RuntimeError("XposedOrNot answered 500")):
            scanner.run(self.db, self.pid)
        self.assertTrue(any("breach check for jane@example.com failed" in a["text"]
                            for a in self.db.q("SELECT text FROM alerts")))

    def test_breach_progress_names_no_address(self):
        """Progress text lands in load logs and journals: it counts emails, it doesn't print them."""
        self.db.set("sweep", "0")
        self.db.set("delay_max", "0")
        seen = []
        with mock.patch.object(scanner, "fetch", lambda *a, **k: (404, "", "", "fake", "")), \
                mock.patch.object(scanner.breaches, "xposedornot",
                                  side_effect=lambda e: seen.append(self.db.q("SELECT progress FROM scans")[0]["progress"]) or []):
            scanner.run(self.db, self.pid)
        self.assertEqual(seen, ["breach check 1/1"])

    def test_delete_during_scan_cleans_up(self):
        self.db.set("sweep", "0")
        self.db.set("delay_max", "0")
        started = threading.Event()

        def slow(*a, **k):
            started.set()
            time.sleep(0.2)
            return 404, "", "", "fake", ""
        with mock.patch.object(scanner, "fetch", slow), mock.patch.object(scanner.breaches, "xposedornot", return_value=[]):
            self.assertTrue(scanner.start(self.db, self.pid))
            self.assertFalse(scanner.start(self.db, self.pid))  # one scan per person at a time
            started.wait(5)
            scanner.delete(self.db, self.pid)
            for _ in range(100):
                if not scanner.running(self.pid):
                    break
                time.sleep(0.05)
        self.assertFalse(scanner.running(self.pid))
        self.assertEqual(self.db.q("SELECT COUNT(*) n FROM findings")[0]["n"], 0)
        self.assertIsNone(self.db.person(self.pid))


if __name__ == "__main__":
    unittest.main()


class QuietLogs(unittest.TestCase):
    """VM 2026-10-30: the journal held "Fetched (200) <GET https://www.google.com/search?q=%22Zqxv+Testerson%22...>"."""
    URL = "https://www.fastpeoplesearch.com/name/zqxv-testerson_columbus-oh?x=1&email=jane%40example.com"

    def setUp(self):
        import io
        import logging
        scanner.quiet_logs()
        scanner.quiet_logs()  # twice: one filter per handler, one root handler
        self.out = io.StringIO()
        self.handlers = [h for h in logging.getLogger("scrapling").handlers + logging.getLogger().handlers
                         if any(isinstance(f, scanner.NoQueries) for f in h.filters)]
        self.streams = [h.setStream(self.out) for h in self.handlers]

    def tearDown(self):
        for h, st in zip(self.handlers, self.streams):
            h.setStream(st)

    def assertClean(self, text):
        for leak in ("zqxv", "testerson", "columbus", "jane", "x=1", "/name/", "/search"):
            self.assertNotIn(leak, text.lower())

    def test_one_filter_each_and_info_is_gone(self):
        import logging
        for h in self.handlers:
            self.assertEqual(sum(isinstance(f, scanner.NoQueries) for f in h.filters), 1)
        self.assertEqual(len([h for h in logging.getLogger().handlers if any(isinstance(f, scanner.NoQueries) for f in h.filters)]), 1)
        logging.getLogger("scrapling").info("Fetched (200) <GET https://www.google.com/search?q=%22Zqxv+Testerson%22>")
        self.assertEqual(self.out.getvalue(), "")

    def test_no_address_past_the_site_from_any_logger(self):
        import logging
        logging.getLogger("scrapling").warning("Attempt 1 failed: Page.goto: Timeout 90000ms exceeded.\nCall log:\n"
                                               '  - navigating to "%s"', self.URL)
        logging.getLogger("scrapling").warning("%s blocked <GET %s>", "Bing", "https://www.bing.com/search?q=jane%40example.com")
        logging.getLogger("scrapling.engines").warning("child logger: %s", self.URL)  # the gap the verifier found
        logging.getLogger("patchright").warning("driver: %s", self.URL)
        text = self.out.getvalue()
        self.assertClean(text)
        self.assertIn('navigating to "https://www.fastpeoplesearch.com/[…]"', text)
        self.assertIn("Bing blocked <GET https://www.bing.com/[…]>", text)
        self.assertIn("child logger: https://www.fastpeoplesearch.com/[…]", text)
        self.assertIn("driver: https://www.fastpeoplesearch.com/[…]", text)
        self.assertEqual(text.count("child logger"), 1)  # printed once, not again by the root

    def test_tracebacks_are_cut_too(self):
        import logging
        try:
            raise RuntimeError(f"Page.goto: net::ERR_ABORTED at {self.URL}")
        except RuntimeError:
            logging.getLogger("scrapling.engines").exception("fetch failed")
        url = self.URL  # noqa: F841  in the frame the stack shows
        logging.getLogger("patchright").warning("where: " + self.URL, stack_info=True)
        text = self.out.getvalue()
        self.assertIn("Stack (most recent call last)", text)
        self.assertIn("RuntimeError: Page.goto: net::ERR_ABORTED at https://www.fastpeoplesearch.com/[…]", text)
        self.assertClean(text)


class MemorySizedPools(Base):
    """Item 3: browsers sized to free memory (measured 2026-10-30: 1.5 GB typical, 3.5 GB worst per browser)."""

    def count(self, free, setting="2"):
        self.db.set("stealth_workers", setting)
        with mock.patch.object(scanner, "free_memory_mb", return_value=free):
            return scanner.browser_count(self.db)

    def test_count(self):
        self.assertEqual(self.count(3600), (1, 2, 3600))   # 4 GB machine: one browser (single mode)
        self.assertEqual(self.count(7200), (1, 2, 7200))   # 8 GB Pi
        self.assertEqual(self.count(7500), (1, 2, 7500))   # 8 GB Pi as scan 11 found it: not the 3 that reached 509 MB
        self.assertEqual(self.count(16000), (2, 3, 16000))  # (16000 - 1500) // 2500 = 5, capped at the setting + solver
        self.assertEqual(self.count(23000, "4"), (4, 5, 23000))
        self.assertEqual(self.count(500), (1, 2, 500))     # never none
        self.assertEqual(self.count(None, "3"), (3, 4, None))  # no /proc/meminfo: the owner's setting

    def test_each_stealth_phase_says_how_many_and_sizes_its_pool(self):
        self.db.set("stealth", "1")
        self.db.set("delay_max", "0")
        for free, line, pool in ((6246, "1 browser, 6.1 GB free", (1, True)),       # the 8 GB Pi: one browser does both
                                 (11000, "3 browsers, 10.7 GB free", (2, False))):   # 2 plain and the solver
            progress, pools = [], []
            real_x = self.db.x

            def x(sql, args=()):
                if sql.startswith("UPDATE scans SET progress"):
                    progress.append(args[0])
                return real_x(sql, args)

            class Pool:
                def __init__(self, db, size, single=False):
                    pools.append((size, single))

                def close(self):
                    return []
            with mock.patch.object(self.db, "x", x), mock.patch.object(scanner, "Browsers", Pool), \
                    mock.patch.object(scanner, "free_memory_mb", return_value=free), \
                    mock.patch.object(scanner, "check_page"), mock.patch.object(scanner.breaches, "xposedornot", return_value=[]), \
                    mock.patch.object(scanner.sweep, "search", return_value=([], {})):
                scanner.run(self.db, self.pid)
            self.assertEqual(progress.count(line), 3, progress)  # people-search sites, web search, web pages
            self.assertEqual(pools, [pool] * 3)

    def test_plain_scan_says_nothing_about_browsers(self):
        self.db.set("stealth", "0")
        self.db.set("delay_max", "0")
        with mock.patch.object(scanner, "check_page"), mock.patch.object(scanner.breaches, "xposedornot", return_value=[]), \
                mock.patch.object(scanner.sweep, "search", return_value=([], {})):
            scanner.run(self.db, self.pid)
        self.assertNotIn("browsers", self.db.q("SELECT progress FROM scans")[0]["progress"])

    def test_a_page_waits_while_memory_is_short_and_another_is_loading(self):
        release, order, free = threading.Event(), [], [5000]

        class Slow(FakeSession):
            def fetch(self, url):
                order.append(f"start {url}")
                if url == "first":
                    release.wait(10)
                order.append(f"end {url}")
                return url
        pool = scanner.Browsers(self.db, 2, factory=Slow)
        with mock.patch.object(scanner, "CHECK_EVERY", 0.01), mock.patch.object(scanner, "free_memory_mb", side_effect=lambda: free[0]):
            a = threading.Thread(target=pool.fetch, args=("first", False))
            a.start()
            time.sleep(0.1)
            free[0] = 1500  # under 400 + 2000 now
            b = threading.Thread(target=pool.fetch, args=("second", False))
            b.start()
            time.sleep(0.2)
            self.assertEqual(order, ["start first"])  # second is waiting, though a browser is free
            release.set()
            a.join(5)
            b.join(5)
        self.assertEqual(order, ["start first", "end first", "start second", "end second"])
        self.assertEqual(pool.close(), [])

    def test_alone_it_goes_even_when_memory_is_short(self):
        """Something else holding the memory mustn't stall the scan forever; the low-memory stop is the backstop."""
        pool = scanner.Browsers(self.db, 1, factory=FakeSession)
        with mock.patch.object(scanner, "CHECK_EVERY", 0.01), mock.patch.object(scanner, "free_memory_mb", return_value=900):
            self.assertEqual(pool.fetch("x", False), "x")
        self.assertEqual(pool.pages, 0)
        self.assertEqual(pool.close(), [])

    def test_a_page_queued_while_memory_was_fine_still_waits_when_its_turn_comes(self):
        """VM 2026-10-30: six pages queued at 3.6 GB free; later two browsers loading at once took it to 327 MB."""
        gates = {u: threading.Event() for u in ("one", "two", "three")}
        order, free = [], [5000]

        class Slow(FakeSession):
            def fetch(self, url):
                order.append(f"start {url}")
                gates[url].wait(10)
                order.append(f"end {url}")
                return url
        pool = scanner.Browsers(self.db, 2, factory=Slow)
        with mock.patch.object(scanner, "CHECK_EVERY", 0.01), mock.patch.object(scanner, "free_memory_mb", side_effect=lambda: free[0]):
            ts = [threading.Thread(target=pool.fetch, args=(u, False)) for u in ("one", "two", "three")]

            def started(u):  # wait for the page itself, not a clock: a slow machine (arm64 emulation) reorders them
                for _ in range(500):
                    if f"start {u}" in order:
                        return
                    time.sleep(0.01)
                self.fail(f"{u} never started")
            ts[0].start(); started("one")
            ts[1].start(); started("two")
            ts[2].start()  # queued while memory is fine; both browsers are busy with one and two
            time.sleep(0.1)
            self.assertNotIn("start three", order)
            free[0] = 1500
            gates["one"].set()  # a browser is free for three, but two is still loading and memory is short
            time.sleep(0.2)
            self.assertNotIn("start three", order)
            gates["two"].set()
            gates["three"].set()
            for t in ts:
                t.join(5)
        self.assertLess(order.index("end two"), order.index("start three"))
        self.assertEqual(pool.pages, 0)
        self.assertEqual(pool.close(), [])


class SingleBrowser(Base):
    """Where memory is short, one browser does the plain pages and the Cloudflare ones (solver on per page)."""

    def test_one_browser_both_kinds_one_page_at_a_time(self):
        FakeSession.closed = 0
        made, calls, live = [], [], [0]
        peak = [0]
        lock = threading.Lock()

        class One(FakeSession):
            def fetch(self, url, **kw):
                with lock:
                    live[0] += 1
                    peak[0] = max(peak[0], live[0])
                calls.append((url, kw))
                time.sleep(0.02)
                with lock:
                    live[0] -= 1
                return url

        def factory(solve):
            made.append(One(solve))
            return made[-1]
        pool = scanner.Browsers(self.db, 1, factory=factory, single=True)
        out = []
        ts = [threading.Thread(target=lambda u=u, sv=sv: out.append(pool.fetch(u, sv)))
              for u, sv in (("a", False), ("b", True), ("c", False), ("d", True))]
        for t in ts:
            t.start()
        for t in ts:
            t.join(5)
        self.assertEqual(sorted(out), ["a", "b", "c", "d"])
        self.assertEqual(len(made), 1)  # one browser for everything
        self.assertEqual(made[0].solve, False)
        self.assertEqual(peak[0], 1)  # one page at a time
        self.assertEqual({u: kw for u, kw in calls}, {"a": {}, "b": {"solve_cloudflare": True}, "c": {},
                                                      "d": {"solve_cloudflare": True}})
        self.assertEqual(pool.close(), [])
        self.assertEqual(FakeSession.closed, 1)

    def test_a_stopped_browser_is_replaced_and_the_next_cloudflare_page_works(self):
        made, killed = [], []

        class Hangs(FakeSession):
            def __init__(self, solve):
                super().__init__(solve)
                self.gone = threading.Event()

            def fetch(self, url, **kw):
                if url == "hang":
                    self.gone.wait(10)
                    raise RuntimeError("Playwright connection closed")
                return (url, kw)

            def close(self):
                super().close()
                if self.gone.is_set():
                    raise RuntimeError("Playwright connection closed")

        def factory(solve):
            made.append(Hangs(solve))
            return made[-1]

        def killer(sess):
            killed.append(sess)
            sess.gone.set()
            return True
        pool = scanner.Browsers(self.db, 1, factory=factory, killer=killer, single=True)
        with mock.patch.object(scanner, "CHECK_EVERY", 0.01), mock.patch.object(scanner, "FETCH_LIMIT", 0.05):
            with self.assertRaises(TimeoutError):
                pool.fetch("hang", False)
            self.assertEqual(pool.fetch("cf", True), ("cf", {"solve_cloudflare": True}))
        self.assertEqual(killed, [made[0]])
        self.assertEqual(len(made), 2)  # a fresh browser, still only one alive at a time
        self.assertEqual(pool.close(), [])

    def test_real_single_browser_stopped_mid_page_then_a_solver_page(self):
        """The production path: a real Scrapling browser in single mode, a page that never stops, then a page fetched
        with the Cloudflare solver switched on for it (on a page with no challenge it just returns), nothing left."""
        import http.server

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(s):
                s.send_response(200)
                s.send_header("Content-Type", "text/html")
                s.end_headers()
                s.wfile.write(b"<p>spin</p><script>while(true){}</script>" if s.path == "/hang" else b"<title>ok</title>fine")

            def log_message(s, *a):
                pass
        pool = scanner.Browsers(self.db, 1, single=True)
        try:
            pool._make(False).close()
        except Exception as ex:
            self.skipTest(f"no stealth browser: {type(ex).__name__}")
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        me = os.getpid()
        try:
            with mock.patch.object(scanner, "FETCH_LIMIT", 15):
                with self.assertRaises(TimeoutError):
                    pool.fetch(base + "/hang", False)
                page = pool.fetch(base + "/ok", True)
                self.assertEqual((page.status, page.css("title::text").get()), (200, "ok"))
            self.assertEqual(len(pool.workers[False]), 1)
            self.assertEqual(pool.workers[True], [])  # no second browser
        finally:
            errors = pool.close()
            srv.shutdown()
            srv.server_close()
        self.assertEqual(errors, [])
        time.sleep(1)
        kids = [d for d in Path("/proc").glob("[0-9]*")
                if (d / "stat").exists() and f") S {me} " in (d / "stat").read_text()]
        self.assertEqual([k for k in kids if "chrom" in (k / "cmdline").read_text(errors="replace")
                          or "node" in (k / "cmdline").read_text(errors="replace")], [])


class SettleNotIdle(Base):
    """Stealth pages are read SETTLE_MS after load, not when the network goes quiet (laptop A/B/C, 2026-10-30)."""

    def test_the_pool_browser_gets_the_settle_wait(self):
        with mock.patch("scrapling.fetchers.StealthySession") as S:
            scanner.Browsers(self.db, 1)._make(False)
        kw = S.call_args[1]
        self.assertEqual((kw["network_idle"], kw["wait"], kw["block_ads"]), (False, scanner.SETTLE_MS, True))
        self.assertEqual(scanner.SETTLE_MS, 5000)

    def test_a_one_off_browser_gets_it_too(self):
        with mock.patch.object(scanner, "NO_NETWORK", False), mock.patch("scrapling.fetchers.StealthyFetcher.fetch") as F:
            scanner.get("https://x/1", self.db, True)
        self.assertEqual((F.call_args[1]["network_idle"], F.call_args[1]["wait"], F.call_args[1]["block_ads"]),
                         (False, scanner.SETTLE_MS, True))

    def test_the_removal_browser_gets_it_too(self):
        from guard_dog import removal
        self.db.set("stealth", "1")
        with mock.patch("scrapling.fetchers.StealthyFetcher.fetch") as F:
            removal.browser_page(self.db)("https://x/optout", lambda page: None)
        self.assertEqual((F.call_args[1]["network_idle"], F.call_args[1]["wait"], F.call_args[1]["block_ads"]),
                         (False, scanner.SETTLE_MS, True))

    def test_the_ad_list_spares_the_captcha_hosts(self):
        from scrapling.engines.toolbelt.ad_domains import AD_DOMAINS
        hosts = set(AD_DOMAINS)
        for h in ("challenges.cloudflare.com", "cloudflare.com", "www.google.com", "google.com", "gstatic.com",
                  "www.gstatic.com", "recaptcha.net", "hcaptcha.com", "spokeo.com"):
            self.assertNotIn(h, hosts)

    def test_the_captcha_address_is_a_block_whatever_the_page_says(self):
        at = "https://www.truepeoplesearch.com/InternalCaptcha?returnUrl=%2Fresults%3Fname%3DJane"
        self.assertTrue(scanner.blocked_page(200, "Jane Doe " * 2000, at))
        self.assertFalse(scanner.blocked_page(200, "Jane Doe " * 2000, "https://www.truepeoplesearch.com/results?name=Jane"))
        calls = []

        def fake(url, db, st=None, solve=False):
            calls.append((bool(st), solve))
            return 200, "<html>x</html>", "We could not find any records. " * 200, "fake", at
        with mock.patch.object(scanner, "fetch", fake):
            r = scanner.check_page(self.db, self.pid, PERSON, CHECKED, "https://x/1", "Jane Doe", "Columbus, OH", True)
        self.assertEqual((r, calls), ("blocked", [(False, False), (True, False), (True, True)]))


CF_ORIGIN = "https://challenges.cloudflare.com"


class SiteIsolation(unittest.TestCase):
    """Every cross-site frame on a people-search page got its own renderer past --renderer-process-limit (Pi: 77 at
    once). Site isolation is off except for Cloudflare's challenge frame: with it off there too, the solver got 403."""

    def test_isolation_is_off_except_for_the_challenge_frame(self):
        flags = scanner.BROWSER_LIMITS["extra_flags"]
        self.assertIn("--disable-site-isolation-trials", flags)
        self.assertIn(f"--isolate-origins={CF_ORIGIN}", flags)

    def test_no_flag_that_belongs_to_a_test_or_overrides_scrapling(self):
        # Scrapling passes its own --disable-features and Chromium keeps only the last one, so a second would drop its
        # list; the other two exist in the browser test below only and must never reach a scan.
        for f in scanner.BROWSER_LIMITS["extra_flags"]:
            for bad in ("--disable-features=", "--host-resolver-rules", "--ignore-certificate-errors"):
                self.assertFalse(f.startswith(bad), f)


def _chromium():
    from guard_dog import browser
    return (sys.platform.startswith("linux") and shutil.which("openssl") is not None
            and any((browser.root() / "chromium-1248").glob("chrome-linux*/chrome")))


@unittest.skipUnless(_chromium(), "needs the Playwright Chromium (chromium-1248) and openssl")
class SiteIsolationBrowser(unittest.TestCase):
    """A real browser with the package's flags on a page with 8 frames from 8 other sites (127.0.0.2-9 are each their
    own site and all reach this machine on Linux) and a ninth from a fake challenges.cloudflare.com on the solver's URL
    pattern. Two things the fixture has to get right, or the challenge frame never loads or is never found:
    - cloudflare.com is HSTS-preloaded, so an http frame to it is upgraded to https: the fake serves https with a
      certificate made here, accepted with --ignore-certificate-errors (this test's browser only);
    - Scrapling's __CF_PATTERN__ allows no port after the host, so the frame is on :443 and --host-resolver-rules maps
      challenges.cloudflare.com:443 to the fixture's port (an unprivileged test can't listen on 443)."""
    FRAMES = 8

    @classmethod
    def setUpClass(cls):
        import http.server
        import ssl

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = self.server.body
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        cls.tmp = tempfile.TemporaryDirectory()
        cert, key = Path(cls.tmp.name) / "cert.pem", Path(cls.tmp.name) / "key.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(cert),
                        "-days", "1", "-subj", "/CN=challenges.cloudflare.com",
                        "-addext", "subjectAltName=DNS:challenges.cloudflare.com"], check=True, capture_output=True)
        cls.servers = []
        for i in range(2, 2 + cls.FRAMES):
            s = http.server.ThreadingHTTPServer((f"127.0.0.{i}", 0), H)
            s.body = b"<html><body><p>frame</p></body></html>"
            cls.servers.append(s)
        cf = http.server.ThreadingHTTPServer(("127.0.0.10", 0), H)
        cf.body = b'<html><body><input type="checkbox"><p>challenge</p></body></html>'
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(cert), str(key))
        cf.socket = ctx.wrap_socket(cf.socket, server_side=True)
        cls.servers.append(cf)
        cls.test_flags = [f"--host-resolver-rules=MAP challenges.cloudflare.com:443 127.0.0.10:{cf.server_address[1]}",
                          "--ignore-certificate-errors"]
        frames = "".join(f'<iframe src="http://{s.server_address[0]}:{s.server_address[1]}/"></iframe>'
                         for s in cls.servers[:cls.FRAMES])
        frames += f'<iframe src="{CF_ORIGIN}/cdn-cgi/challenge-platform/h/b/turnstile/if/ov2/t" width="300" height="65">' \
                  "</iframe>"
        main = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        main.body = f"<html><body><p>listing</p>{frames}</body></html>".encode()
        cls.servers.append(main)
        cls.url = f"http://127.0.0.1:{main.server_address[1]}/"
        for s in cls.servers:
            threading.Thread(target=s.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        for s in cls.servers:
            s.shutdown()
            s.server_close()
        cls.tmp.cleanup()

    def setUp(self):
        self.dbdir = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.dbdir.name))

    def tearDown(self):
        self.dbdir.cleanup()

    @staticmethod
    def renderers(top):
        """Renderer processes under `top`. Chromium rewrites a child's cmdline into one space-separated string."""
        kids = {}
        for d in Path("/proc").glob("[0-9]*"):
            try:
                kids.setdefault(int((d / "stat").read_text().rsplit(")", 1)[1].split()[1]), []).append(int(d.name))
            except (OSError, ValueError, IndexError):
                continue
        n, todo = 0, list(kids.get(top, []))
        while todo:
            p = todo.pop()
            todo += kids.get(p, [])
            try:
                args = Path(f"/proc/{p}/cmdline").read_bytes().replace(b"\0", b" ").split()
            except OSError:
                continue
            n += b"--type=renderer" in args
        return n

    def load(self, flags):
        """(most renderers alive at once, challenge frame found with a box) for the fixture page, through the pool's
        own browser with `flags` plus this test's resolver and certificate flags."""
        from scrapling.engines._browsers._stealth import __CF_PATTERN__
        with mock.patch.object(scanner, "BROWSER_LIMITS", dict(scanner.BROWSER_LIMITS, extra_flags=flags + self.test_flags)):
            s = scanner.Browsers(self.db, 1)._make(False)
        top = s.playwright._impl_obj._connection._transport._proc.pid
        peak, done, seen = [0], threading.Event(), []

        def watch():
            while not done.is_set():
                peak[0] = max(peak[0], self.renderers(top))
                done.wait(0.2)

        def look(page):
            time.sleep(1)
            f = page.frame(url=__CF_PATTERN__)
            seen.append(f is not None and bool(f.frame_element().bounding_box()))
        t = threading.Thread(target=watch, daemon=True)
        t.start()
        try:
            self.assertIn("listing", s.fetch(self.url, page_action=look).get_all_text())
        finally:
            done.set()
            t.join()
            s.close()
        return peak[0], seen == [True]

    def test_the_challenge_frame_alone_gets_its_own_renderer(self):
        flags = scanner.BROWSER_LIMITS["extra_flags"]
        shared, _ = self.load([f for f in flags if not f.startswith("--isolate-origins=")])
        isolated, found = self.load(flags)
        self.assertLessEqual(shared, 3)  # the eight frames and the challenge frame all in the page's renderers
        self.assertEqual(isolated, shared + 1)  # the package's flags: only the challenge frame moves out
        self.assertTrue(found)  # where the solver looks for it, with a box to click

    def test_without_the_flags_the_fixture_makes_a_renderer_per_frame(self):
        """Proof the fixture really has cross-site frames, so the test above can fail."""
        flags = [f for f in scanner.BROWSER_LIMITS["extra_flags"]
                 if f != "--disable-site-isolation-trials" and not f.startswith("--isolate-origins=")]
        self.assertGreaterEqual(self.load(flags)[0], self.FRAMES + 2)


class SweepRows(Base):
    """A page the web sweep finds on a people-search site isn't one of that site's search addresses: supersede()
    must leave it alone, or it's hidden and brought back every scan and a listing that went away never counts as gone."""
    PROFILE = "https://www.spokeo.com/Jane-Doe/Ohio/Columbus/p000000001"

    def setUp(self):
        super().setUp()
        from guard_dog import sweep
        from guard_dog.brokers import BROKERS, search_urls
        self.sweep = sweep
        self.spokeo = next(b for b in BROKERS if b["id"] == "spokeo")
        self.searched = {u for b in BROKERS for u, _, _ in search_urls(b, self.db.person(self.pid))}
        self.search = search_urls(self.spokeo, self.db.person(self.pid))[0][0]
        self.hit = dict(url=self.PROFILE, domain="spokeo.com", name="Jane Doe", place="Columbus, OH")

    def one_scan(self, sweep_status):
        """The order run() keeps: people-search phase, supersede(), web sweep. Returns this scan's alerts."""
        before = self.db.q("SELECT COALESCE(MAX(id), 0) n FROM alerts")[0]["n"]
        scanner.record(self.db, self.pid, self.spokeo, self.search, "not found", [], None)
        scanner.supersede(self.db, self.pid, self.searched)
        self.assertNotEqual(self.status(self.PROFILE), "superseded")
        scanner.record(self.db, self.pid, self.sweep.source_for(self.hit), self.PROFILE, sweep_status, [], None)
        return [a["text"] for a in self.db.q("SELECT text FROM alerts WHERE id > ?", (before,))]

    def status(self, url):
        r = self.db.q("SELECT status FROM findings WHERE url=?", (url,))
        return r[0]["status"] if r else None

    def test_a_sweep_page_is_never_superseded_and_gone_takes_two_scans(self):
        self.assertEqual(self.one_scan("listed"), ["Spokeo: new listing found."])
        self.assertEqual(self.one_scan("listed"), [])
        self.assertEqual(self.one_scan("not found"),
                         ["Spokeo no longer shows the listing; proven when the next scan agrees."])
        self.assertEqual(self.one_scan("not found"),
                         ["Spokeo: the listing is gone (two scans in a row didn't find it)."])
        self.assertEqual(self.one_scan("not found"), [])
        rows = {r["url"]: (r["kind"], r["status"]) for r in self.rows()}
        self.assertEqual(rows, {self.search: ("broker", "not found"), self.PROFILE: ("broker-web", "not found")})

    def test_an_older_sweep_row_tagged_broker_heals_on_its_next_check(self):
        scanner.record(self.db, self.pid, dict(self.spokeo, kind="broker"), self.PROFILE, "listed", [], None)
        scanner.supersede(self.db, self.pid, self.searched)  # what a 0.1.0 scan did to it
        self.assertEqual(self.status(self.PROFILE), "superseded")
        scanner.record(self.db, self.pid, self.sweep.source_for(self.hit), self.PROFILE, "listed", [], None)
        scanner.supersede(self.db, self.pid, self.searched)
        r = self.db.q("SELECT kind, status FROM findings WHERE url=?", (self.PROFILE,))[0]
        self.assertEqual((r["kind"], r["status"]), ("broker-web", "listed"))

    def test_the_sweep_skips_a_search_address_it_just_checked(self):
        from guard_dog.brokers import BROKERS, search_urls
        self.db.set("delay_min", "0")
        self.db.set("delay_max", "0")
        self.db.set("stealth", "0")
        plain = next(b for b in BROKERS if not b["blocks_bots"] and search_urls(b, self.db.person(self.pid)))
        url = search_urls(plain, self.db.person(self.pid))[0][0]
        hits = [dict(url=url, domain=self.sweep.urlsplit(url).netloc.lower().removeprefix("www."), name="Jane Doe",
                     place="", query='"x"', engine="bing")]
        fetched = []

        def fake_fetch(u, db, st=None, solve=False):
            fetched.append(u)
            return 200, NOBODY, NOBODY, "fake", ""
        with mock.patch.object(scanner, "fetch", fake_fetch), \
                mock.patch.object(scanner.sweep, "search", return_value=(hits, {})), \
                mock.patch.object(scanner.breaches, "xposedornot", return_value=[]):
            scanner.run(self.db, self.pid)
        self.assertEqual(fetched.count(url), 1)
        self.assertEqual(self.db.q("SELECT kind FROM findings WHERE url=?", (url,))[0]["kind"], "broker")

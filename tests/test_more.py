"""The rest: breach services, the entry point, network-address rules, DB migration, scan failure paths — no network."""
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from guard_dog import __main__ as entry
from guard_dog import breaches, brokers, scanner, sweep, web
from guard_dog.db import DB


class Breaches(unittest.TestCase):
    def test_xposedornot(self):
        meta = json.dumps({"exposedBreaches": [{"breachID": "Big", "domain": "big.com", "breachedDate": "2024-05-01T00:00",
                                                "exposedData": ["Emails", "Passwords"], "passwordRisk": "plaintext"}]})
        answers = {"/v1/breaches": (200, meta.encode()),
                   "/check-email/": (200, json.dumps({"breaches": [["Big", "Unknown"]]}).encode())}
        breaches._xon_meta["ts"] = 0

        def fake(url, headers=None, timeout=30):
            return next(v for k, v in answers.items() if k in url)
        with mock.patch.object(breaches, "_get", fake):
            got = breaches.xposedornot("a@b.com")
        self.assertEqual([b["breach"] for b in got], ["Big", "Unknown"])
        self.assertEqual((got[0]["date"], got[0]["password_risk"]), ("2024-05-01", "plaintext"))
        self.assertTrue(breaches.password_leaked(got[0]))
        self.assertFalse(breaches.password_leaked(got[1]))
        with mock.patch.object(breaches, "_get", return_value=(404, b"")):
            self.assertEqual(breaches.xposedornot("a@b.com"), [])
        with mock.patch.object(breaches, "_get", return_value=(500, b"")):
            with self.assertRaises(RuntimeError):
                breaches.xposedornot("a@b.com")

    def test_hibp(self):
        body = json.dumps([{"Name": "X", "Domain": "x.com", "BreachDate": "2020-01-01", "DataClasses": ["Passwords"]},
                           {"Name": "Y", "DataClasses": ["Emails"]}]).encode()
        with mock.patch.object(breaches, "_get", return_value=(200, body)) as g:
            got = breaches.hibp("a@b.com", "KEY")
        self.assertEqual(g.call_args[0][1], {"hibp-api-key": "KEY"})
        self.assertEqual([b["password_risk"] for b in got], ["exposed", "none"])
        with mock.patch.object(breaches, "_get", return_value=(404, b"")):
            self.assertEqual(breaches.hibp("a@b.com", "K"), [])
        with mock.patch.object(breaches, "_get", return_value=(401, b"")):
            with self.assertRaises(RuntimeError):
                breaches.hibp("a@b.com", "K")

    def test_password_seen_sends_only_5_characters(self):
        # sha1("password") = 5BAA61E4C9B93F3F0682250B6CF8331B7EE68FD8
        with mock.patch.object(breaches, "_get", return_value=(200, b"1E4C9B93F3F0682250B6CF8331B7EE68FD8:42\nABC:1")) as g:
            self.assertEqual(breaches.password_seen("password"), 42)
        self.assertTrue(g.call_args[0][0].endswith("/range/5BAA6"))
        with mock.patch.object(breaches, "_get", return_value=(200, b"ABC:1")):
            self.assertEqual(breaches.password_seen("password"), 0)
        with mock.patch.object(breaches, "_get", return_value=(503, b"")):
            with self.assertRaises(RuntimeError):
                breaches.password_seen("x")

    def test_get_ok_and_http_errors(self):
        err = breaches.urllib.error.HTTPError("u", 404, "nf", {}, None)
        err.read = lambda: b"gone"
        ok = mock.MagicMock()
        ok.__enter__.return_value = mock.Mock(status=200, read=lambda: b"body")
        with mock.patch.dict(breaches.os.environ, {"GUARD_DOG_NO_NETWORK": "0"}):  # urlopen is faked below
            with mock.patch.object(breaches.urllib.request, "urlopen", side_effect=err):
                self.assertEqual(breaches._get("https://x"), (404, b"gone"))
            with mock.patch.object(breaches.urllib.request, "urlopen", return_value=ok) as u:
                self.assertEqual(breaches._get("https://x", {"A": "1"}), (200, b"body"))
        self.assertEqual(u.call_args[0][0].get_header("A"), "1")
        self.assertTrue(u.call_args[0][0].get_header("User-agent").startswith("keephaven-guard-dog"))


class EntryPoint(unittest.TestCase):
    def test_default_data_per_platform(self):
        with mock.patch.object(entry.sys, "platform", "win32"), mock.patch.dict(entry.os.environ, {"APPDATA": "/w"}):
            self.assertEqual(str(entry.default_data()), "/w/Keephaven Guard Dog")
        with mock.patch.object(entry.sys, "platform", "darwin"):
            self.assertIn("Library/Application Support/Keephaven Guard Dog", str(entry.default_data()))
        with mock.patch.object(entry.sys, "platform", "linux"), mock.patch.dict(entry.os.environ, {"XDG_DATA_HOME": "/x"}):
            self.assertEqual(str(entry.default_data()), "/x/keephaven-guard-dog")

    def test_main_starts_watch_and_dashboard_owner_only(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(entry.web, "serve") as serve, \
                mock.patch.object(entry.threading, "Thread") as thread, \
                mock.patch.object(entry.sys, "argv", ["gd", "--data", d + "/data", "--host", "0.0.0.0", "--allow-host", "pi"]):
            entry.main()
            self.assertEqual(Path(d, "data").stat().st_mode & 0o777, 0o700)
            thread.assert_not_called()  # no scan before the dashboard is listening
            serve.call_args[1]["ready"]()
        self.assertEqual(serve.call_args[0][1:], ("0.0.0.0", 8642, ["pi"]))
        self.assertIs(thread.call_args[1]["target"], scanner.scheduler)

    def test_an_address_not_there_yet_is_one_plain_line(self):
        """At boot Tailscale's address may not exist yet: the log should say so, not show a traceback."""
        import errno
        for err, words in ((errno.EADDRNOTAVAIL, "100.64.0.10 isn't on this machine yet"),
                           (errno.EADDRINUSE, "Port 8642 is already in use")):
            with tempfile.TemporaryDirectory() as d, \
                    mock.patch.object(entry.web, "serve", side_effect=OSError(err, "x")), \
                    mock.patch.object(entry.sys, "argv", ["gd", "--data", d + "/data", "--host", "100.64.0.10"]):
                with self.assertRaises(SystemExit) as cm:
                    entry.main()
            self.assertIn(words, str(cm.exception.code))
            self.assertIn("tried again in 15 s", str(cm.exception.code))
        with tempfile.TemporaryDirectory() as d, mock.patch.object(entry.web, "serve", side_effect=OSError(errno.EACCES, "x")), \
                mock.patch.object(entry.sys, "argv", ["gd", "--data", d + "/data"]):
            with self.assertRaises(PermissionError):
                entry.main()

    def test_serve_says_ready_only_after_binding(self):
        order = []
        with mock.patch.object(web, "build", side_effect=lambda *a, **k: order.append("bound") or (mock.Mock(), "", True)):
            web.serve(mock.Mock(dir="/data"), "127.0.0.1", 8642, ready=lambda: order.append("ready"))
        self.assertEqual(order, ["bound", "ready"])
        with mock.patch.object(web, "build", side_effect=OSError("Cannot assign requested address")):
            with self.assertRaises(OSError):
                web.serve(mock.Mock(dir="/data"), "100.64.0.10", 8642, ready=lambda: order.append("too soon"))
        self.assertEqual(order, ["bound", "ready"])


class Addresses(unittest.TestCase):
    def tearDown(self):
        web.TAILSCALE = None

    def test_on_tailscale(self):
        with mock.patch.object(web, "my_addresses", return_value={"192.168.1.5", "pi.local", "100.64.0.10"}):
            self.assertTrue(web.on_tailscale())
        with mock.patch.object(web, "my_addresses", return_value={"192.168.1.5", "pi.local"}), \
                mock.patch.object(web.socket, "socket", side_effect=OSError("no route")):
            self.assertFalse(web.on_tailscale())

    def test_cgnat_is_decided_once(self):
        web.TAILSCALE = None
        with mock.patch.object(web, "on_tailscale", return_value=False) as ot:
            self.assertFalse(web.own_network("100.64.0.10"))
            self.assertFalse(web.own_network("100.64.0.11"))
        ot.assert_called_once()

    def test_my_addresses_survives_lookup_failures(self):
        with mock.patch.object(web.socket, "getaddrinfo", side_effect=OSError), \
                mock.patch.object(web.socket, "socket", side_effect=OSError):
            names = web.my_addresses()
        self.assertTrue(any(n.endswith(".local") for n in names))

    def test_build_on_a_tailscale_address_and_port_80(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(web, "Server") as srv, mock.patch.object(web, "my_addresses", return_value={"pi"}):
            db = DB(Path(d))
            _, code, local = web.build(db, "100.64.0.12", 80, ["pi.tail.ts.net"])
        self.assertTrue(web.TAILSCALE)
        self.assertFalse(local)
        self.assertTrue(code)
        with tempfile.TemporaryDirectory() as d, mock.patch.object(web, "Server"), \
                mock.patch.object(web, "make_handler") as mh, mock.patch.object(web, "my_addresses", return_value=set()):
            web.build(DB(Path(d)), "100.64.0.12", 80, ["pi.tail.ts.net"])
        hosts = mh.call_args[0][2]
        self.assertIn("pi.tail.ts.net", hosts)  # port 80: bare names too
        self.assertIn("100.64.0.12:80", hosts)

    def test_serve_prints_the_setup_code(self):
        fake = mock.Mock()
        with mock.patch.object(web, "build", return_value=(fake, "ab-cd-ef", False)), mock.patch("builtins.print") as pr:
            web.serve(mock.Mock(dir="/data"), "0.0.0.0", 8642)
        fake.serve_forever.assert_called_once()
        self.assertIn("ab-cd-ef", " ".join(str(c) for c in pr.call_args_list))
        with mock.patch.object(web, "build", return_value=(fake, "", True)), mock.patch("builtins.print") as pr:
            web.serve(mock.Mock(dir="/data"), "127.0.0.1", 8642)
        self.assertEqual(len(pr.call_args_list), 1)  # local-only: no setup code line


class Migration(unittest.TestCase):
    def test_old_database_gets_new_columns(self):
        with tempfile.TemporaryDirectory() as d:
            c = sqlite3.connect(Path(d) / "guard-dog.sqlite")
            c.execute("CREATE TABLE people(id INTEGER PRIMARY KEY, label TEXT, names TEXT, emails TEXT, phones TEXT,"
                      " places TEXT, usernames TEXT, consent TEXT, created REAL, last_scan REAL)")
            c.execute("CREATE TABLE removals(finding INTEGER PRIMARY KEY, state TEXT, updated REAL, evidence TEXT,"
                      " note TEXT, history TEXT)")
            c.execute("CREATE TABLE findings(id INTEGER PRIMARY KEY, person INTEGER, source TEXT, url TEXT, status TEXT,"
                      " matched TEXT, copy TEXT, first_seen REAL, last_seen REAL, removal TEXT, removal_ts REAL,"
                      " UNIQUE(person, source, url))")
            c.commit()
            c.close()
            db = DB(Path(d))
            cols = {r[1] for r in db.c.execute("PRAGMA table_info(findings)")}
            pcols = {r[1] for r in db.c.execute("PRAGMA table_info(people)")}
            rcols = {r[1] for r in db.c.execute("PRAGMA table_info(removals)")}
        self.assertIn("prefill", rcols)
        self.assertTrue({"last_checked", "check_note", "ever_listed", "kind"} <= cols)
        self.assertTrue({"relatives", "birth_year"} <= pcols)

    def test_old_removal_marks_move_into_the_removals_table(self):
        with tempfile.TemporaryDirectory() as d:
            db = DB(Path(d))
            pid = db.save_person(None, "T", "", names=["Jane Doe"])
            scanner.record(db, pid, {"id": "s", "name": "S"}, "https://s/1", "listed", [], None)
            scanner.record(db, pid, {"id": "t", "name": "T"}, "https://t/1", "not found", [], None)
            db.x("UPDATE findings SET removal='requested', removal_ts=5 WHERE source='s'")
            db.x("UPDATE findings SET removal='gone', removal_ts=6 WHERE source='t'")
            db2 = DB(Path(d))
            got = {r["source"]: r["state"] for r in db2.q("SELECT f.source, r.state FROM removals r JOIN findings f ON f.id=r.finding")}
            left = db2.q("SELECT COUNT(*) n FROM findings WHERE removal IS NOT NULL")[0]["n"]
        self.assertEqual(got, {"s": "requested", "t": "gone"})
        self.assertEqual(left, 0)

    def test_stale_settings_are_dropped(self):
        with tempfile.TemporaryDirectory() as d:
            DB(Path(d)).set("rescan_days", "30")
            self.assertEqual(DB(Path(d)).q("SELECT COUNT(*) n FROM settings WHERE k='rescan_days'")[0]["n"], 0)


class BrokerUrls(unittest.TestCase):
    def test_single_names_skipped_and_duplicates_once(self):
        b = brokers.BROKERS[0]
        urls = brokers.search_urls(b, {"names": ["Cher", "Jane Doe", "Jane Doe"], "places": ["Columbus, OH", "Columbus, OH"]})
        self.assertEqual(len(urls), 1)
        self.assertEqual(brokers.split_place("OH"), ("", "OH"))
        self.assertEqual(brokers.split_place("Springfield"), ("Springfield", ""))


class FailurePaths(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.tmp.name))
        self.pid = self.db.save_person(None, "T", "", names=["Jane Doe"], emails=["j@x.com"])
        for k, v in (("delay_min", "0"), ("delay_max", "0")):
            self.db.set(k, v)

    def tearDown(self):
        self.tmp.cleanup()

    def texts(self):
        return [a["text"] for a in self.db.q("SELECT text FROM alerts")]

    def test_new_breach_alert_after_the_first_scan(self):
        self.db.set("sweep", "0")
        self.db.x("UPDATE people SET last_scan=1 WHERE id=?", (self.pid,))
        b = [dict(breach="New", domain="n.com", date="2026", exposed="Emails", password_risk="none")]
        with mock.patch.object(scanner, "fetch", lambda *a, **k: (404, "", "", "x")), \
                mock.patch.object(scanner.breaches, "xposedornot", return_value=b):
            scanner.run(self.db, self.pid)
        self.assertIn("j@x.com is in a new breach: New.", self.texts())

    def test_hibp_used_when_a_key_is_set(self):
        self.db.set("sweep", "0")
        self.db.set("hibp_key", "K")
        with mock.patch.object(scanner, "fetch", lambda *a, **k: (404, "", "", "x")), \
                mock.patch.object(scanner.breaches, "hibp", return_value=[]) as h:
            scanner.run(self.db, self.pid)
        h.assert_called_once_with("j@x.com", "K")

    def test_a_crashing_check_is_an_alert_and_the_scan_still_finishes(self):
        with mock.patch.object(scanner, "check_page", side_effect=RuntimeError("parser blew up")), \
                mock.patch.object(scanner.sweep, "search",
                                  return_value=([dict(url="https://w.example/p", domain="w.example", name="Jane Doe",
                                                      place="", query='"x"', engine="bing")], {})), \
                mock.patch.object(scanner.breaches, "xposedornot", return_value=[]):
            scanner.run(self.db, self.pid)
        t = self.texts()
        self.assertTrue(any(x.startswith("people-search check failed") and "parser blew up" in x for x in t))
        self.assertTrue(any(x.startswith("web page check failed (w.example)") for x in t))
        self.assertEqual(self.db.q("SELECT state FROM scans")[0]["state"], "done")

    def test_unexpected_error_marks_the_scan_failed(self):
        with mock.patch.object(scanner.breaches, "xposedornot", return_value=[]), \
                mock.patch.object(scanner, "search_urls", side_effect=KeyError("search")):
            scanner.run(self.db, self.pid)
        st = self.db.q("SELECT state, progress FROM scans")[0]
        self.assertEqual(st["state"], "failed")
        self.assertIn("search", st["progress"])
        self.assertFalse(scanner.running(self.pid))

    def test_cancel_before_breaches_and_brokers(self):
        scanner._cancel.add(self.pid)
        with mock.patch.object(scanner.breaches, "xposedornot") as x:
            scanner.run(self.db, self.pid)
        x.assert_not_called()
        self.assertIsNone(self.db.person(self.pid))

    def test_sweep_engine_crash_is_a_failure(self):
        with mock.patch.object(scanner, "in_parallel", return_value=[((0, 0), "RuntimeError: boom")]):
            hits, failed = sweep.search(self.db, {"names": ["Jane Doe"], "places": [], "emails": [], "phones": [],
                                                  "usernames": []}, lambda s: None, lambda: None, lambda: False)
        self.assertEqual(failed, {"brave": ["RuntimeError: boom"]})

    def test_scheduler_loop_starts_who_is_due(self):
        with mock.patch.object(scanner, "due", return_value=self.pid), mock.patch.object(scanner, "start") as st, \
                mock.patch("guard_dog.requests.send_waiting") as sw, mock.patch("guard_dog.inbox.poll"), \
                mock.patch.object(scanner.time, "sleep", side_effect=StopIteration):
            with self.assertRaises(StopIteration):
                scanner.scheduler(self.db)
        st.assert_called_once_with(self.db, self.pid)

    def test_scheduler_closes_scans_left_running_by_a_crash(self):
        """PiCloud 2026-10-30: the Pi went down mid-scan; that scan and five more said running all morning."""
        old = self.db.x("INSERT INTO scans(person, started, state, progress, stealth) VALUES(?,?,?,?,?)",
                        (self.pid, 1.0, "running", "web search 32/36: google", 1))
        done = self.db.x("INSERT INTO scans(person, started, finished, state, progress, stealth) VALUES(?,?,?,?,?,?)",
                         (self.pid, 1.0, 2.0, "done", "done", 1))
        with mock.patch.object(scanner, "due", return_value=None), mock.patch("guard_dog.requests.send_waiting"), \
                mock.patch("guard_dog.inbox.poll"), mock.patch.object(scanner.time, "sleep", side_effect=StopIteration):
            with self.assertRaises(StopIteration):
                scanner.scheduler(self.db)
        rows = {r["id"]: r for r in self.db.q("SELECT * FROM scans")}
        self.assertEqual((rows[old]["state"], rows[old]["progress"]), ("failed", "stopped: Guard Dog restarted"))
        self.assertIsNotNone(rows[old]["finished"])
        self.assertEqual((rows[done]["state"], rows[done]["finished"]), ("done", 2.0))


class Fetching(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_fetch_wraps_the_response(self):
        resp = mock.Mock(status=200, html_content="<p>hi</p>", url="https://site/results?q=1")
        resp.get_all_text.return_value = "hi"
        with mock.patch.object(scanner, "get", return_value=resp) as g:
            self.assertEqual(scanner.fetch("u", self.db), (200, "<p>hi</p>", "hi", "plain", "https://site/results?q=1"))
            self.db.set("stealth", "1")
            self.assertEqual(scanner.fetch("u", self.db)[3], "stealth")
            self.assertEqual(scanner.fetch("u", self.db, True, solve=True)[3], "stealth+solve")
        self.assertEqual(g.call_args[0][2:], (True, True))

    def test_plain_and_pooled_get(self):
        with mock.patch.object(scanner, "NO_NETWORK", False), \
                mock.patch("scrapling.fetchers.Fetcher.get", return_value="plain") as fg:
            scanner._tls.browsers = None
            self.assertEqual(scanner.get("https://x", self.db, False), "plain")
            self.assertEqual(fg.call_args[1]["impersonate"], "chrome")
            pool = mock.Mock()
            pool.fetch.return_value = "pooled"
            scanner._tls.browsers = pool
            self.assertEqual(scanner.get("https://x", self.db, True, True), "pooled")
            pool.fetch.assert_called_once_with("https://x", True)
            scanner._tls.browsers = None

    def test_browser_factory_starts_a_session(self):
        with mock.patch("scrapling.fetchers.StealthySession") as S:
            s = scanner.Browsers(self.db, 1)._make(True)
        self.assertTrue(S.call_args[1]["solve_cloudflare"])
        self.assertTrue(S.call_args[1]["disable_resources"])
        s.start.assert_called_once()


class OwnBox(unittest.TestCase):
    def test_link_at_the_top_of_the_page(self):
        from scrapling.parser import Selector
        rs, n = sweep.results(Selector('<a href="https://solo.example/">Jane Doe</a>'), '"Jane Doe"')
        self.assertEqual((rs, n), (["https://solo.example/"], 1))


if __name__ == "__main__":
    unittest.main()


class LastBranches(unittest.TestCase):
    def test_email_on_a_page_is_a_listing_and_a_page_without_the_searched_name(self):
        p = {"names": ["Jane Doe", "Jane Roe"], "emails": ["jane@example.com"], "phones": [], "places": [], "usernames": []}
        self.assertEqual(scanner.match("Contact Jane Doe at JANE@example.com", p, "Jane Doe", "")[0], "listed")
        # the searched name isn't on the page, only another of hers: no "other name" evidence from that alone
        st, got = scanner.match("Jane Roe of somewhere", p, "Jane Doe", "")
        self.assertEqual(st, "unconfirmed")
        self.assertNotIn("other name: Jane Roe", got)

    def test_breach_list_is_cached_and_kept_when_the_refresh_fails(self):
        breaches._xon_meta.update(ts=0, by_id={"Old": {"domain": "old.com"}})
        with mock.patch.object(breaches, "_get", return_value=(500, b"")) as g:
            self.assertEqual(breaches._xon_details(), {"Old": {"domain": "old.com"}})
        breaches._xon_meta["ts"] = breaches.time.time()
        with mock.patch.object(breaches, "_get") as g:
            breaches._xon_details()
        g.assert_not_called()

    def test_scheduler_waits_when_nobody_is_due(self):
        with mock.patch.object(scanner, "due", return_value=None), mock.patch.object(scanner, "start") as st, \
                mock.patch("guard_dog.requests.send_waiting"), mock.patch("guard_dog.inbox.poll"), \
                mock.patch.object(scanner.time, "sleep", side_effect=StopIteration):
            with self.assertRaises(StopIteration):
                scanner.scheduler(mock.Mock())
        st.assert_not_called()

    def test_build_on_all_addresses_and_my_addresses(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(web, "Server"), \
                mock.patch.object(web, "make_handler") as mh:
            web.build(DB(Path(d)), "0.0.0.0", 8642)
        hosts = mh.call_args[0][2]
        self.assertNotIn("0.0.0.0:8642", hosts)
        self.assertIn("127.0.0.1:8642", hosts)
        with mock.patch.object(web.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("10.0.0.9", 0))]):
            self.assertIn("10.0.0.9", web.my_addresses())


class Guards(unittest.TestCase):
    def test_breach_requests_refuse_in_a_no_network_run(self):
        with mock.patch.dict(breaches.os.environ, {"GUARD_DOG_NO_NETWORK": "1"}):
            with self.assertRaises(RuntimeError):
                breaches._get("https://api.xposedornot.com/v1/breaches")

    def test_own_box_stops_after_four_levels(self):
        from scrapling.parser import Selector
        deep = "<div><div><div><div><div><div><p>Jane Doe <a href='https://deep.example/'>x</a></p></div></div></div></div></div></div>"
        self.assertEqual(sweep.results(Selector(deep), '"Jane Doe"')[0], ["https://deep.example/"])


class Places(unittest.TestCase):
    def test_every_common_way_of_typing_a_place(self):
        cases = {"Columbus, OH": ("Columbus", "OH"), "Columbus OH": ("Columbus", "OH"),
                 "Columbus, Ohio": ("Columbus", "OH"), "Youngstown Ohio": ("Youngstown", "OH"),
                 "New York New York": ("New York", "NY"), "Charleston West Virginia": ("Charleston", "WV"),
                 "Kansas City Missouri": ("Kansas City", "MO"), "St. Louis, MO": ("St. Louis", "MO"),
                 "OH": ("", "OH"), "Ohio": ("", "OH"), "Springfield": ("Springfield", ""), "  ": ("", "")}
        for typed, want in cases.items():
            self.assertEqual(brokers.split_place(typed), want, typed)

    def test_a_city_typed_without_a_comma_still_anchors_and_searches(self):
        p = {"names": ["Jane Doe"], "emails": [], "phones": [], "usernames": [], "places": ["Youngstown Ohio"]}
        self.assertEqual(scanner.match("Jane Doe Age 52 Lives in Youngstown, OH", p, "Jane Doe", "")[0], "listed")
        url = brokers.search_urls(brokers.BROKERS[0], p)[0][0]
        self.assertIn("Youngstown", url)
        self.assertIn("OH", url)


class ManualRemoval(unittest.TestCase):
    def test_asked_by_hand_from_every_state(self):
        from guard_dog import removal
        with tempfile.TemporaryDirectory() as d:
            db = DB(Path(d))
            for start, path in (("found", []), ("ready", ["ready"]), ("failed", ["failed"]),
                                ("requested", ["ready", "requested"]), ("gone", ["ready", "requested", "gone"])):
                fid = {"found": 1, "ready": 2, "failed": 3, "requested": 4, "gone": 5}[start]
                for s in path:
                    removal.move(db, fid, s)
                web.mark_asked_by_hand(db, fid)
                want = {"found": "requested", "ready": "requested", "failed": "requested", "requested": "requested",
                        "gone": "gone"}[start]
                self.assertEqual(removal.state(db, fid), want, start)


class Superseded(unittest.TestCase):
    def test_old_search_addresses_are_hidden_not_deleted(self):
        from collections import Counter

        from guard_dog import removal
        with tempfile.TemporaryDirectory() as d:
            db = DB(Path(d))
            pid = db.save_person(None, "Me", "", names=["Jane Doe"])
            for url, st in (("https://s/old", "possible"), ("https://s/new", "listed"), ("https://s/inflight", "listed")):
                scanner.record(db, pid, {"id": "s", "name": "S"}, url, st, ["name: Jane Doe"], None)
            inflight = db.q("SELECT id FROM findings WHERE url='https://s/inflight'")[0]["id"]
            removal.move(db, inflight, "ready")
            removal.move(db, inflight, "requested")
            scanner.supersede(db, pid, {"https://s/new"})
            st = {r["url"]: r["status"] for r in db.q("SELECT url, status FROM findings")}
            self.assertEqual(st, {"https://s/old": "superseded", "https://s/new": "listed", "https://s/inflight": "listed"})
            shown = Counter(r["status"] for r in db.q("SELECT status FROM findings WHERE status != 'superseded'"))
            p = db.person(pid)
            self.assertNotIn("might be you", web.summary(p, shown, 0, False, web.who(p)))
            scanner.record(db, pid, {"id": "s", "name": "S"}, "https://s/other", "listed", [], None)
            scanner.supersede(db, pid, set())  # no search addresses to compare: nothing is hidden
            self.assertEqual(db.q("SELECT status FROM findings WHERE url='https://s/other'")[0]["status"], "listed")


class BrowserLimits(unittest.TestCase):
    def test_the_limits_reach_the_browser(self):
        with tempfile.TemporaryDirectory() as d, mock.patch("scrapling.fetchers.StealthySession") as S:
            scanner.Browsers(DB(Path(d)), 1)._make(False)
        self.assertEqual(S.call_args[1]["max_pages"], 1)
        self.assertEqual(S.call_args[1]["extra_flags"], ["--renderer-process-limit=2", "--disable-site-isolation-trials",
                                                         "--isolate-origins=https://challenges.cloudflare.com"])

    def test_a_browser_that_wont_shut_down_is_reported(self):
        with tempfile.TemporaryDirectory() as d:
            pool = scanner.Browsers(DB(Path(d)), 1)
            stuck = mock.Mock()
            stuck.is_alive.return_value = True
            pool.workers[False].append(stuck)
            self.assertEqual(pool.close(), [("closing a browser", "a browser didn't shut down within 2 minutes")])


class Refetch(unittest.TestCase):
    def test_manual_rows_without_details_are_fetched_again_once_per_scan(self):
        from guard_dog import removal
        with tempfile.TemporaryDirectory() as d:
            db = DB(Path(d))
            pid = db.save_person(None, "Me", "", names=["Jane Doe"])
            for src, url in (("spokeo", "https://s/1"), ("spokeo", "https://s/2"), ("whitepages", "https://w/1")):
                scanner.record(db, pid, {"id": src, "name": src}, url, "listed", [], None)
            ids = [r["id"] for r in db.q("SELECT id FROM findings ORDER BY id")]
            for fid in ids:
                removal.move(db, fid, "manual")
            db.x("UPDATE removals SET prefill='{\"Your removal email\": \"r@x.com\"}' WHERE finding=?", (ids[1],))
            with mock.patch.object(removal, "run") as run, mock.patch.object(removal, "browser_page", return_value="WP"):
                scanner.refetch_details(db, pid)
            self.assertEqual([c.args[1]["id"] for c in run.call_args_list], [ids[0]])  # no details yet, and has a script
            self.assertEqual(run.call_args.args[3:], ("dry_run", "WP"))
            with mock.patch.object(removal, "run", side_effect=removal.BadMove("nope")):
                scanner.refetch_details(db, pid)
            self.assertTrue(any("people-search check failed (spokeo)" in a["text"] for a in db.q("SELECT text FROM alerts")))

    def test_a_scan_runs_it_after_the_people_search_pass(self):
        with tempfile.TemporaryDirectory() as d:
            db = DB(Path(d))
            pid = db.save_person(None, "Me", "", names=["Jane Doe"])
            for k, val in (("sweep", "0"), ("delay_max", "0")):
                db.set(k, val)
            with mock.patch.object(scanner, "fetch", lambda *a, **k: (404, "", "", "x")), \
                    mock.patch.object(scanner.breaches, "xposedornot", return_value=[]), \
                    mock.patch.object(scanner, "refetch_details") as rf:
                scanner.run(db, pid)
            rf.assert_called_once()


class SchedulerInbox(unittest.TestCase):
    def test_inbox_trouble_is_an_alert_and_the_watch_goes_on(self):
        with tempfile.TemporaryDirectory() as d:
            db = DB(Path(d))
            pid = db.save_person(None, "Me", "", names=["Jane Doe"])
            with mock.patch.object(scanner, "due", return_value=None), \
                    mock.patch("guard_dog.requests.send_waiting"), \
                    mock.patch("guard_dog.inbox.poll", side_effect=OSError("auth failed")), \
                    mock.patch.object(scanner.time, "sleep", side_effect=StopIteration):
                with self.assertRaises(StopIteration):
                    scanner.scheduler(db)
            self.assertIn("Couldn't read the removal inbox: OSError: auth failed", [a["text"] for a in db.q("SELECT text FROM alerts")])


class SchedulerInboxCadence(unittest.TestCase):
    def test_the_inbox_is_read_at_most_every_15_minutes(self):
        with tempfile.TemporaryDirectory() as d:
            db = DB(Path(d))
            sleeps = [None, StopIteration]
            with mock.patch.object(scanner, "due", return_value=None), mock.patch("guard_dog.requests.send_waiting"), \
                    mock.patch("guard_dog.inbox.poll") as poll, \
                    mock.patch.object(scanner.time, "sleep", side_effect=lambda s: (_ for _ in ()).throw(StopIteration) if sleeps.pop(0) else None):
                with self.assertRaises(StopIteration):
                    scanner.scheduler(db)
            self.assertEqual(poll.call_count, 1)  # two passes of the loop, five minutes apart: one look


class PasswordReset(unittest.TestCase):
    """The admin-only reset (packaging/deb/reset-password writes the marker): clear once, never in a loop."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.tmp.name))
        self.db.set("password_hash", "salt$hash")

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_reset_closes_the_dashboard_to_the_network(self):
        self.db.set("network", "1")
        (self.db.dir / "reset-password").write_text("")
        self.assertTrue(entry.password_reset(self.db))
        self.assertEqual(self.db.get("network"), "0")
        self.assertEqual(self.db.q("SELECT text FROM alerts WHERE person IS NULL")[0]["text"],
                         "Dashboard closed to the network again: its password was reset")

    def test_no_marker_nothing_happens(self):
        self.assertFalse(entry.password_reset(self.db))
        self.assertEqual(self.db.get("password_hash"), "salt$hash")

    def test_a_marker_that_cant_be_removed_is_not_acted_on_twice(self):
        (self.db.dir / "reset-password").write_text("")
        with mock.patch.object(entry.Path, "unlink", side_effect=PermissionError("no")):
            self.assertTrue(entry.password_reset(self.db))
        self.assertEqual(self.db.get("password_hash"), "")
        self.db.set("password_hash", "salt$new")  # the owner set a new one
        self.assertFalse(entry.password_reset(self.db))  # same marker, next start: ignored
        self.assertEqual(self.db.get("password_hash"), "salt$new")
        import os
        st = (self.db.dir / "reset-password").stat()
        os.utime(self.db.dir / "reset-password", ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))  # a fresh request
        self.assertTrue(entry.password_reset(self.db))
        self.assertEqual(self.db.get("password_hash"), "")

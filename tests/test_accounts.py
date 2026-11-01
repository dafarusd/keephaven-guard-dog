"""Accounts by username (maigret): maigret itself is faked; nothing is installed and no site is asked."""
import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from guard_dog import accounts, scanner
from guard_dog.db import DB

HIT = {"Medium": {"username": "janed", "url_user": "https://medium.com/@janed", "is_similar": False,
                  "status": {"status": "Claimed", "url": "https://medium.com/@janed", "tags": ["blog", "writing"]}},
       "Similar": {"username": "jane-d", "url_user": "https://x.example/jane-d", "is_similar": True,
                   "status": {"status": "Claimed", "url": "https://x.example/jane-d", "tags": []}},
       "Gone": {"username": "janed", "url_user": "https://y.example/janed", "status": {"status": "Available"}}}


NOISY = {"ChaturBate": {"url_user": "https://chaturbate.com/x", "status": {"status": "Claimed", "tags": ["porn"]}}}


def fake_maigret(report=HIT, code=0, hang_for=(), control_report=None):
    """calls: the real usernames asked (the made-up control name is kept apart, in .control)."""
    calls = []
    run_calls = []

    def run(cmd, **k):
        user = cmd[4]
        is_control = user.startswith("gd") and len(user) == 13
        (run.control if is_control else calls).append((cmd, k))
        if (user in hang_for) or (is_control and "control" in hang_for):
            raise subprocess.TimeoutExpired(cmd, k["timeout"])
        out = Path(cmd[cmd.index("-fo") + 1])
        if code == 0:
            (out / f"report_{user}_simple.json").write_text(json.dumps(control_report or {} if is_control else report))
        return subprocess.CompletedProcess(cmd, code, b"", b"")
    run.control = run_calls
    return run, calls


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.tmp.name))
        self.pid = self.db.save_person(None, "Me", "", names=["Jane Doe"], usernames=["janed"])
        self.site = mock.patch.object(accounts, "site_list", return_value="/opt/maigret/resources/data.json")
        self.site.start()

    def tearDown(self):
        self.site.stop()
        self.tmp.cleanup()


class Running(Base):
    def test_the_command_is_pinned_and_quiet(self):
        cmd = accounts.command("janed", "/tmp/o", "http://p:1")
        self.assertEqual(cmd[1:5], ["-I", "-m", "maigret", "janed"])
        for flag in ("--no-autoupdate", "--no-recursion", "--no-extracting"):
            self.assertIn(flag, cmd)
        self.assertEqual(cmd[cmd.index("--db") + 1], "/opt/maigret/resources/data.json")  # the bundled list, never a URL
        self.assertEqual(cmd[cmd.index("--top-sites") + 1], "500")
        self.assertEqual(cmd[-2:], ["--proxy", "http://p:1"])
        for never in ("--ai", "--cloudflare-bypass", "--self-check", "--submit", "-a", "--all-sites", "--permute"):
            self.assertNotIn(never, cmd)
        self.assertNotIn("--proxy", accounts.command("janed", "/tmp/o"))

    def test_only_claimed_accounts_that_arent_guesses(self):
        self.assertEqual(accounts.parse(HIT), [dict(site="Medium", url="https://medium.com/@janed", tags="blog, writing")])
        self.assertEqual(accounts.parse({}), [])

    def test_check_runs_once_per_username_with_its_cache_in_our_folder(self):
        run, calls = fake_maigret()
        self.db.save_person(self.pid, "Me", "", names=["Jane Doe"], usernames=["janed", "jdoe77"])
        found, problems = accounts.check(self.db, self.db.person(self.pid), run=run)
        self.assertEqual(problems, [])
        self.assertEqual([c[0][4] for c in calls], ["janed", "jdoe77"])
        self.assertEqual(calls[0][1]["env"]["HOME"], str(self.db.dir / "maigret"))
        self.assertEqual(calls[0][1]["timeout"], accounts.RUN_LIMIT)
        self.assertEqual([(a["site"], a["username"]) for a in found], [("Medium", "janed"), ("Medium", "jdoe77")])
        self.assertEqual(list((self.db.dir / "maigret").iterdir()), [])  # each run's report folder is cleaned up

    def test_a_stuck_or_failed_run_is_a_problem_and_the_rest_go_on(self):
        self.db.save_person(self.pid, "Me", "", names=["Jane Doe"], usernames=["janed", "jdoe77"])
        run, _ = fake_maigret(hang_for=("janed",))
        found, problems = accounts.check(self.db, self.db.person(self.pid), run=run)
        self.assertEqual((len(found), problems), (1, ["janed: took longer than 15 minutes"]))
        run, _ = fake_maigret(code=2)
        found, problems = accounts.check(self.db, self.db.person(self.pid), run=run)
        self.assertEqual((found, problems), ([], ["the made-up name check: maigret stopped (2)"]))  # nothing kept

    def test_sites_that_find_a_made_up_name_are_left_out(self):
        report = dict(HIT, **NOISY)
        run, calls = fake_maigret(report, control_report=NOISY)
        found, problems = accounts.check(self.db, self.db.person(self.pid), run=run)
        self.assertEqual([a["site"] for a in found], ["Medium"])
        self.assertEqual(len(run.control), 1)
        made_up = run.control[0][0][4]
        self.assertNotEqual(made_up, accounts.control())  # a new one each time
        self.assertRegex(made_up, r"^gd[a-z2-9]{11}$")
        run, calls = fake_maigret(hang_for=("control",))
        found, problems = accounts.check(self.db, self.db.person(self.pid), run=run)
        self.assertEqual((found, calls), ([], []))
        self.assertEqual(problems, ["the made-up name check: took longer than 15 minutes"])

    def test_no_usernames_runs_nothing(self):
        run, calls = fake_maigret()
        self.db.save_person(self.pid, "Me", "", names=["Jane Doe"])
        self.assertEqual(accounts.check(self.db, self.db.person(self.pid), run=run), ([], []))
        self.assertEqual(run.control, [])

    def test_cancel_stops_before_the_next_username(self):
        run, calls = fake_maigret()
        accounts.check(self.db, self.db.person(self.pid), run=run, cancelled=lambda: True)
        self.assertEqual((calls, run.control), ([], []))
        n = []
        accounts.check(self.db, self.db.person(self.pid), run=run, cancelled=lambda: n.append(1) or len(n) > 1)
        self.assertEqual((len(run.control), calls), (1, []))  # cancelled after the made-up name


class Scans(Base):
    def scan(self, run):
        with mock.patch.object(accounts.subprocess, "run", run), mock.patch.object(accounts, "installed", return_value=True), \
                mock.patch.object(scanner, "BROKERS", []):
            self.db.set("sweep", "0")
            self.db.set("archive", "0")
            scanner.run(self.db, self.pid)

    def test_off_by_default_and_off_when_not_installed(self):
        run, calls = fake_maigret()
        self.scan(run)
        self.assertEqual(calls, [])  # the switch is off by default
        self.db.set("accounts", "1")
        with mock.patch.object(accounts, "installed", return_value=False):
            self.assertFalse(accounts.due(self.db, self.db.person(self.pid)))

    def test_weekly_news_after_the_first_look(self):
        self.db.set("accounts", "1")
        run, calls = fake_maigret()
        self.scan(run)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.db.q("SELECT site, username, url, tags FROM accounts")[0]["url"], "https://medium.com/@janed")
        self.assertFalse([a for a in self.db.q("SELECT text FROM alerts") if "account" in a["text"]])
        self.scan(run)
        self.assertEqual(len(calls), 1)  # within the week
        self.db.x("UPDATE people SET accounts_checked=accounts_checked-8*86400")
        more = dict(HIT, GitHub={"url_user": "https://github.com/janed", "status": {"status": "Claimed", "tags": ["coding"]}})
        run2, _ = fake_maigret(more)
        self.scan(run2)
        texts = [a["text"] for a in self.db.q("SELECT text FROM alerts")]
        self.assertIn("New account with the username janed on GitHub.", texts)
        self.assertEqual(sum("New account" in t for t in texts), 1)

    def test_a_failed_look_is_tried_again_and_says_so_plainly(self):
        from guard_dog import web
        self.db.set("accounts", "1")
        run, _ = fake_maigret(code=1)
        self.scan(run)
        self.assertIsNone(self.db.person(self.pid)["accounts_checked"])
        t = [a["text"] for a in self.db.q("SELECT text FROM alerts") if a["text"].startswith("accounts check: ")][0]
        self.assertFalse(web.worth_telling(t))
        self.assertIn("Couldn't finish looking for accounts", web.plain_alert(t))

    def test_new_usernames_are_looked_at_again_and_deleting_clears_them(self):
        self.db.set("accounts", "1")
        run, calls = fake_maigret()
        self.scan(run)
        self.db.save_person(self.pid, "Me", "", names=["Jane Doe"], usernames=["janed", "jdoe77"])
        self.scan(run)
        self.assertEqual(len(calls), 3)
        self.db.delete_person(self.pid)
        self.assertEqual(self.db.q("SELECT COUNT(*) n FROM accounts")[0]["n"], 0)

    def test_deleted_mid_look_writes_nothing(self):
        self.db.set("accounts", "1")

        def check(db, person, cancelled):
            scanner._cancel.add(self.pid)
            return [dict(site="Medium", username="janed", url="u", tags="")], []
        with mock.patch.object(accounts, "check", check), mock.patch.object(accounts, "installed", return_value=True), \
                mock.patch.object(scanner, "BROKERS", []):
            self.db.set("sweep", "0")
            self.db.set("archive", "0")
            scanner._running[self.pid] = True
            scanner.run(self.db, self.pid)
        self.assertEqual(self.db.q("SELECT COUNT(*) n FROM accounts")[0]["n"], 0)


class Installing(unittest.TestCase):
    def setUp(self):
        accounts.INSTALL.update(state="", note="")
        p = mock.patch.object(accounts, "packaged", return_value=False)  # the install.sh edition, even in the .deb build
        p.start()
        self.addCleanup(p.stop)

    def wait(self):
        for _ in range(100):
            if accounts.INSTALL["state"] != "installing":
                return
            time.sleep(0.02)

    def test_pinned_into_our_own_python(self):
        seen = []

        def run(cmd, **k):
            seen.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        with mock.patch.object(accounts, "installed", return_value=True):
            accounts.install(run=run)
            self.wait()
        self.assertEqual(seen[0][1:4], ["-m", "pip", "install"])
        self.assertIn("--require-hashes", seen[0])
        self.assertEqual(seen[0][-2:], ["-r", str(accounts.LOCK)])
        self.assertNotIn("maigret==0.6.6", seen[0])  # only what the lock lists, with its hashes
        self.assertEqual(accounts.INSTALL["state"], "done")

    def test_no_lock_no_install(self):
        with mock.patch.object(accounts, "LOCK", Path("/nonexistent/accounts-lock.txt")), \
                mock.patch.object(accounts.threading, "Thread") as t:
            accounts.install()
        t.assert_not_called()
        self.assertEqual(accounts.INSTALL, {"state": "failed",
                                            "note": "the list of checked packages is missing from this install"})

    def test_the_lock_pins_every_package_with_hashes(self):
        text = accounts.LOCK.read_text()
        self.assertIn("--index-url https://pypi.org/simple", text)
        reqs = [r for r in text.replace("\\\n", " ").splitlines() if r.strip() and not r.lstrip().startswith(("#", "--"))]
        self.assertGreater(len(reqs), 60)
        for r in reqs:
            name = r.split(";")[0].split()[0]
            self.assertRegex(name, r"^[A-Za-z0-9_.-]+==[\w.]+$", r[:60])
            self.assertIn("--hash=sha256:", r, name)
        self.assertIn(accounts.PACKAGE, [r.split()[0] for r in reqs])

    def test_a_failure_keeps_pips_last_line(self):
        run = lambda cmd, **k: subprocess.CompletedProcess(cmd, 1, "", "Collecting...\nERROR: No matching distribution")
        accounts.install(run=run)
        self.wait()
        self.assertEqual(accounts.INSTALL, {"state": "failed", "note": "ERROR: No matching distribution"})
        accounts.install(run=lambda cmd, **k: subprocess.CompletedProcess(cmd, 1, "", ""))
        self.wait()
        self.assertEqual(accounts.INSTALL["note"], "pip stopped (1)")

        def boom(cmd, **k):
            raise subprocess.TimeoutExpired(cmd, 1800)
        accounts.install(run=boom)
        self.wait()
        self.assertTrue(accounts.INSTALL["note"].startswith("TimeoutExpired"))

    def test_not_in_the_packaged_edition(self):
        run = mock.Mock()
        with mock.patch.object(accounts, "packaged", return_value=True), \
                mock.patch.object(accounts.threading, "Thread") as t:
            accounts.install(run=run)
        t.assert_not_called()
        run.assert_not_called()
        self.assertEqual(accounts.INSTALL, {"state": "failed", "note": accounts.NOT_HERE})

    def test_one_at_a_time(self):
        accounts.INSTALL.update(state="installing")
        with mock.patch.object(accounts.threading, "Thread") as t:
            accounts.install()
        t.assert_not_called()

    def test_installed_and_site_list_follow_the_package(self):
        spec = mock.Mock(origin="/opt/x/maigret/__init__.py")
        with mock.patch.object(accounts.importlib.util, "find_spec", return_value=spec):
            self.assertTrue(accounts.installed())
            self.assertEqual(accounts.site_list(), "/opt/x/maigret/resources/data.json")
        with mock.patch.object(accounts.importlib.util, "find_spec", return_value=None):
            self.assertFalse(accounts.installed())


if __name__ == "__main__":
    unittest.main()

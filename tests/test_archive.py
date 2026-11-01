"""Old profile copies in the Internet Archive: the CDX server is faked, nothing goes to web.archive.org."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from guard_dog import archive, scanner
from guard_dog.db import DB


def cdx_answer(archived):
    """A fake _get: archived = {address: [timestamps]}; everything else was never archived."""
    asked = []

    def get(url, headers=None, timeout=30):
        from urllib.parse import parse_qs, urlsplit
        q = parse_qs(urlsplit(url).query)
        asked.append(q["url"][0])
        stamps = archived.get(q["url"][0])
        if stamps == 429:
            return 429, b""
        rows = [["timestamp", "original", "statuscode"]] + [[s, "https://" + q["url"][0], "200"] for s in stamps or []]
        return 200, json.dumps(rows if stamps else []).encode()
    return get, asked


class Usernames(unittest.TestCase):
    def test_theirs_first_else_the_email_names(self):
        self.assertEqual(archive.usernames({"usernames": ["@JaneD", "jd", "jane d", "janed"], "emails": ["x@e.com"]}),
                         ["janed"])  # too short and not a username shape are skipped, repeats once
        self.assertEqual(archive.usernames({"usernames": [], "emails": ["jane.doe@e.com", "jd@e.com"]}), ["jane.doe"])
        self.assertEqual(archive.usernames({"usernames": [], "emails": []}), [])


class Check(unittest.TestCase):
    def test_hits_with_their_dates_and_the_newest_copy(self):
        get, asked = cdx_answer({"github.com/janed": ["20150102030405", "20190607080910"]})
        slept = []
        with mock.patch.object(archive, "_get", get), mock.patch.object(archive, "PAUSE", 1.5):
            hits, problems = archive.check({"usernames": ["janed"]}, sleep=slept.append)
        self.assertEqual(problems, [])
        self.assertEqual(len(asked), 15)
        self.assertIn("janed.tumblr.com", asked)
        self.assertEqual(hits, [dict(platform="GitHub", username="janed", url="https://github.com/janed", snapshots=2,
                                     first="20150102", last="20190607",
                                     copy="https://web.archive.org/web/20190607080910/https://github.com/janed")])
        self.assertEqual(slept, [1.5] * 14)  # paced, and no wait after the last one

    def test_slow_down_once_then_stop_for_this_scan(self):
        get, asked = cdx_answer({"facebook.com/janed": 429})
        slept = []
        with mock.patch.object(archive, "_get", get), mock.patch.object(archive, "BACKOFF", 20):
            hits, problems = archive.check({"usernames": ["janed"]}, sleep=slept.append)
        self.assertEqual(asked.count("facebook.com/janed"), 2)  # one retry after the wait
        self.assertIn(20, slept)
        self.assertEqual(len(asked), 4)  # Twitter, Instagram, Facebook twice: then it stops
        self.assertIn("asked to slow down", problems[0])

    def test_a_failure_is_one_problem_and_the_rest_go_on(self):
        def get(url, headers=None, timeout=30):
            return (503, b"") if "reddit" in url else (200, b"[]")
        with mock.patch.object(archive, "_get", get):
            hits, problems = archive.check({"usernames": ["janed"]}, sleep=lambda s: None)
        self.assertEqual((hits, len(problems)), ([], 1))
        self.assertIn("Reddit (janed): RuntimeError: the Internet Archive answered 503", problems[0])

    def test_cancel_stops_it(self):
        get, asked = cdx_answer({})
        with mock.patch.object(archive, "_get", get):
            archive.check({"usernames": ["janed"]}, sleep=lambda s: None, cancelled=lambda: len(asked) >= 3)
        self.assertEqual(len(asked), 3)


class Scans(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.tmp.name))
        self.pid = self.db.save_person(None, "Me", "", names=["Jane Doe"], usernames=["janed"])
        self.db.set("sweep", "0")

    def tearDown(self):
        self.tmp.cleanup()

    def scan(self, archived):
        get, asked = cdx_answer(archived)
        with mock.patch.object(archive, "_get", get), mock.patch.object(scanner, "BROKERS", []):
            scanner.run(self.db, self.pid)
        return asked

    def test_weekly_news_only_after_the_first_look_and_again_when_usernames_change(self):
        self.assertEqual(len(self.scan({"github.com/janed": ["20150102030405"]})), 15)
        self.assertEqual(self.db.q("SELECT COUNT(*) n FROM ghosts")[0]["n"], 1)
        self.assertFalse([a for a in self.db.q("SELECT text FROM alerts") if "Internet Archive" in a["text"]])
        self.assertEqual(self.scan({}), [])  # within the week: not asked again
        self.db.x("UPDATE people SET archive_checked=archive_checked-8*86400")
        self.scan({"github.com/janed": ["20150102030405", "20200101000000"], "keybase.io/janed": ["20180101000000"]})
        g = {r["url"]: dict(r) for r in self.db.q("SELECT * FROM ghosts")}
        self.assertEqual(g["https://github.com/janed"]["snapshots"], 2)
        texts = [a["text"] for a in self.db.q("SELECT text FROM alerts")]
        self.assertIn("The Internet Archive has an old copy of a Keybase profile named janed.", texts)
        self.db.save_person(self.pid, "Me", "", names=["Jane Doe"], usernames=["janed", "jdoe77"])
        self.assertEqual(len(self.scan({})), 30)  # a new username: looked at again right away

    def test_a_cut_short_look_is_tried_again_next_scan(self):
        self.scan({"twitter.com/janed": 429})
        self.assertIsNone(self.db.person(self.pid)["archive_checked"])
        self.assertTrue(any(a["text"].startswith("archive check: 1 problem") for a in self.db.q("SELECT text FROM alerts")))
        self.assertEqual(len(self.scan({})), 15)
        self.assertIsNotNone(self.db.person(self.pid)["archive_checked"])

    def test_off_in_settings_or_no_usernames_means_no_request(self):
        self.db.set("archive", "0")
        self.assertEqual(self.scan({}), [])
        self.db.set("archive", "1")
        self.db.save_person(self.pid, "Me", "", names=["Jane Doe"])
        self.assertEqual(self.scan({}), [])

    def test_deleting_a_person_deletes_their_copies(self):
        self.scan({"github.com/janed": ["20150102030405"]})
        self.db.delete_person(self.pid)
        self.assertEqual(self.db.q("SELECT COUNT(*) n FROM ghosts")[0]["n"], 0)


if __name__ == "__main__":
    unittest.main()


class Edges(unittest.TestCase):
    def test_the_alert_in_plain_words_and_not_as_news(self):
        from guard_dog import web
        t = "archive check: 1 problem: the Internet Archive asked to slow down"
        self.assertFalse(web.worth_telling(t))
        self.assertEqual(web.plain_alert(t), "Couldn't finish looking for old profile copies in the Internet Archive; "
                                             "it's tried again on the next scan.")

    def test_deleted_mid_look_writes_nothing(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = DB(Path(tmp.name))
        pid = db.save_person(None, "Me", "", names=["Jane Doe"], usernames=["janed"])

        def check(person, cancelled):
            scanner._cancel.add(pid)
            return [dict(platform="GitHub", username="janed", url="u", snapshots=1, first="2015", last="2015", copy="c")], []
        with mock.patch.object(archive, "check", check), mock.patch.object(scanner, "BROKERS", []):
            scanner._running[pid] = True
            scanner.run(db, pid)
        self.assertEqual(db.q("SELECT COUNT(*) n FROM ghosts")[0]["n"], 0)
        self.assertIsNone(db.person(pid))

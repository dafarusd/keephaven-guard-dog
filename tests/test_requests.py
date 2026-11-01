"""Removal letters and sending them — the mail server is faked, nothing is sent anywhere."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from guard_dog import letters, requests, scanner
from guard_dog.db import DB

ME = {"names": ["Jane Doe"], "label": "Me", "home_state": "OH", "home_country": "US", "places": ["Columbus OH", "San Diego CA"]}
SPOKEO = {"id": "spokeo", "name": "Spokeo", "region": "us", "category": "people-search", "request_email": "privacy@spokeo.com"}


class Letters(unittest.TestCase):
    def test_the_approved_generic_letter_exactly(self):
        finding = {"status": "listed", "url": "https://www.spokeo.com/Jane-Doe/Ohio/Columbus/p1",
                   "matched": "name: Jane Doe; city: Columbus, OH; age 52"}
        subject, body, kind = letters.write(ME, SPOKEO, "removals@example.com", 7, finding)
        self.assertEqual((subject, kind), ("Request to delete my personal information [GD-7]", "generic"))
        self.assertEqual(body, """Hello Spokeo privacy team,

I'm asking you to delete all personal information you hold about me, and to stop selling, sharing or licensing it. \
Please pass this request on to anyone you've shared my information with.

If the law where I live gives me a right to have my data deleted or to opt out of its sale, I'm using that right \
with this request. If it doesn't, please delete it anyway.

To find my records:
Name: Jane Doe
Where you list me: https://www.spokeo.com/Jane-Doe/Ohio/Columbus/p1
City and state: Columbus, OH
Email for this request: removals@example.com

Please reply to this email once it's done. I won't create an account for this. If you need anything else to find \
my records, just ask.

Thank you,
Jane Doe
""")

    def test_a_listing_without_a_city_shows_only_its_address(self):
        _, body, _ = letters.write(ME, SPOKEO, "r@example.com", 4, {"status": "possible", "url": "https://s/1", "matched": "name: Jane Doe"})
        self.assertIn("Where you list me: https://s/1", body)
        self.assertNotIn("City and state", body)

    def test_no_listing_no_listing_lines(self):
        _, body, _ = letters.write(ME, SPOKEO, "r@example.com", 1, None)
        self.assertNotIn("Where you list me", body)
        self.assertNotIn("City and state", body)
        for never in ("phone", "birth", "Relatives", "street"):
            self.assertNotIn(never, body)

    def test_which_law_comes_from_where_they_live_now(self):
        self.assertEqual(letters.kind_for(ME), "generic")  # lived in California once: doesn't count
        self.assertEqual(letters.kind_for(dict(ME, home_state="CA")), "ccpa")
        self.assertEqual(letters.kind_for(dict(ME, home_country="DE")), "gdpr")
        self.assertEqual(letters.kind_for(dict(ME, home_country="GB")), "gdpr")
        s, b, k = letters.write(dict(ME, home_state="CA"), SPOKEO, "r@example.com", 2)
        self.assertIn("§ 1798.105", b)
        self.assertIn("§ 1798.130 you have 45 days", b)
        self.assertEqual(b.count("reply to this email"), 0)
        self.assertIn("If you need anything else to find my records, just ask.", " ".join(b.split()))
        self.assertTrue(s.startswith("CCPA request"))
        _, b, _ = letters.write(dict(ME, home_country="GB"), SPOKEO, "r@example.com", 3,
                                {"status": "listed", "url": "u", "matched": "city: London, GB"})
        self.assertIn("the UK GDPR, if you're in the UK", b)
        self.assertIn("City and country: London, GB", b)
        self.assertEqual(letters.FOLLOW_UP_DAYS, {"ccpa": 45, "gdpr": 30, "generic": 60})


class FakeSMTP:
    def __init__(self, fail_for=()):
        self.sent, self.fail_for, self.logged_in = [], fail_for, None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, user, pw):
        self.logged_in = (user, pw)

    def send_message(self, msg):
        if msg["To"] in self.fail_for:
            raise OSError("mailbox unavailable")
        self.sent.append(msg)


class Sending(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.tmp.name))
        self.pid = self.db.save_person(None, "Me", "", names=["Jane Doe"], places=["Columbus OH"], home_state="OH")
        scanner.record(self.db, self.pid, {"id": "spokeo", "name": "Spokeo"}, "https://www.spokeo.com/x", "listed",
                       ["name: Jane Doe", "city: Columbus, OH"], None)

    def tearDown(self):
        self.tmp.cleanup()

    def ready(self):
        requests.save_inbox(self.db, "removals@example.com", "abcd efgh")

    def test_inbox_file_is_owner_only_and_keeps_the_password(self):
        self.ready()
        f = self.db.dir / requests.SECRET
        self.assertEqual(f.stat().st_mode & 0o777, 0o600)
        requests.save_inbox(self.db, "new@example.com", "")
        self.assertEqual(requests.inbox(self.db), {"address": "new@example.com", "host": "smtp.gmail.com", "port": 465,
                                                    "password": "abcd efgh"})

    def test_who_gets_one(self):
        ids = {b["id"] for b, _ in requests.targets(self.db, self.pid)}
        self.assertIn("spokeo", ids)  # found listing them
        self.assertTrue({"beenverified", "truepeoplesearch", "whitepages"} <= ids)  # people-search
        self.assertNotIn("acxiom", ids)  # marketing broker: only when asked for everyone
        everyone = {b["id"] for b, _ in requests.targets(self.db, self.pid, everyone=True)}
        self.assertGreater(len(everyone), 300)
        self.assertTrue(all(b.get("request_email") for b, _ in requests.targets(self.db, self.pid, everyone=True)))

    def test_plan_is_idempotent_and_the_found_listing_is_in_its_letter(self):
        n = len(requests.plan(self.db, self.pid))
        self.assertEqual(len(requests.plan(self.db, self.pid)), n)
        sp = self.db.q("SELECT * FROM requests WHERE broker='spokeo'")[0]
        self.assertIn("Where you list me: https://www.spokeo.com/x", sp["body"])
        self.assertIn(f"[GD-{sp['id']}]", sp["subject"])

    def test_nothing_goes_without_approval_and_then_only_the_approved(self):
        self.ready()
        requests.plan(self.db, self.pid)
        smtp = FakeSMTP()
        self.assertEqual(requests.send(self.db, self.pid, connect=lambda: smtp), (0, 0, 0))
        self.assertEqual(smtp.sent, [])
        n = requests.approve(self.db, self.pid)
        sent, later, failed = requests.send(self.db, self.pid, connect=lambda: smtp)
        self.assertEqual((sent, later, failed), (n, 0, 0))
        self.assertEqual(smtp.logged_in, ("removals@example.com", "abcd efgh"))
        m = next(x for x in smtp.sent if x["To"] == "privacy@spokeo.com")
        self.assertEqual(m["From"], "removals@example.com")
        self.assertTrue(m["Subject"].startswith("Request to delete my personal information [GD-"))
        r = self.db.q("SELECT * FROM requests WHERE broker='spokeo'")[0]
        self.assertEqual(r["state"], "sent")
        self.assertAlmostEqual(r["follow_up"] - r["sent"], 60 * 86400, delta=1)

    def test_daily_cap_and_the_rest_later(self):
        self.ready()
        requests.plan(self.db, self.pid, everyone=True)
        requests.approve(self.db, self.pid)
        with mock.patch.object(requests, "DAILY_CAP", 3):
            smtp = FakeSMTP()
            sent, later, _ = requests.send(self.db, self.pid, connect=lambda: smtp)
            self.assertEqual(sent, 3)
            self.assertGreater(later, 0)
            self.assertEqual(requests.send(self.db, self.pid, connect=lambda: smtp)[:2], (0, later))  # cap reached today
            self.db.x("UPDATE requests SET sent=sent-90000 WHERE state='sent'")
            with mock.patch.object(requests.smtplib, "SMTP_SSL", lambda *a, **k: smtp):
                requests.send_waiting(self.db)
            self.assertEqual(len(smtp.sent), 6)
        self.assertTrue(any("Sent 3 more removal emails" in a["text"] for a in self.db.q("SELECT text FROM alerts")))

    def test_a_failed_address_is_kept_with_the_reason(self):
        self.ready()
        requests.plan(self.db, self.pid)
        requests.approve(self.db, self.pid)
        smtp = FakeSMTP(fail_for=("privacy@spokeo.com",))
        sent, _, failed = requests.send(self.db, self.pid, connect=lambda: smtp)
        self.assertEqual(failed, 1)
        self.assertIn("mailbox unavailable", self.db.q("SELECT note FROM requests WHERE broker='spokeo'")[0]["note"])

    def test_not_ready_without_inbox_or_consent(self):
        requests.plan(self.db, self.pid)
        with self.assertRaises(requests.NotReady):
            requests.approve(self.db, self.pid)
        self.ready()
        mom = self.db.save_person(None, "Mom", "", names=["Mary Doe"])
        with self.assertRaises(requests.NotReady):
            requests.check_ready(self.db, self.db.person(mom))
        self.db.save_person(mom, "Mom", "she asked me on 2026-10-29", names=["Mary Doe"])
        requests.check_ready(self.db, self.db.person(mom))  # no error now

    def test_mail_trouble_on_the_schedule_becomes_an_alert(self):
        self.ready()
        requests.plan(self.db, self.pid)
        requests.approve(self.db, self.pid)

        def boom(*a, **k):
            raise OSError("535 Username and Password not accepted")
        with mock.patch.object(requests.smtplib, "SMTP_SSL", boom):
            requests.send_waiting(self.db)
        self.assertTrue(any("Couldn't send removal emails" in a["text"] for a in self.db.q("SELECT text FROM alerts")))
        self.assertGreater(len(requests.drafts(self.db, self.pid, "approved")), 0)  # kept for next time

    def test_when_todays_cap_is_used_up_nothing_more_goes(self):
        self.ready()
        requests.plan(self.db, self.pid)
        self.db.x("UPDATE requests SET state='approved'")
        with mock.patch.object(requests, "DAILY_CAP", 0), mock.patch.object(requests, "send") as s:
            requests.send_waiting(self.db)
        s.assert_not_called()

    def test_a_scheduled_run_that_sends_nothing_says_nothing(self):
        self.ready()
        requests.plan(self.db, self.pid)
        self.db.x("UPDATE requests SET state='approved'")
        with mock.patch.object(requests, "send", return_value=(0, 3, 0)):
            requests.send_waiting(self.db)
        self.assertFalse(any("more removal" in a["text"] for a in self.db.q("SELECT text FROM alerts")))

    def test_waiting_with_no_inbox_is_skipped(self):
        requests.plan(self.db, self.pid)
        self.db.x("UPDATE requests SET state='approved'")
        requests.send_waiting(self.db)  # no inbox set: nothing happens, no crash
        self.assertEqual(self.db.q("SELECT COUNT(*) n FROM requests WHERE state='sent'")[0]["n"], 0)

    def test_follow_up_after_the_clock(self):
        self.ready()
        requests.plan(self.db, self.pid)
        requests.approve(self.db, self.pid)
        requests.send(self.db, self.pid, connect=FakeSMTP)
        self.assertEqual(requests.due(self.db, self.pid), [])
        later = time.time() + 61 * 86400
        due = requests.due(self.db, self.pid, now=later)
        self.assertTrue(due)
        requests.follow_up(self.db, due[0]["id"])
        r = self.db.q("SELECT * FROM requests WHERE id=?", (due[0]["id"],))[0]
        self.assertEqual(r["state"], "draft")
        self.assertTrue(r["subject"].startswith("Follow-up: Request to delete"))
        self.assertIn("haven't had an answer", r["body"])

    def test_deleting_a_person_deletes_their_requests(self):
        requests.plan(self.db, self.pid)
        self.db.delete_person(self.pid)
        self.assertEqual(self.db.q("SELECT COUNT(*) n FROM requests")[0]["n"], 0)


if __name__ == "__main__":
    unittest.main()

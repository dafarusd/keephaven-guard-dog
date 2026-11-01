import tempfile
import unittest
from pathlib import Path

from guard_dog import removal, scanner
from guard_dog.db import DB

B = {"id": "site", "name": "Site", "checked": "2026-10-29"}


class Record(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.tmp.name))
        self.pid = self.db.save_person(None, "T", "", names=["Jane Doe"])

    def tearDown(self):
        self.tmp.cleanup()

    def status(self):
        return self.db.q("SELECT status FROM findings")[0]["status"]

    def alerts(self):
        return [a["text"] for a in self.db.q("SELECT text FROM alerts ORDER BY id")]

    def test_failed_check_keeps_listed(self):
        scanner.record(self.db, self.pid, B, "u", "listed", ["phone: x"], None)
        for s in ("needs stealth", "blocked", "error", "unconfirmed"):
            scanner.record(self.db, self.pid, B, "u", s, [], None)
            self.assertEqual(self.status(), "listed")

    def test_alerts(self):
        scanner.record(self.db, self.pid, B, "u", "listed", [], None)
        scanner.record(self.db, self.pid, B, "u", "possible", [], None)
        scanner.record(self.db, self.pid, B, "u", "listed", [], None)  # flapping, no removal: no second "new"
        self.assertEqual(self.alerts(), ["Site: new listing found."])
        fid = self.db.q("SELECT id FROM findings")[0]["id"]
        removal.move(self.db, fid, "ready")
        removal.move(self.db, fid, "requested")
        scanner.record(self.db, self.pid, B, "u", "possible", [], None)  # not proof it's gone
        scanner.record(self.db, self.pid, B, "u", "listed", [], None)
        scanner.record(self.db, self.pid, B, "u", "not found", [], None)  # one miss: not proof yet
        self.assertEqual(removal.state(self.db, fid), "requested")
        scanner.record(self.db, self.pid, B, "u", "not found", [], None)  # two in a row
        self.assertEqual(self.alerts()[1:], ["Site no longer shows the listing; proven when the next scan agrees.",
                                             "Site: the listing is gone."])
        scanner.record(self.db, self.pid, B, "u", "listed", [], None)
        self.assertEqual(self.alerts()[-1], "Site: the listing is back after it was removed. Ask them again.")
        self.assertEqual(removal.state(self.db, fid), "back")
        hist = [h["to"] for h in __import__("json").loads(self.db.q("SELECT history FROM removals")[0]["history"])]
        self.assertEqual(hist, ["ready", "requested", "gone", "back"])

    def test_a_listing_is_gone_only_when_two_scans_in_a_row_agree(self):
        """PiCloud 2026-10-30: a captcha page read as "not found" flipped two of his listings to not listed."""
        scanner.record(self.db, self.pid, B, "u", "listed", [], None)
        fid = self.db.q("SELECT id FROM findings")[0]["id"]
        scanner.record(self.db, self.pid, B, "u", "not found", [], None)
        row = self.db.q("SELECT status, check_note FROM findings")[0]
        self.assertEqual((row["status"], row["check_note"]), ("not found", scanner.UNSEEN))
        self.assertEqual(self.alerts()[-1], "Site no longer shows the listing; proven when the next scan agrees.")
        scanner.record(self.db, self.pid, B, "u", "blocked", ["http 403"], None)  # proves nothing: the wait goes on
        scanner.record(self.db, self.pid, B, "u", "not found", [], None)
        self.assertEqual(self.alerts()[-1], "Site: the listing is gone (two scans in a row didn't find it).")
        self.assertEqual(self.db.q("SELECT check_note FROM findings")[0]["check_note"], "")
        self.assertEqual(removal.state(self.db, fid), "found")  # no removal was asked: nothing to move
        scanner.record(self.db, self.pid, B, "u", "not found", [], None)  # a third miss says nothing new
        self.assertEqual(len(self.alerts()), 3)

    def test_listed_then_missed_then_listed_again(self):
        scanner.record(self.db, self.pid, B, "u", "listed", [], None)
        scanner.record(self.db, self.pid, B, "u", "not found", [], None)
        scanner.record(self.db, self.pid, B, "u", "listed", [], None)
        self.assertEqual(self.alerts()[-1], "Site: the listing shows again. Last scan didn't see it, so that miss wasn't real.")
        self.assertEqual(self.db.q("SELECT check_note FROM findings")[0]["check_note"], "")  # the count starts over
        scanner.record(self.db, self.pid, B, "u", "not found", [], None)
        self.assertEqual(self.alerts()[-1], "Site no longer shows the listing; proven when the next scan agrees.")

    def test_copies_pruned(self):
        for i in range(6):
            scanner.save_copy(self.db, self.pid, "site", "u", f"<p>{i}</p>")
        scanner.save_copy(self.db, self.pid, "site", "u", "<p>5</p>")  # unchanged page: no new file
        self.assertEqual(len(list((self.db.dir / "copies").glob("*.html"))), scanner.KEEP_COPIES)


if __name__ == "__main__":
    unittest.main()

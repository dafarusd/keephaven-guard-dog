"""Reading the removal inbox — a fake IMAP server with made-up mail; nothing is read from a real mailbox."""
import tempfile
import time
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest import mock

from guard_dog import inbox, removal, requests, scanner, web
from guard_dog.db import DB

SPOKEO = {"id": "spokeo", "name": "Spokeo", "website": "https://www.spokeo.com", "request_email": "privacy@spokeo.com",
          "category": "people-search", "region": "us", "optout": [{"url": "https://www.spokeo.com/optout"}]}
WHITEPAGES = {"id": "whitepages", "name": "Whitepages", "website": "https://www.whitepages.com",
              "request_email": "support@whitepages.com", "category": "people-search", "region": "us", "optout": []}


def mail(frm, subject, body, mid):
    m = EmailMessage()
    m["From"], m["Subject"], m["Message-ID"], m["Date"] = frm, subject, f"<{mid}@test>", "Thu, 29 Oct 2026 12:00:00 +0000"
    m.set_content(body)
    return m.as_bytes()


class FakeIMAP:
    """Records every command; raises if asked to (to test that nothing is written when a handler fails)."""

    def __init__(self, messages, raise_on_fetch=None):
        self.messages, self.calls, self.raise_on_fetch = messages, [], raise_on_fetch

    def __getattr__(self, name):  # any command we didn't expect is recorded, then fails
        def cmd(*a, **k):
            self.calls.append((name, a, k))
            raise AssertionError(f"unexpected IMAP command {name}")
        return cmd

    def login(self, u, p):
        self.calls.append(("login",))

    def select(self, box, readonly=False):
        self.calls.append(("select", box, readonly))
        return "OK", [b"1"]

    def search(self, charset, crit):
        self.calls.append(("search", crit))
        return "OK", [b" ".join(str(i + 1).encode() for i in range(len(self.messages)))]

    def fetch(self, num, parts):
        self.calls.append(("fetch", num, parts))
        if self.raise_on_fetch and num == self.raise_on_fetch:
            raise OSError("connection dropped")
        raw = self.messages[int(num) - 1]
        if "HEADER.FIELDS" in parts:
            head, _, _ = raw.partition(b"\n\n")
            return "OK", [(b"1", head + b"\n\n")]
        return "OK", [(b"1", raw)]

    def logout(self):
        self.calls.append(("logout",))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.tmp.name))
        self.pid = self.db.save_person(None, "Me", "", names=["Jane Doe"])
        requests.save_inbox(self.db, "removals@example.com", "pw")
        self.p = mock.patch.object(requests, "brokers", return_value=[SPOKEO, WHITEPAGES])
        self.p.start()
        requests.plan(self.db, self.pid)
        self.db.x("UPDATE requests SET state='sent', sent=?", (time.time() - 3 * 86400,))
        self.rid = {r["broker"]: r["id"] for r in self.db.q("SELECT id, broker FROM requests")}
        self.opened = []

    def tearDown(self):
        self.p.stop()
        self.tmp.cleanup()

    def poll(self, messages, **k):
        self.imap = FakeIMAP(messages, **k)
        return inbox.poll(self.db, connect=lambda: self.imap, open_page=lambda url, steps: self.opened.append(url))

    def writes(self):
        return [c for c in self.imap.calls if c[0] not in inbox.ALLOWED]


class Sorting(Base):
    def test_tagged_confirmation_closes_the_request(self):
        n = self.poll([mail("Spokeo <privacy@spokeo.com>", f"Re: Request to delete [GD-{self.rid['spokeo']}]",
                            "Your information has been removed from our site.", "a")])
        self.assertEqual(n, 1)
        r = self.db.q("SELECT state, reply FROM requests WHERE broker='spokeo'")[0]
        self.assertEqual((r["state"], r["reply"]), ("confirmed", "confirmed"))
        self.assertIn("Spokeo says it's done; the next scan checks it's gone.", [a["text"] for a in self.db.q("SELECT text FROM alerts")])
        self.assertEqual(self.writes(), [])
        self.assertIn(("select", "INBOX", True), self.imap.calls)
        self.assertTrue(all("PEEK" in c[2] for c in self.imap.calls if c[0] == "fetch"))

    def test_verification_link_followed_only_when_its_theirs(self):
        self.poll([mail("Spokeo <noreply@mail.spokeo.com>", "Confirm your opt-out",
                        "Click the link below to confirm your opt-out: https://www.spokeo.com/optout/confirm?token=SECRET123",
                        "b")])
        self.assertEqual(self.opened, ["https://www.spokeo.com/optout/confirm?token=SECRET123"])
        row = self.db.q("SELECT state, link FROM mail")[0]
        self.assertEqual((row["state"], row["link"]), ("done", "www.spokeo.com/optout/confirm"))  # never the token
        self.assertNotIn("SECRET123", str([dict(r) for r in self.db.q("SELECT * FROM mail")]))

    def test_a_link_to_someone_else_or_a_spoofed_sender_is_never_followed(self):
        self.poll([mail("Spokeo <noreply@spokeo.com>", "Confirm your opt-out",
                        "Click the link below to confirm your opt-out: https://evil.example/spokeo", "c"),
                   mail("Spokeo <help@spoke0.example>", "Confirm your opt-out",
                        "Click the link below to confirm your opt-out: https://www.spokeo.com/optout/confirm", "d")])
        self.assertEqual(self.opened, [])
        # the look-alike sender isn't any broker's: kept out of the queue, but counted as ignored
        self.assertEqual(sorted(r["state"] for r in self.db.q("SELECT state FROM mail")), ["ignored", "needs_you"])

    def test_a_tag_from_a_stranger_is_not_trusted(self):
        self.poll([mail("Someone <x@other.example>", f"Re: [GD-{self.rid['spokeo']}] done",
                        "Your information has been removed.", "e")])
        self.assertEqual(self.db.q("SELECT state FROM requests WHERE broker='spokeo'")[0]["state"], "sent")
        self.assertEqual(self.db.q("SELECT state, broker FROM mail")[0]["state"], "needs_you")

    def test_untagged_mail_matches_only_one_open_request(self):
        mom = self.db.save_person(None, "Mom", "she asked", names=["Mary Doe"])
        requests.plan(self.db, mom)
        self.db.x("UPDATE requests SET state='sent', sent=? WHERE person=?", (time.time(), mom))
        self.poll([mail("Spokeo <privacy@spokeo.com>", "Your request", "Your information has been removed.", "f")])
        self.assertEqual({r["state"] for r in self.db.q("SELECT state FROM requests WHERE broker='spokeo'")}, {"sent"})
        self.assertEqual(self.db.q("SELECT state FROM mail")[0]["state"], "needs_you")

    def test_other_answers_wait_for_the_owner(self):
        self.poll([mail("Whitepages <support@whitepages.com>", f"[GD-{self.rid['whitepages']}]",
                        "We could not locate any record matching your request.", "g"),
                   mail("Spokeo <privacy@spokeo.com>", f"[GD-{self.rid['spokeo']}]",
                        "We are unable to process your request.", "h")])
        texts = [a["text"] for a in self.db.q("SELECT text FROM alerts")]
        self.assertIn("Whitepages says it has no record — check it: the email is in the removal inbox.", texts)
        self.assertIn("Spokeo turned the request down — check it: the email is in the removal inbox.", texts)
        self.assertEqual(self.db.q("SELECT reply FROM requests WHERE broker='spokeo'")[0]["reply"], "rejected")

    def test_classes(self):
        c = inbox.classify
        self.assertEqual(c("", "Please click the link below to confirm. Your data will be removed."), "needs_verification")
        self.assertEqual(c("", "No records were found for you."), "no_record")
        self.assertEqual(c("", "Your request has been denied."), "rejected")
        self.assertEqual(c("", "Your opt-out is complete."), "confirmed")
        self.assertEqual(c("", "Thanks for writing."), "unknown")

    def test_each_mail_is_read_once(self):
        msgs = [mail("Spokeo <privacy@spokeo.com>", f"[GD-{self.rid['spokeo']}]", "Thanks for writing.", "i")]
        self.assertEqual(self.poll(msgs), 1)
        self.assertEqual(self.poll(msgs), 0)

    def test_a_removal_waiting_on_the_site_is_confirmed_by_its_email(self):
        self.db.x("DELETE FROM requests")
        scanner.record(self.db, self.pid, {"id": "spokeo", "name": "Spokeo"}, "https://www.spokeo.com/s", "listed", [], None)
        fid = self.db.q("SELECT id FROM findings")[0]["id"]
        removal.move(self.db, fid, "ready")
        removal.move(self.db, fid, "requested")
        self.poll([mail("Spokeo <privacy@spokeo.com>", "Opt-out", "Your listing has been removed.", "j")])
        self.assertEqual(removal.state(self.db, fid), "confirmed")


class Safety(Base):
    def test_no_poll_when_nothing_is_open(self):
        self.db.x("UPDATE requests SET reply='confirmed'")
        connect = mock.Mock()
        self.assertEqual(inbox.poll(self.db, connect=connect, open_page=lambda *a: None), 0)
        connect.assert_not_called()

    def test_no_write_even_when_a_message_breaks_mid_poll(self):
        with self.assertRaises(OSError):
            self.poll([mail("Spokeo <privacy@spokeo.com>", "x", "y", "k"), mail("a@b.example", "x", "y", "l")],
                      raise_on_fetch=b"2")
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.imap.calls[-1], ("logout",))

    def test_the_wrapper_refuses_writes(self):
        ro = inbox.ReadOnly(mock.Mock())
        for name in ("store", "expunge", "copy", "move", "append", "delete"):
            with self.assertRaises(PermissionError):
                getattr(ro, name)
        with self.assertRaises(PermissionError):
            ro.select("INBOX", readonly=False)
        with self.assertRaises(PermissionError):
            ro.fetch(b"1", "(RFC822)")

    def test_tls_is_verified(self):
        with mock.patch.object(inbox.imaplib, "IMAP4_SSL") as ssl_client, mock.patch.object(inbox, "ReadOnly") as ro:
            ro.return_value.search.return_value = ("OK", [b""])
            inbox.poll(self.db, open_page=lambda *a: None)
        ctx = ssl_client.call_args.kwargs["ssl_context"]
        self.assertTrue(ctx.check_hostname)
        self.assertEqual(ctx.verify_mode, inbox.ssl.CERT_REQUIRED)

    def test_old_mail_rows_are_forgotten(self):
        self.db.x("INSERT INTO mail(msgid, state, seen) VALUES('old', 'done', ?)", (time.time() - 200 * 86400,))
        self.db.x("UPDATE requests SET reply='x'")
        inbox.poll(self.db, connect=mock.Mock(), open_page=lambda *a: None)
        self.assertEqual(self.db.q("SELECT COUNT(*) n FROM mail")[0]["n"], 0)


if __name__ == "__main__":
    unittest.main()


class Edges(Base):
    def test_html_only_mail_and_bad_date_and_logout_failure(self):
        m = EmailMessage()
        m["From"], m["Subject"], m["Message-ID"], m["Date"] = "privacy@spokeo.com", f"[GD-{self.rid['spokeo']}]", "<x@t>", "not a date"
        m.set_content("<p>Your information has been <b>removed</b>.</p>", subtype="html")
        imap = FakeIMAP([m.as_bytes()])
        imap.logout = mock.Mock(side_effect=OSError("gone"))
        inbox.poll(self.db, connect=lambda: imap, open_page=lambda *a: None)
        self.assertEqual(self.db.q("SELECT state FROM requests WHERE broker='spokeo'")[0]["state"], "confirmed")
        self.assertAlmostEqual(self.db.q("SELECT date FROM mail")[0]["date"], time.time(), delta=60)
        empty = EmailMessage()
        empty.add_attachment(b"x", maintype="application", subtype="pdf")
        self.assertEqual(inbox.body_text(empty), "")

    def test_a_header_that_wont_come_is_skipped(self):
        imap = FakeIMAP([mail("privacy@spokeo.com", "x", "y", "q")])
        imap.fetch = lambda num, parts: ("NO", [None])
        self.assertEqual(inbox.poll(self.db, connect=lambda: imap, open_page=lambda *a: None), 0)

    def test_no_inbox_set_no_poll(self):
        (self.db.dir / requests.SECRET).unlink()
        connect = mock.Mock()
        self.assertEqual(inbox.poll(self.db, connect=connect), 0)
        connect.assert_not_called()

    def test_the_real_browser_is_used_when_none_is_given(self):
        with mock.patch("guard_dog.removal.browser_page", return_value=lambda url, steps: self.opened.append(url)) as bp:
            self.imap = FakeIMAP([mail("noreply@spokeo.com", "Confirm your opt-out",
                                       "Click the link below to confirm: https://www.spokeo.com/c", "r")])
            inbox.poll(self.db, connect=lambda: self.imap)
        bp.assert_called_once()
        self.assertEqual(self.opened, ["https://www.spokeo.com/c"])

    def test_a_link_that_wont_open_is_left_to_the_owner(self):
        def broken(url, steps):
            raise TimeoutError("page took too long")
        self.imap = FakeIMAP([mail("noreply@spokeo.com", "Confirm your opt-out",
                                   "Click the link below to confirm: https://www.spokeo.com/c", "s")])
        inbox.poll(self.db, connect=lambda: self.imap, open_page=broken)
        self.assertIn("Spokeo asked to confirm the request; Guard Dog couldn't open their link (TimeoutError). "
                      "Open the email in the removal inbox and click it.", [a["text"] for a in self.db.q("SELECT text FROM alerts")])
        self.assertEqual(self.db.q("SELECT state FROM mail")[0]["state"], "needs_you")

    def test_removal_only_link_and_confirmation_when_already_further(self):
        self.db.x("DELETE FROM requests")
        scanner.record(self.db, self.pid, {"id": "spokeo", "name": "Spokeo"}, "https://www.spokeo.com/s", "listed", [], None)
        fid = self.db.q("SELECT id FROM findings")[0]["id"]
        removal.move(self.db, fid, "manual")
        self.poll([mail("noreply@spokeo.com", "Confirm your opt-out", "Click the link below to confirm: https://www.spokeo.com/c", "t"),
                   mail("privacy@spokeo.com", "Done", "Your listing has been removed.", "u")])
        self.assertEqual(self.opened, ["https://www.spokeo.com/c"])
        self.assertEqual(removal.state(self.db, fid), "manual")  # manual → confirmed isn't a move; the alert still says it
        self.assertIn("Spokeo says it's done; the next scan checks it's gone.", [a["text"] for a in self.db.q("SELECT text FROM alerts")])

    def test_a_removal_only_answer_that_needs_a_look(self):
        self.db.x("DELETE FROM requests")
        scanner.record(self.db, self.pid, {"id": "spokeo", "name": "Spokeo"}, "https://www.spokeo.com/s", "listed", [], None)
        fid = self.db.q("SELECT id FROM findings")[0]["id"]
        removal.move(self.db, fid, "ready")
        removal.move(self.db, fid, "requested")
        self.poll([mail("privacy@spokeo.com", "About your request", "We are unable to process your request.", "v")])
        self.assertIn("Spokeo turned the request down — check it: the email is in the removal inbox.",
                      [a["text"] for a in self.db.q("SELECT text FROM alerts")])

    def test_broker_domains(self):
        self.assertEqual(inbox.broker_domains({"request_email": "privacy@x.example"}), {"x.example"})
        self.assertEqual(inbox.broker_domains({"website": "https://www.y.example"}), {"y.example"})
        self.assertTrue(inbox.on("mail.spokeo.com", {"spokeo.com"}))
        self.assertFalse(inbox.on("spokeo.com.evil.example", {"spokeo.com"}))


class RealInboxGaps(Base):
    """The five gaps the first real look at a removal inbox showed (2026-10-29)."""

    def test_a_bounce_fails_the_request_and_tries_the_other_address(self):
        other = dict(SPOKEO, emails=[{"email": "privacy@spokeo.com"}, {"email": "dpo@spokeo.com"}])
        with mock.patch.object(requests, "brokers", return_value=[other, WHITEPAGES]):
            self.poll([mail("Mail Delivery Subsystem <mailer-daemon@googlemail.com>", "Delivery Status Notification (Failure)",
                            f"Address not found. --- Original message --- Subject: Request to delete [GD-{self.rid['spokeo']}]", "b1")])
            r = self.db.q("SELECT * FROM requests WHERE broker='spokeo'")[0]
            self.assertEqual((r["state"], r["to_addr"], r["note"]), ("draft", "dpo@spokeo.com", "Try Spokeo's other address"))
            self.assertIn("privacy@spokeo.com", requests.dead(self.db))
            texts = [a["text"] for a in self.db.q("SELECT text FROM alerts")]
            self.assertTrue(any(t.startswith("The removal email to Spokeo bounced") and "ready on Remove from all" in t for t in texts))
        # no other address: failed, and a later plan never drafts to the dead one again
        self.poll([mail("mailer-daemon@googlemail.com", "Undeliverable",
                        f"Subject: [GD-{self.rid['whitepages']}]", "b2")])
        self.assertEqual(self.db.q("SELECT state, note FROM requests WHERE broker='whitepages'")[0]["state"], "failed")
        self.db.x("DELETE FROM requests WHERE broker='whitepages'")
        requests.plan(self.db, self.pid)
        self.assertEqual(self.db.q("SELECT COUNT(*) n FROM requests WHERE broker='whitepages'")[0]["n"], 0)

    def test_a_bounce_without_a_tag_waits_for_the_owner(self):
        self.poll([mail("postmaster@example.net", "Returned mail", "Something went wrong.", "b3")])
        self.assertEqual(self.db.q("SELECT cls, state FROM mail")[0]["state"], "needs_you")

    def test_help_desk_senders_under_the_brokers_name(self):
        self.assertTrue(inbox.sent_by("spokeo.zendesk.com", {"spokeo.com"}))
        self.assertFalse(inbox.sent_by("evil.zendesk.com", {"spokeo.com"}))
        self.assertFalse(inbox.sent_by("checkr.zendesk.com", {"check.com"}))
        self.assertTrue(inbox.sent_by("check.zendesk.com", {"check.com"}))
        self.assertFalse(inbox.sent_by("spokeo.zendesk.com.evil.example", {"spokeo.com"}))
        self.poll([mail("Spokeo Support <support@spokeo.zendesk.com>", f"Re: Request [GD-{self.rid['spokeo']}]",
                        "Your information has been removed. Confirm here: https://spokeo.zendesk.com/x", "h1")])
        r = self.db.q("SELECT state FROM requests WHERE broker='spokeo'")[0]
        self.assertEqual(r["state"], "confirmed")  # the same treatment as from spokeo.com, never more
        self.assertEqual(self.opened, [])  # and a help desk's link is never opened
        self.poll([mail("Spokeo Support <support@spokeo.zendesk.com>", "Please verify",
                        "Click the link below to confirm your request: https://spokeo.zendesk.com/verify", "h2")])
        self.assertEqual(self.opened, [])

    def test_mail_from_no_broker_is_ignored_but_counted(self):
        self.poll([mail("Google <no-reply@accounts.google.com>", "Security alert", "A new sign-in.", "g1")])
        self.assertEqual(self.db.q("SELECT state, subject FROM mail")[0]["state"], "ignored")
        self.assertEqual(self.db.q("SELECT subject FROM mail")[0]["subject"], "")  # not even the subject is kept
        self.assertEqual(inbox.queue(self.db), [])

    def test_received_replies_are_not_news(self):
        self.poll([mail("support@spokeo.zendesk.com", f"Inquiry Received: Request [GD-{self.rid['spokeo']}] (ID 123)",
                        "This is an automated response. We have received your request.", "a1")])
        r = self.db.q("SELECT state, reply FROM requests WHERE broker='spokeo'")[0]
        self.assertEqual((r["state"], r["reply"]), ("sent", "acknowledged"))
        self.assertEqual(self.db.q("SELECT state FROM mail")[0]["state"], "done")
        self.assertFalse(self.db.q("SELECT text FROM alerts"))
        # still waiting for the real answer: it's matched later, and the follow-up clock still runs
        self.db.x("UPDATE requests SET follow_up=? WHERE broker='spokeo'", (time.time() + 57 * 86400,))
        self.assertEqual([r["broker"] for r in requests.due(self.db, self.pid, now=time.time() + 58 * 86400)
                          if r["broker"] == "spokeo"], ["spokeo"])
        self.poll([mail("support@spokeo.zendesk.com", f"Re: Request [GD-{self.rid['spokeo']}]",
                        "Your information has been removed.", "a2")])
        self.assertEqual(self.db.q("SELECT state FROM requests WHERE broker='spokeo'")[0]["state"], "confirmed")

    def test_one_person_with_a_request_and_a_form_removal(self):
        scanner.record(self.db, self.pid, {"id": "spokeo", "name": "Spokeo"}, "https://www.spokeo.com/s", "listed", [], None)
        fid = self.db.q("SELECT id FROM findings")[0]["id"]
        removal.move(self.db, fid, "ready")
        removal.move(self.db, fid, "requested")
        self.poll([mail("Spokeo <noreply@spokeo.com>", "Spokeo Directory Removal Confirmation #123",
                        "Your listing has been removed.", "o1")])
        self.assertEqual(removal.state(self.db, fid), "confirmed")  # a removal confirmation goes to the form removal
        self.assertEqual(self.db.q("SELECT state FROM requests WHERE broker='spokeo'")[0]["state"], "sent")  # not the email
        self.poll([mail("Spokeo <privacy@spokeo.com>", "About your email", "Your information has been deleted.", "o2")])
        self.assertEqual(self.db.q("SELECT state FROM requests WHERE broker='spokeo'")[0]["state"], "confirmed")
        self.assertEqual(len(self.db.q("SELECT 1 FROM mail WHERE state='done'")), 2)

    def test_two_people_waiting_on_one_broker_still_go_to_the_queue(self):
        mom = self.db.save_person(None, "Mom", "she asked", names=["Mary Doe"])
        scanner.record(self.db, mom, {"id": "spokeo", "name": "Spokeo"}, "https://www.spokeo.com/m", "listed", [], None)
        fid = self.db.q("SELECT id FROM findings WHERE person=?", (mom,))[0]["id"]
        removal.move(self.db, fid, "manual")
        self.poll([mail("Spokeo <noreply@spokeo.com>", "Spokeo Directory Removal Confirmation", "Removed.", "o3")])
        self.assertEqual(self.db.q("SELECT state FROM mail")[0]["state"], "needs_you")

    def test_received_for_a_form_removal_changes_nothing(self):
        self.db.x("DELETE FROM requests")
        scanner.record(self.db, self.pid, {"id": "spokeo", "name": "Spokeo"}, "https://www.spokeo.com/s", "listed", [], None)
        fid = self.db.q("SELECT id FROM findings")[0]["id"]
        removal.move(self.db, fid, "manual")
        self.db.x("DELETE FROM alerts")  # the scan's own alert
        self.poll([mail("Spokeo <noreply@spokeo.com>", "We received your request",
                        "This is an automated response. We have received your request.", "a3")])
        self.assertEqual((self.db.q("SELECT state FROM mail")[0]["state"], removal.state(self.db, fid)), ("done", "manual"))
        self.assertFalse(self.db.q("SELECT text FROM alerts"))


def report_bounce(tag_subject, mid):
    """A bounce shaped like Gmail's: a short readable part first, the original's headers attached at the end."""
    m = EmailMessage()
    m["From"], m["Subject"], m["Message-ID"] = "Mail Delivery Subsystem <mailer-daemon@googlemail.com>", \
        "Delivery Status Notification (Failure)", f"<{mid}@test>"
    m["Date"] = "Thu, 29 Oct 2026 12:00:00 +0000"
    m.set_content("Address not found. Your message wasn't delivered because the address couldn't be found.")
    m.add_attachment(f"From: removals@example.com\nTo: privacy@spokeo.com\nSubject: {tag_subject}\n",
                     subtype="rfc822-headers")
    return m.as_bytes()


class SecondRealLook(Base):
    """What the second real look at the removal inbox showed (2026-10-29, 19:21)."""

    def test_the_tag_in_the_returned_headers_is_found(self):
        self.poll([report_bounce(f"Request to delete my personal information [GD-{self.rid['spokeo']}]", "rb1")])
        self.assertEqual(self.db.q("SELECT state FROM mail")[0]["state"], "done")
        self.assertIn(self.db.q("SELECT state FROM requests WHERE broker='spokeo'")[0]["state"], ("draft", "failed"))
        self.assertIn("privacy@spokeo.com", requests.dead(self.db))

    def test_use_our_form_with_the_form_removal_under_way_is_settled(self):
        scanner.record(self.db, self.pid, {"id": "spokeo", "name": "Spokeo"}, "https://www.spokeo.com/s", "listed", [], None)
        fid = self.db.q("SELECT id FROM findings")[0]["id"]
        removal.move(self.db, fid, "manual")
        removal.move(self.db, fid, "requested")
        self.db.x("DELETE FROM alerts")
        # the shape of Spokeo's help-desk answer: it also explains the form's own confirmation link
        self.poll([mail("Spokeo Customer Care <support@spokeo.zendesk.com>", f"Re: Request to delete [GD-{self.rid['spokeo']}]",
                        "Thank you for contacting Spokeo Customer Care. The fastest and simplest way to make your request is "
                        "to use our easy, self-service tool located at https://www.spokeo.com/optout. Each request requires "
                        "email verification. After submitting, please check your inbox and click the confirmation link to "
                        "complete your request. If we do not receive a reply, we will consider your request resolved.", "u1")])
        self.assertEqual(self.db.q("SELECT cls, state FROM mail")[0]["cls"], "use_form")
        self.assertEqual(self.db.q("SELECT state FROM mail")[0]["state"], "done")
        r = self.db.q("SELECT state, reply FROM requests WHERE broker='spokeo'")[0]
        self.assertEqual((r["state"], r["reply"]), ("sent", "use_form"))
        self.assertFalse(self.db.q("SELECT text FROM alerts"))
        self.assertEqual(self.opened, [])  # nothing clicked
        self.db.x("UPDATE requests SET follow_up=0")
        self.assertEqual([x for x in requests.due(self.db, self.pid) if x["broker"] == "spokeo"], [])  # no follow-up either

    def test_use_our_form_without_a_form_removal_tells_the_owner(self):
        self.poll([mail("support@spokeo.zendesk.com", f"Re: [GD-{self.rid['spokeo']}]",
                        "Please use our opt-out form at https://www.spokeo.com/optout.", "u2")])
        self.assertEqual(self.db.q("SELECT state FROM mail")[0]["state"], "needs_you")
        self.assertIn("Spokeo says to use its own removal form instead of email. If the scan finds a listing there",
                      self.db.q("SELECT text FROM alerts")[0]["text"])

    def test_to_complete_the_removal_click_is_a_confirmation_step(self):
        scanner.record(self.db, self.pid, {"id": "spokeo", "name": "Spokeo"}, "https://www.spokeo.com/s", "listed", [], None)
        fid = self.db.q("SELECT id FROM findings")[0]["id"]
        removal.move(self.db, fid, "manual")
        removal.move(self.db, fid, "requested")
        self.poll([mail("Spokeo <customercare@spokeo.com>", "Spokeo Directory Removal Confirmation 1234567890",
                        "You have requested that the following url be removed from Spokeo searches: "
                        "https://www.spokeo.com/Jane-Doe/p1 To complete the removal process, please click this URL or paste "
                        "it into your browser: https://www.spokeo.com/optout/confirm?token=abc Sincerely, The Spokeo Team",
                        "v1")])
        self.assertEqual(self.db.q("SELECT cls FROM mail")[0]["cls"], "needs_verification")
        self.assertEqual(self.opened, ["https://www.spokeo.com/optout/confirm?token=abc"])  # not the listing before it

    def test_use_our_form_for_a_form_removal_alone(self):
        self.db.x("DELETE FROM requests")
        scanner.record(self.db, self.pid, {"id": "spokeo", "name": "Spokeo"}, "https://www.spokeo.com/s", "listed", [], None)
        fid = self.db.q("SELECT id FROM findings")[0]["id"]
        removal.move(self.db, fid, "manual")
        self.poll([mail("Spokeo <noreply@spokeo.com>", "About your removal", "Please use our opt-out form.", "u3")])
        self.assertEqual(self.db.q("SELECT state FROM mail")[0]["state"], "done")

    def test_an_unclear_answer_leaves_the_request_open(self):
        self.poll([mail("privacy@spokeo.com", f"Re: [GD-{self.rid['spokeo']}]", "Thanks for writing to us.", "k1")])
        self.assertEqual(self.db.q("SELECT reply FROM requests WHERE broker='spokeo'")[0]["reply"], "unknown")
        self.db.x("UPDATE requests SET follow_up=0 WHERE broker='spokeo'")
        self.assertIn("spokeo", [x["broker"] for x in requests.due(self.db, self.pid)])  # the clock still runs
        self.poll([mail("privacy@spokeo.com", f"Re: [GD-{self.rid['spokeo']}]", "Your data has been deleted.", "k2")])
        self.assertEqual(self.db.q("SELECT state FROM requests WHERE broker='spokeo'")[0]["state"], "confirmed")

    def test_two_unclear_answers_keep_one_open_request_and_both_wait(self):
        before = self.db.q("SELECT id, follow_up FROM requests WHERE broker='spokeo'")
        for i in (1, 2):
            self.poll([mail("privacy@spokeo.com", f"Re: [GD-{self.rid['spokeo']}]", f"Note {i} about your email.", f"kk{i}")])
        after = self.db.q("SELECT id, follow_up, state, reply FROM requests WHERE broker='spokeo'")
        self.assertEqual([(r["id"], r["follow_up"]) for r in after], [(r["id"], r["follow_up"]) for r in before])
        self.assertEqual((after[0]["state"], after[0]["reply"]), ("sent", "unknown"))
        self.assertEqual(len([m for m in inbox.queue(self.db) if m["broker"] == "spokeo"]), 2)

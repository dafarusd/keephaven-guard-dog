"""The web sweep: link unwrapping, result relevance, block detection, engine failures — no network.
Fixtures in tests/fixtures/ are real pages saved 2026-10-29 (plain fetch, query "keephaven" nixos):
bing.html = 10 results unrelated to the query, mojeek.html = a captcha page, brave.html = a 429 "suspicious" page.
relevant.html is synthetic (built on Bing's result markup), because no engine returned a relevant page to a plain
request that day."""
import base64
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scrapling.parser import Selector

from guard_dog import scanner, sweep
from guard_dog.db import DB

FIX = Path(__file__).parent / "fixtures"


def page(name):
    return Selector(FIX.joinpath(name).read_text())


class FakePage:
    def __init__(self, status, html):
        self.status = status
        self.sel = Selector(html)

    def css(self, q):
        return self.sel.css(q)

    def get_all_text(self, **k):
        return self.sel.get_all_text()


RELEVANT = """<html><head><title>"Jane Doe" - Search</title></head><body><ol id=b_results>
<li class=b_algo><h2><a href="https://www.bing.com/ck/a?!&&p=1&u=a1{u1}&ntb=1">Jane Doe - Columbus, OH | PeopleSite</a></h2>
<p>Jane Doe, age 52, lives in Columbus. Phone 330-555-0142.</p></li>
<li class=b_algo><h2><a href="https://blog.example/post">A post</a></h2><p>Written by Jane Doe last spring.</p></li>
<li class=b_algo><h2><a href="https://other.example/">Unrelated</a></h2><p>Nothing to see here.</p></li>
</ol><footer><a href="https://www.bing.com/privacy">Privacy</a><a href="https://go.microsoft.com/x">Terms</a></footer>
</body></html>""".replace("{u1}", base64.urlsafe_b64encode(b"https://peoplesite.example/jane-doe").decode().rstrip("="))


class Unwrap(unittest.TestCase):
    def test_real_bing_link(self):
        href = page("bing.html").css("li.b_algo h2 a::attr(href)").get()
        self.assertTrue(sweep.unwrap(href).startswith("https://math.stackexchange.com/"))

    def test_duckduckgo_google_plain_and_junk(self):
        self.assertEqual(sweep.unwrap("//duckduckgo.com/l/?uddg=https%3A%2F%2Fa.example%2Fp&rut=x"), "https://a.example/p")
        self.assertEqual(sweep.unwrap("https://www.google.com/url?q=https://b.example/&sa=U"), "https://b.example/")
        self.assertEqual(sweep.unwrap("https://c.example/x"), "https://c.example/x")
        self.assertEqual(sweep.unwrap("https://www.bing.com/ck/a?u=a1%%%notbase64"), "")
        self.assertEqual(sweep.unwrap("https://www.bing.com/ck/a?u=zz"), "https://www.bing.com/ck/a?u=zz")


class Results(unittest.TestCase):
    def test_unrelated_results_are_dropped_and_counted(self):
        rs, candidates = sweep.results(page("bing.html"), '"keephaven" nixos')
        self.assertEqual(rs, [])
        self.assertGreaterEqual(candidates, 5)

    def test_only_results_that_mention_the_search(self):
        rs, _ = sweep.results(Selector(RELEVANT), '"Jane Doe"')
        self.assertEqual(rs, ["https://peoplesite.example/jane-doe", "https://blog.example/post"])

    def test_phone_compared_by_digits(self):
        rs, _ = sweep.results(Selector(RELEVANT), '"(330) 555-0142"')
        self.assertEqual(rs, ["https://peoplesite.example/jane-doe"])

    def test_block_pages_are_recognised(self):
        for name in ("mojeek.html", "brave.html"):
            self.assertTrue(scanner.blocked_page(200, page(name).get_all_text()), name)

    def test_queries(self):
        qs = sweep.queries({"names": ["Jane Doe", "Cher"], "places": ["Columbus, OH", "OH"], "emails": ["j@x.com"],
                            "phones": ["330 555 0142", "12"], "usernames": ["janedoe77", "abc"]})
        self.assertEqual([q for q, _, _ in qs], ['"Jane Doe"', '"Jane Doe" "Columbus"', '"j@x.com"',
                                                 '"330-555-0142"', '"(330) 555-0142"', '"janedoe77"'])


class Search(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.tmp.name))
        self.person = {"names": ["Jane Doe"], "places": [], "emails": [], "phones": [], "usernames": []}

    def tearDown(self):
        self.tmp.cleanup()

    def go(self, answers, stealth="0"):
        self.db.set("stealth", stealth)
        calls = []

        def fake_get(url, db, st, solve=False):
            eng = next(e["id"] for e in sweep.ENGINES if url.startswith(e["url"].split("{")[0]))
            calls.append((eng, st))
            a = answers.get((eng, st), answers.get(eng))
            if isinstance(a, Exception):
                raise a
            return a
        with mock.patch.object(scanner, "get", fake_get):
            hits, failed = sweep.search(self.db, self.person, lambda s: None, lambda: None, lambda: False, 3)
        return hits, failed, calls

    def test_each_failure_kind_is_reported(self):
        hits, failed, _ = self.go({"bing": FakePage(200, RELEVANT),
                                   "brave": FakePage(429, FIX.joinpath("brave.html").read_text()),
                                   "mojeek": FakePage(200, FIX.joinpath("mojeek.html").read_text())})
        self.assertEqual([h["url"] for h in hits], ["https://peoplesite.example/jane-doe", "https://blog.example/post"])
        self.assertEqual(hits[0]["engine"], "bing")
        self.assertEqual(failed, {"brave": ["http 429"], "mojeek": ["captcha or block page"]})

    def test_unrelated_and_exception(self):
        _, failed, _ = self.go({"bing": FakePage(200, FIX.joinpath("bing.html").read_text()),
                                "brave": ConnectionError("reset"), "mojeek": FakePage(200, RELEVANT)})
        self.assertEqual(failed, {"bing": ["results unrelated to the search"], "brave": ["ConnectionError"]})

    def test_stealth_adds_engines_and_retries_refused_plain_ones(self):
        ok = FakePage(200, RELEVANT)
        hits, failed, calls = self.go({("brave", False): FakePage(429, "Too many requests"), ("brave", True): ok,
                                       "bing": ok, "mojeek": ok, "duckduckgo": ok, "google": ok, "startpage": ok},
                                      stealth="1")
        self.assertIn(("brave", True), calls)
        self.assertEqual({c[0] for c in calls}, {e["id"] for e in sweep.ENGINES})
        self.assertEqual(failed, {})
        self.assertEqual(len(hits), 2)  # same two results from every engine, kept once

    def test_an_engine_that_blocks_or_hangs_isnt_asked_again(self):
        """PiCloud 2026-10-29: Google hung once; each of its remaining searches would have opened another page."""
        self.person["places"] = ["Columbus, OH", "Akron, OH", "Dayton, OH"]  # four searches per engine
        ok = FakePage(200, RELEVANT)
        _, failed, calls = self.go({"bing": FakePage(200, "<title>Unusual traffic</title>Are you a robot?"),
                                    "brave": TimeoutError("the page didn't finish within 180 s"), "mojeek": ok})
        self.assertEqual([c[0] for c in calls].count("bing"), 1)
        self.assertEqual([c[0] for c in calls].count("brave"), 1)
        self.assertEqual([c[0] for c in calls].count("mojeek"), 4)
        self.assertEqual(failed["bing"], ["captcha or block page"] + ["skipped after a block"] * 3)
        self.assertEqual(failed["brave"], ["the page didn't finish within 180 s"] + ["skipped after a block"] * 3)
        self.assertNotIn("mojeek", failed)

    def test_low_memory_stops_an_engine_and_one_odd_error_doesnt(self):
        self.person["places"] = ["Columbus, OH"]
        _, failed, calls = self.go({"bing": TimeoutError("memory ran low (150 MB free)"),
                                    "brave": ConnectionError("reset"), "mojeek": FakePage(200, RELEVANT)})
        self.assertEqual(failed["bing"], ["memory ran low (150 MB free)", "skipped after a block"])
        self.assertEqual(failed["brave"], ["ConnectionError", "ConnectionError"])  # a dropped connection: try the next

    def test_source_for(self):
        b = sweep.source_for({"domain": "spokeo.com"})
        self.assertEqual((b["id"], b["kind"]), ("spokeo", "broker-web"))
        w = sweep.source_for({"domain": "blog.example"})
        self.assertEqual((w["id"], w["kind"], w["optout"]), ("blog.example", "web", sweep.GOOGLE_RAY))


if __name__ == "__main__":
    unittest.main()


class Refusals(Search):
    def test_duckduckgo_202_and_a_playwright_timeout_stop_the_engine(self):
        class TimeoutError(Exception):  # Playwright's own, not the builtin
            pass
        self.person["places"] = ["Columbus, OH", "Akron, OH"]
        ok = FakePage(200, RELEVANT)
        _, failed, calls = self.go({"duckduckgo": FakePage(202, "<html></html>"), "google": TimeoutError("Timeout 90000ms"),
                                    "bing": ok, "brave": ok, "mojeek": ok, "startpage": ok}, stealth="1")
        self.assertEqual([c[0] for c in calls].count("duckduckgo"), 1)
        self.assertEqual([c[0] for c in calls].count("google"), 1)
        self.assertEqual(failed["duckduckgo"], ["http 202", "skipped after a block", "skipped after a block"])
        self.assertEqual(failed["google"], ["TimeoutError", "skipped after a block", "skipped after a block"])

"""Matcher v3 on synthetic pages shaped like the people-search sites seen in a real scan (no real copies used:
the shapes were read with every name, place and number masked)."""
import time
import unittest

from guard_dog.scanner import match

YEAR = time.gmtime().tm_year
P = {"names": ["Jane Q Doe"], "emails": [], "phones": [], "usernames": [],
     "places": ["Columbus, OH", "Dayton, OH"], "relatives": ["John Doe", "Mary Doe"], "birth_year": YEAR - 52}
Q = ("Jane Q Doe", "Columbus, OH")


def m(page, person=P):
    return match(page, person, *Q)


class Shapes(unittest.TestCase):
    def test_card_list_with_age_past_city_and_relatives(self):  # FastPeopleSearch / AnyWho shape
        st, got = m("Jane Doe in other states. Jane Q Doe Age 52 • Columbus, OH Past Addresses: Dayton, OH • "
                    "Akron, OH Relatives: John Doe • Mary Doe")
        self.assertEqual(st, "listed")
        self.assertIn("age 52", got)
        self.assertIn("relative: John Doe", got)

    def test_profile_with_birth_date_and_previous_cities(self):  # MyLife shape
        st, got = m(f"Jane Q Doe is 52 years old and was born on 01/02/{YEAR - 52}. Jane Q Doe lives in Columbus, OH ; "
                    "previous city include Dayton OH. Family members and associates include John Doe.")
        self.assertEqual(st, "listed")

    def test_report_row_with_age_and_lived_in(self):  # PeopleFinders shape
        self.assertEqual(m("Jane Q Doe 1970 View report Age 52 Lived in Dayton, OH | Jane Q Doe Phone & Email")[0], "listed")

    def test_street_address_counts(self):
        st, got = m("Jane Q Doe Current Address 1234 Elm Street Columbus, OH")
        self.assertEqual(st, "listed")
        self.assertIn("street address", got)

    def test_namesakes_across_the_country_are_not_him(self):  # 411 "people found across the US" shape
        page = ("We found 30 people named Jane Doe across the US. Jane Q Doe Miami, FL Age 40s Used to live in Tampa, FL • "
                "Baltimore, MD Related to Bob Smith • Jane Doe Jackson, MS Age 70s Used to live in Biloxi, MS • Newark, NJ "
                "Related to Al Jones")
        self.assertEqual(m(page), ("not found", ["only other people with this name (ages don't fit)"]))
        self.assertEqual(m(page, dict(P, birth_year=None))[0], "unconfirmed")  # without a birth year: can't rule out

    def test_age_that_doesnt_fit_the_birth_year_is_a_namesake(self):
        self.assertEqual(m("Jane Q Doe Age 81 Lives in Columbus, OH Relatives: Tom Doe")[0], "not found")
        unknown_year = dict(P, birth_year=None)
        self.assertEqual(m("Jane Q Doe Age 81 Lives in Columbus, OH Relatives: Tom Doe", unknown_year)[0], "listed")

    def test_birth_year_on_the_page_that_doesnt_fit(self):
        self.assertEqual(m(f"Jane Q Doe was born in {YEAR - 30}. Lives in Columbus, OH. Relatives: Tom Doe")[0], "not found")
        self.assertEqual(m(f"Jane Q Doe was born in {YEAR - 52}. Lives in Columbus, OH")[0], "listed")

    def test_a_ranged_age_is_a_range(self):  # 411 and Spokeo shapes: "Age 50s", "in their 50s", "Age 65+"
        st, got = m("Jane Q Doe Age 50s Lives in Columbus, OH Related to Tom Doe")
        self.assertEqual(st, "listed")  # read as exactly 50, this was "someone else" for a 52-year-old
        self.assertIn("age 50s", got)
        self.assertEqual(m("Jane Q Doe is in their 50s. Lives in Columbus, OH. Relatives: Tom Doe")[0], "listed")
        old = dict(P, birth_year=YEAR - 71)
        self.assertEqual(m("Jane Q Doe Age 65+ Lives in Columbus, OH Relatives: Tom Doe", old)[0], "listed")
        self.assertEqual(m("Jane Q Doe Age 65+ Lives in Columbus, OH Relatives: Tom Doe")[0], "not found")  # 52
        self.assertEqual(m("Jane Q Doe Age 50-59 Lives in Columbus, OH Relatives: Tom Doe")[0], "listed")
        self.assertEqual(m("Jane Q Doe Age 30s Lives in Columbus, OH Relatives: Tom Doe")[0], "not found")
        self.assertEqual(m("Jane Q Doe Age 59-50 Lives in Columbus, OH Relatives: Tom Doe")[0], "listed")  # nonsense: no age

    def test_a_range_edge_allows_a_birthday_not_yet_had(self):
        # born YEAR-60: 60 this year, or still 59 before the birthday; "50s" fits, "60s" fits too
        p = dict(P, birth_year=YEAR - 60)
        self.assertEqual(m("Jane Q Doe Age 50s Lives in Columbus, OH", p)[0], "listed")
        self.assertEqual(m("Jane Q Doe Age 60s Lives in Columbus, OH", p)[0], "listed")
        self.assertEqual(m("Jane Q Doe Age 40s Lives in Columbus, OH", p)[0], "not found")

    def test_a_range_with_an_exact_age_needs_only_one_to_fit(self):
        self.assertEqual(m("Jane Q Doe Age 52 in their 50s Lives in Columbus, OH")[0], "listed")
        self.assertEqual(m("Jane Q Doe Age 81 in their 80s Lives in Columbus, OH")[0], "not found")

    def test_count_with_details_hidden(self):  # AdvancedBackgroundChecks shape
        st, got = m("Jane Q Doe - Public Records Search. We found 1 result for Jane Q Doe. Click a row to view the full profile.")
        self.assertEqual(st, "possible")
        self.assertIn("1 records, details hidden", got)

    def test_aggregate_stats_in_their_city(self):  # Spokeo name page shape
        st, _ = m("We found 3 people named Jane Q Doe in Columbus, in 1 state. Where does Jane Q Doe live?")
        self.assertEqual(st, "possible")

    def test_an_average_age_is_not_their_age(self):  # aggregate sentences on name pages
        st, got = m("We found 4 people named Jane Q Doe in Columbus, average age 47. The median age is 51. "
                    "Typical age 50s.")
        self.assertEqual(st, "possible")
        self.assertFalse(any(g.startswith("age") for g in got))

    def test_shell_page_with_only_the_name(self):  # a results page that loaded no results (JS shell / consent wall)
        self.assertEqual(m("Search results for Jane Q Doe. View Details. This site can't be used for employment.")[0],
                         "unconfirmed")

    def test_terms_gate_is_unconfirmed_not_namesakes(self):  # 411 shape
        page = ("To continue, please agree to our Terms of Service. 30 people found for Jane Doe across the US. "
                "Jane Q Doe Miami, FL Age 40s Used to live in Tampa, FL | Jane Doe Jackson, MS Age 70s")
        self.assertEqual(m(page, dict(P, birth_year=None)),
                         ("unconfirmed", ["name: Jane Q Doe", "the details are behind the site's terms page"]))

    def test_city_in_a_neighbours_entry_doesnt_count(self):  # court archive list shape
        page = ("Indictments: Smith, John, 45, Columbus; theft. Doe, Jane Q, 52, address unknown; breaking and entering. "
                "Brown, Al, 30, Dayton; trespass.")
        st, got = m(page, dict(P, birth_year=None))
        self.assertEqual(st, "unconfirmed")
        self.assertFalse(any(g.startswith("city:") for g in got))
        mine = "Indictments: Doe, Jane Q, 52, Columbus; breaking and entering. Brown, Al, 30, Dayton; trespass."
        self.assertEqual(m(mine, dict(P, birth_year=None))[0], "possible")  # her own entry carries her city

    def test_profile_sentences_stay_together(self):
        st, got = m("Jane Q Doe is 52 years old. Jane Q Doe lives in Columbus, OH. Family members include John Doe.")
        self.assertEqual(st, "listed")
        self.assertIn("relative: John Doe", got)

    def test_the_strongest_of_their_entries_decides(self):
        page = "Jane Q Doe Age 52 Lives in Columbus, OH. Other listings follow. Jane Q Doe of Dayton"
        st, got = m(page)
        self.assertEqual(st, "listed")
        self.assertIn("age 52", got)

    def test_a_card_spread_over_lines_stays_one_entry(self):  # how page text comes out of a real card
        page = "Jane Q Doe\nAge 52\nLives in:\nColumbus, OH\nUsed to live in:\nDayton, OH\nRelated to:\nJohn Doe"
        self.assertEqual(m(page)[0], "listed")

    def test_relative_alone_anchors_a_block(self):
        self.assertEqual(m("Jane Q Doe, 52, Springfield IL. Related to Mary Doe")[0], "listed")


if __name__ == "__main__":
    unittest.main()


class NoRecordsAfterTheName(unittest.TestCase):
    def test_saved_familytreenow_pages_with_no_records(self):
        """VM 2026-10-30: a made-up person came back "possible", then "listed", from the title and search box
        repeating the name and city and the page's own "Living People" and "Connect with relatives"."""
        from pathlib import Path

        from scrapling.parser import Selector
        person = {"names": ["Zqxv Testerson"], "emails": [], "phones": [], "usernames": [], "places": ["Columbus, OH"],
                  "birth_year": 1971}
        for name in ("familytreenow-no-records.html", "familytreenow-no-records-2.html"):
            html = Path(__file__).with_name("fixtures").joinpath(name).read_text()
            text = Selector(html).get_all_text(ignore_tags=("script", "style", "noscript"))
            self.assertEqual(match(text, person, "Zqxv Testerson", "Columbus, OH"), ("not found", []), name)

    def test_a_result_titled_name_in_city_with_details_still_counts(self):
        st, _ = m("Jane Q Doe in Columbus, OH. Jane Q Doe, Age 52. Lives in Columbus, OH. Relatives: John Doe")
        self.assertEqual(st, "listed")

    def test_search_echo_then_no_records(self):
        self.assertEqual(m("Search Criteria Jane Q Doe in Columbus, OH Edit Search 0 Records for Jane Q Doe No records found :( "
                           "Connect with relatives and explore your family tree.")[0], "not found")

    def test_no_results_elsewhere_on_a_real_listing_page_doesnt_hide_it(self):
        st, _ = m("Jane Q Doe Age 52 • Columbus, OH Past Addresses: Dayton, OH Relatives: John Doe • Mary Doe. "
                  + "Similar names " * 20 + "Jane Doe in Toledo: no results found")
        self.assertEqual(st, "listed")

    def test_no_results_further_down_still_counts_when_nothing_else_does(self):
        self.assertEqual(m("Results for Jane Q Doe " + "Refine your search by state or age. " * 6 + "No records found.")[0],
                         "not found")

    def test_living_in_a_menu_isnt_where_they_live(self):
        from guard_dog.scanner import block_signals
        _, signals, _ = block_signals("jane q doe filter results 0 living people 0 death records", "", P)
        self.assertNotIn("lives / lived in", signals)
        for words in ("lives in columbus", "living in dayton", "resides in akron", "used to live in toledo",
                      "current address", "past addresses", "previous cities"):
            self.assertIn("lives / lived in", block_signals(f"jane q doe {words}", "", P)[1], words)

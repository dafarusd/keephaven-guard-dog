import unittest

from guard_dog.scanner import blocked_page, match

P = {"names": ["Jane Q Doe", "Jane Doe", "Jane Roe"], "emails": ["jane@example.com"], "phones": ["(330) 555-0142"],
     "places": ["Columbus, OH", "Dayton, OH"], "usernames": ["janedoe77", "mike"]}
Q = ("Jane Q Doe", "Columbus, OH")


def m(page):
    return match(page, P, *Q)[0]


class FalsePositives(unittest.TestCase):
    def test_echoed_query(self):
        self.assertEqual(m("Results for Jane Q Doe in Columbus, OH. Search again."), "unconfirmed")

    def test_variant_of_searched_name_is_not_an_alias(self):
        self.assertNotEqual(m("Jane Doe in Columbus, OH"), "listed")

    def test_place_in_footer_is_not_a_listing(self):  # noqa: the footer city is outside the name's block
        self.assertNotEqual(m("Jane Q Doe in Columbus, OH " + "x " * 400 + "Nearby cities: Dayton"), "listed")

    def test_their_other_city_with_an_age_is_possible(self):
        self.assertEqual(m("Jane Doe, 61, Dayton OH"), "possible")

    def test_short_username_ignored(self):
        self.assertNotEqual(m("Jane Q Doe. Ask mike about it."), "listed")

    def test_other_person(self):
        self.assertEqual(m("John Smith, Columbus OH, 330-555-9999"), "not found")


class FalseNegatives(unittest.TestCase):
    def test_phone_beats_no_results_phrase(self):
        self.assertEqual(m("Jane Doe, Columbus OH. Phone 330-555-0142. There are no photos. We couldn't find a work address."), "listed")

    def test_full_middle_name_and_last_first(self):
        self.assertEqual(m("Jane Quinn Doe — phone (330) 555-0142"), "listed")
        self.assertEqual(m("DOE, JANE  330.555.0142"), "listed")

    def test_alias_counts(self):
        self.assertEqual(m("Jane Q Doe, also known as Jane Roe"), "listed")

    def test_long_username_near_name(self):
        self.assertEqual(m("Jane Doe (janedoe77) posted"), "listed")

    def test_no_results_page(self):
        self.assertEqual(m("Jane Q Doe in Columbus, OH: No results found."), "not found")


class Blocks(unittest.TestCase):
    def test_block_page_on_200(self):
        self.assertTrue(blocked_page(200, "Access Denied. Reference #18.abc"))
        self.assertTrue(blocked_page(200, "Press & Hold to confirm you are a human"))
        self.assertFalse(blocked_page(200, "Jane Doe lives in Columbus. " * 300 + "protected by reCAPTCHA"))


if __name__ == "__main__":
    unittest.main()

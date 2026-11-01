"""Spokeo: pick their profile on the search page the scan saved, then paste its address into spokeo.com/optout with the
removal email. Spokeo emails a confirmation link. If the form shows a picture captcha, the removal goes manual."""
import re
from urllib.parse import urljoin

from ..removal import NeedsHand, OptOut, decline_consent, pick_record

PROFILE = re.compile(r"^(?:https://www\.spokeo\.com)?/[^/]+/[^/]+/[^/]+/p\d+$")


class Spokeo(OptOut):
    by_hand = ("Open Spokeo's removal page.", "Paste your Spokeo profile address and your removal email (below).",
               "Tick 'I'm not a robot' and press the button.", "Open the email Spokeo sends and click its link.")
    site = "spokeo"
    start_url = ""  # their search page from the scan (the finding's address)
    optout_url = "https://www.spokeo.com/optout"
    fields = ("removal_email",)
    reads = ("your Spokeo profile address",)  # read from Spokeo's own search page, not from the person's details
    confirm = "email"
    domains = ("spokeo.com",)

    def fill(self, page, data, pick, ev):
        cards = []
        for a in page.query_selector_all("a[href]"):
            href = a.get_attribute("href") or ""
            if PROFILE.match(href):
                box = a.evaluate_handle("e => e.closest('div') && e.closest('div').parentElement || e.parentElement")
                cards.append((urljoin(self.optout_url, href), box.inner_text()))
        url = pick_record(cards, pick)
        ev.prefill.update({"Your Spokeo profile address": url, "Your removal email": data["removal_email"]})
        ev.step(f"picked their profile out of {len(cards)} on the page", page)
        page.goto(self.optout_url)
        page.wait_for_load_state("networkidle")
        decline_consent(page, ev)
        form = page.locator("form:has(input[name=email])")
        # On the live site the address field came back empty: the page's scripts take over the form after it loads
        # and reset what was typed. So type key by key like a person (email first, Tab out so its checks run), wait,
        # check both values stuck, and type again if they were wiped: three tries, each logged.
        values = (("email", data["removal_email"]), ("url", url))
        for attempt in range(1, 4):
            for name, value in values:
                box = form.locator(f"input[name={name}]")
                box.click()
                box.fill("")
                box.press_sequentially(value, delay=15)
                box.press("Tab")
            page.wait_for_timeout(1500)
            wiped = [name for name, value in values if form.locator(f"input[name={name}]").input_value() != value]
            ev.step(f"attempt {attempt}: " + (f"wiped {', '.join(wiped)}" if wiped else "kept"))
            if not wiped:
                break
        else:
            raise RuntimeError(f"Spokeo's form didn't keep the {wiped[0]} field after 3 tries")
        if page.locator("iframe[title*='reCAPTCHA'][src*='anchor']").count():
            raise NeedsHand("Spokeo asks you to tick 'I'm not a robot'")

    def press(self, page, ev):
        page.locator("form:has(input[name=email]) button[type=submit]").click()
        page.wait_for_selector("text=/check your email|confirmation|sent/i", timeout=30000)


SCRIPT = Spokeo()

# Keephaven Guard Dog

Finds where you show up online, helps you get removed, and keeps watching. It runs on your own computer. The
people you add and what it finds stay on that computer; only the searches go out.

## Get it

Download the file for your computer from the Releases page:

- `keephaven-guard-dog_0.1.0_amd64.deb` for a normal 64-bit PC (133 MB)
- `keephaven-guard-dog_0.1.0_arm64.deb` for a Raspberry Pi 5 with 8 GB running 64-bit Raspberry Pi OS (119 MB) — a Pi 4 hasn't been tried

On Ubuntu, right-click the file, choose Open With…, pick App Center and press Open. (Double-clicking it opens it
as an archive instead.) On Debian, double-click it and Software opens. On Raspberry Pi OS, right-click the file
and choose Package Install (double-clicking it opens it as an archive there too).

Press Install. App Center warns that it's from a third party — it is, it's not from Ubuntu. Your computer asks for
your password once, because it's installing a program.

Tested on Ubuntu 24.04 and Debian 13 desktops, and on a Raspberry Pi 5 with 8 GB running Raspberry Pi OS.

It takes about 630 MB once installed (560 MB on the Pi version), plus about 420 MB if you turn on stealth mode.

## First time

Open **Keephaven Guard Dog** from your apps menu. It opens in your web browser.

If nothing seems to happen, look at your web browser — it opened Guard Dog as a new tab.

It asks you to pick a password first. Anyone who uses this computer could open the page, so the password keeps
it yours. Then add a person, yourself or someone who asked you to (a parent, say), and press Scan now.

Forgot the password? Open **Reset Guard Dog password** from the apps menu. It asks for your computer's password,
then the page lets you pick a new one. The people you added and their results stay.

## What it does

- **Scan.** Searches 17 people-search sites for each name and place, and breach lists for each email. A site
  only counts as listing someone if it shows a detail Guard Dog didn't search with (a phone, an email, another
  city, another name), so a site that just repeats your search back isn't counted. With a birth year set, a
  listing whose age doesn't fit is someone else with the same name.
- Sweep the web. Asks search engines about every name (alone and with each city), email, phone number and
  username, then opens every page they return and checks it. Up to 200 pages a scan by default (Settings, up to
  2,000). If an engine blocks the searches, you're told.
- **Remove you from all.** One page writes a removal email to every site that lists you (and, if you choose, to
  all 415 data brokers on the list), shows you each one, and sends them from your removal inbox when you press
  Send — up to 250 a day, the rest on the next days. Each email asks the broker to delete your information and
  stop selling it, under California's law if you live there, the GDPR if you live in Europe or the UK, and in
  plain terms otherwise. It carries your name, your listing on that site and the city it shows. Never your
  birth date, phone, street address or relatives. With no answer after 60 days (45 under California's law, 30
  under the GDPR), the site's card offers a follow-up.
- Reads the answers. While removals are waiting, it reads the removal inbox every 15 minutes (read only — it
  never marks, moves or deletes mail) and sorts each broker's reply: done, asked to confirm, no record, turned
  down, or unclear. It clicks a confirmation link only when the sender and the link are both that broker's own.
- **Remove by hand.** For every listing: the site's opt-out page, the steps, and a saved copy of what it showed.
  Mark it requested and the next scan checks it's gone.
- Old profile copies. Once a week, asks the Internet Archive whether it kept copies of profiles with each
  username on 15 sites (Twitter/X, Instagram, Facebook, Reddit, GitHub and others), and says how to ask the
  Archive to remove them.
- **Watch.** Scans again every 6 hours by default (Settings, 1 to 720 hours, or 0 for only when you press Scan)
  and tells you about new listings, listings that come back, and new breaches. A listing only counts as gone
  after two scans in a row don't show it.
- Check a password. Only the first 5 characters of its SHA-1 fingerprint leave your computer.

Looking for accounts by username isn't in this version yet. It comes in a later update.

## Stealth mode

A switch in Settings, off by default. Off, Guard Dog makes plain requests and skips the 10 sites that block bots.
On, it uses a hidden browser that gets through Cloudflare checks, so those sites get checked too, and the web
sweep adds DuckDuckGo, Google and Startpage to Brave, Bing and Mojeek.

The first time, Settings offers to download that browser: 200 MB from Google's Chrome for Testing
(Chromium 156.0.8078.4). Guard Dog checks the download against the fingerprint it was built with and refuses one
that doesn't match.

Some sites' terms forbid automated searches. Turning stealth on is your call.

## From your phone or another computer

Settings has a switch: **Open this dashboard from your phone and other computers.** Turn it on and Settings shows
the address to type, like `http://yourcomputer.local:8642` and one with numbers. The `.local` one may not work on
every phone — the number one always does. Turn it off and it's back to this computer only. No restart either way.

Anyone on the same network as this computer — at home, or wherever it's connected — can reach the sign-in page
while it's on. Your password and results cross that network unencrypted (plain http).

## What it can and can't check

Of the 17 people-search sites, 7 have a search page Guard Dog has confirmed, so "not listed" there means not
listed. On the other 10, a page with no listing proves nothing yet, so those say "can't be checked in this
version" instead of all clear. Two of them, Whitepages and PeopleSearchNow, blocked the check in every test run,
even with stealth on.

## Speed and memory

- Debian 13 desktop, 8 GB, 4 cores, stealth on, a person with no listings: a scan took 3.2 minutes. Free memory
  went from 4.8 GB to 3.1 GB at the lowest.
- Raspberry Pi 5, 8 GB, Raspberry Pi OS desktop, stealth on, a person with no listings: a scan took 2.6 minutes.
  Free memory went from 7.4 GB to 6.5 GB at the lowest.

A scan uses up to 1.7 GB of memory while the hidden browser loads a page, so Guard Dog runs fewer browsers when memory is short.

## What leaves your computer

- Each email goes to the breach service (XposedOrNot, or Have I Been Pwned with your own key).
- Each name and place goes to each people-search site, from your own connection (or your proxy). Emails are never
  typed into a people-search site.
- With the web sweep on (the default), every name, email, phone number and username goes to Brave, Bing and Mojeek
  as a quoted search, and to DuckDuckGo, Google and Startpage too when stealth is on. Turn the sweep off in
  Settings if you'd rather they never see your email or phone.
- Once a week, each username (or the part of each email before the @) goes to web.archive.org. Turn it off in
  Settings.

## Removing it

Open **Remove Keephaven Guard Dog** from the apps menu (on Raspberry Pi OS it's under Preferences). It asks for
your computer's password, then removes it. The people you added and their results stay on the computer, in a
folder only an admin can open, and installing it again picks them up.

## Credits

Built on [Scrapling](https://github.com/D4Vinci/Scrapling) by Karim Shoair (BSD-3-Clause).

Some people-search sites are one company under several names (`guard_dog/data/families.json`, each with the page
that shows it): BeenVerified, PeopleLooker and NeighborWho are The Lifetime Value Co.; 411.com names Whitepages,
Inc.; AnyWho's removal page is Spokeo's; and TruePeopleSearch and six other sites appear to be one operator (they
share the same terms of service, though no page names the company). When one of them lists you and a sister site
couldn't be checked, Guard Dog shows it as likely listing you. The idea of grouping sites this way comes from
Scrubbr; the groups here were read on the sites' own pages.

The removal emails follow the structure of [Scrubbr](https://github.com/jamessk22/Scrubbr)'s letters (MIT): a
deletion and opt-out request per broker with a reference tag in the subject, and a California version citing the
statute. The wording is our own. Reading an age range as a range, not an age, is Scrubbr's rule too.

The Internet Archive check comes from [incognito](https://github.com/Salicath/incognito)'s Wayback scanner (MIT):
its 15 profile-address shapes, the archive query and the pacing between requests.

The list of data brokers (`guard_dog/data/brokers.json`) was built by visiting each broker's own website and
recording the opt-out pages and privacy email addresses printed there, with the date. Which brokers to visit came
from [Eraser](https://github.com/drumandbytes/eraser)'s broker list, used as leads only; nothing from it is copied
in.

## License

AGPL-3.0 — see `LICENSE`. Copyright (c) 2026 Keephaven LLC. Commercial licenses for Guard Dog's own code are available from Keephaven LLC; see `NOTICE`.

Keephaven™ is a trademark of Keephaven LLC. The license covers the code, not the name.

The projects it's built on keep their own licenses: Scrapling BSD-3-Clause, Scrubbr, incognito and Eraser MIT, Python PSF. Each one's full text is in `THIRD-PARTY.md`.

Built by Dafarus. Follow the work on X: [@Dafarusd](https://x.com/Dafarusd)

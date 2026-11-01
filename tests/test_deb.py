"""The .deb's own files: the admin-only password reset script and what the package declares."""
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

DEB = Path(__file__).resolve().parent.parent / "packaging" / "deb"


@unittest.skipUnless(DEB.is_dir(), "checks the source tree's packaging files")
class ResetScript(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "calls.log"
        for tool in ("chown", "systemctl"):
            (self.bin / tool).write_text(f'#!/bin/sh\necho "{tool} $*" >> "{self.log}"\n')
            (self.bin / tool).chmod(0o755)
        self.data = self.root / "data"
        src = (DEB / "reset-password").read_text()
        # the same script, pointed at stand-ins: it fixes its own PATH and data folder, so a copy is the only way in
        self.script = self.root / "reset-password"
        self.script.write_text(src.replace("PATH=/usr/sbin:/usr/bin:/sbin:/bin", f"PATH={self.bin}:/usr/bin:/bin")
                                  .replace("DATA=/var/lib/keephaven-guard-dog", f"DATA={self.data}"))

    def tearDown(self):
        self.tmp.cleanup()

    def run_it(self, *args):
        return subprocess.run(["/bin/sh", str(self.script), *args], capture_output=True, text=True,
                              env={"PATH": "/nowhere", "IFS": "x"})

    def test_writes_the_marker_owned_by_the_service_and_restarts_it(self):
        self.data.mkdir()
        r = self.run_it("--anything", "; rm -rf /")  # arguments are ignored
        self.assertEqual(r.returncode, 0, r.stderr)
        m = self.data / "reset-password"
        self.assertEqual(stat.S_IMODE(m.stat().st_mode), 0o600)
        self.assertEqual(self.log.read_text().splitlines(),
                         [f"chown keephaven-guard-dog:keephaven-guard-dog {m}", "systemctl restart keephaven-guard-dog.service"])
        self.assertIn("set a new password", r.stdout)

    def test_no_data_folder_nothing_to_reset(self):
        r = self.run_it()
        self.assertEqual(r.returncode, 1)
        self.assertFalse(self.log.exists())

    def test_the_real_script_trusts_nothing_from_its_environment(self):
        src = (DEB / "reset-password").read_text()
        for line in ("set -eu", "PATH=/usr/sbin:/usr/bin:/sbin:/bin", "umask 077", "DATA=/var/lib/keephaven-guard-dog"):
            self.assertIn(line, src)
        self.assertNotIn("$1", src)
        self.assertNotIn("$@", src)
        self.assertTrue(os.access(DEB / "reset-password", os.X_OK))


@unittest.skipUnless(DEB.is_dir(), "checks the source tree's packaging files")
class Declared(unittest.TestCase):
    def test_service_asks_for_a_password_and_menu_entry_needs_an_admin(self):
        self.assertIn("--require-password", (DEB / "keephaven-guard-dog.service").read_text())
        self.assertIn("--network-switch", (DEB / "keephaven-guard-dog.service").read_text())
        entry = (DEB / "keephaven-guard-dog-reset-password.desktop").read_text()
        self.assertIn("Exec=pkexec /opt/keephaven-guard-dog/bin/reset-password", entry)
        policy = (DEB / "co.keephaven.guard-dog.reset-password.policy").read_text()
        self.assertEqual(policy.count("<allow_active>auth_admin</allow_active>"), 1)
        self.assertIn(">/opt/keephaven-guard-dog/bin/reset-password</annotate>", policy)
        self.assertNotIn(">yes<", policy)  # never allowed without the admin password


BUILD_DEB = Path(__file__).resolve().parent.parent / "tools" / "build-deb.sh"


@unittest.skipUnless(BUILD_DEB.is_file(), "checks the source tree's build script")
class BuildNeedsItsCommit(unittest.TestCase):
    """A package has to say what it was built from: the build stops before it touches anything otherwise."""

    def run_build(self, *args):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "out"
            p = subprocess.run(["bash", str(BUILD_DEB), "amd64", d, d, str(out), *args],
                               capture_output=True, text=True, timeout=60)
            return p, out.exists()

    def test_no_commit_or_not_a_commit_stops_first(self):
        for args in ((), ("nothex",), ("abc12",), ("0123456789abcdef0123456789abcdef012345678",)):
            p, made = self.run_build(*args)
            self.assertEqual(p.returncode, 1, args)
            self.assertIn("STOP: give the source commit (7 to 40 hex characters) as the 5th argument", p.stdout, args)
            self.assertFalse(made, args)  # OUT is the first thing the script would create

    def test_the_commit_goes_into_build(self):
        text = BUILD_DEB.read_text()
        self.assertIn('"$COMMIT" "$ARCH" > "$APP/BUILD"', text)
        self.assertNotIn(".commit", text)
        self.assertNotIn("unknown", text)


ROOT = Path(__file__).resolve().parent.parent
THIRD_PARTY = ROOT / "tools" / "third_party.py"
AGPL_SHA = "4df3c306dddaaf4baffdff5ca820cc679ac8cd6dc263c6a74517783e42fa7a3b"  # ~/projects/keephaven/LICENSE, the family's


@unittest.skipUnless((ROOT / "LICENSE").is_file() and DEB.is_dir(), "checks the source tree's license files")
class Licensed(unittest.TestCase):
    """AGPL-3.0-only, Keephaven LLC, the same as the rest of Keephaven; everything it carries or learned from named."""

    def test_the_family_license_text_and_holder(self):
        import hashlib
        self.assertEqual(hashlib.sha256((ROOT / "LICENSE").read_bytes()).hexdigest(), AGPL_SHA)
        self.assertIn("Copyright (c) 2026 Keephaven LLC.", (ROOT / "NOTICE").read_text())
        self.assertIn('license = "AGPL-3.0-only"', (ROOT / "pyproject.toml").read_text())

    def test_the_package_says_so_with_the_full_text(self):
        text = (DEB / "copyright").read_text()
        self.assertNotIn("not-chosen", text)
        self.assertNotIn("dafarusd", text)
        self.assertIn("Files: *\nCopyright: 2026 Keephaven LLC\nLicense: AGPL-3.0-only\n", text)
        body = text.split("\nLicense: AGPL-3.0-only\n")[-1]
        lines = [line[1:] for line in body.splitlines()]
        self.assertEqual([("" if line == "." else line) for line in lines],
                         [("" if not line.strip() else line) for line in (ROOT / "LICENSE").read_text().splitlines()])

    def test_the_projects_it_learned_from_with_their_texts(self):
        text = (ROOT / "THIRD-PARTY.md").read_text()
        for line in ("Copyright (c) 2024, Karim shoair", "Copyright (c) 2026 jamessk22",
                     "Copyright (c) 2026 Incognito Contributors", "Copyright (c) 2026 Drumandbytes OÜ (fork maintainer)",
                     "Chrome for Testing 156.0.8078.4"):
            self.assertIn(line, text)
        self.assertEqual(text.count("Redistribution and use in source and binary forms"), 1)  # BSD-3, Scrapling
        self.assertEqual(text.count("Permission is hereby granted, free of charge"), 3)  # MIT, the other three

    def test_the_libraries_python_links_in(self):
        import hashlib
        names = sorted(p.name for p in (DEB / "python-licenses").iterdir())
        self.assertEqual(len(names), 19)
        for n in ("LICENSE.cpython.txt", "LICENSE.openssl-3.txt", "LICENSE.sqlite.txt", "LICENSE.libffi.txt"):
            self.assertIn(n, names)
        # each text exactly as the release's full archives carry it (python.pin says which), so none can drift
        pinned = dict(reversed(line.split("  ", 1)) for line in (DEB / "python-licenses.sha256").read_text().splitlines())
        self.assertEqual(sorted(pinned), names)
        for n in names:
            self.assertEqual(hashlib.sha256((DEB / "python-licenses" / n).read_bytes()).hexdigest(), pinned[n], n)

    def test_the_build_ships_them(self):
        text = BUILD_DEB.read_text()
        self.assertIn('packaging/deb/python-licenses/*.txt "$APP/python/licenses/"', text)
        self.assertIn('"$SRC/NOTICE" "$SRC/THIRD-PARTY.md" "$PKG/usr/share/doc/keephaven-guard-dog/"', text)
        self.assertIn('"$SRC/tools/third_party.py" "$APP/venv/lib/python3.12/site-packages"', text)


@unittest.skipUnless(THIRD_PARTY.is_file(), "checks the source tree's package-list tool")
class PackageList(unittest.TestCase):
    """The shipped THIRD-PARTY.md names every package in the build, or the build stops."""

    def site(self, d, **packages):
        for folder, meta in packages.items():
            (Path(d) / f"{folder}.dist-info").mkdir()
            if meta is not None:
                (Path(d) / f"{folder}.dist-info" / "METADATA").write_text(meta)

    def run_it(self, d):
        return subprocess.run([sys.executable, "-I", str(THIRD_PARTY), d], capture_output=True, text=True, timeout=60)

    def test_every_package_with_its_license(self):
        with tempfile.TemporaryDirectory() as d:
            self.site(d, scrapling_0_5="Metadata-Version: 2.1\nName: scrapling\nVersion: 0.5\n"
                                       "Classifier: License :: OSI Approved :: BSD License\n",
                      orjson_3="Metadata-Version: 2.4\nName: orjson\nVersion: 3.13.0\n"
                               "License-Expression: MPL-2.0 AND (Apache-2.0 OR MIT)\n",
                      odd_1="Metadata-Version: 2.1\nName: odd\nVersion: 1\nLicense: line one\n line two\n",
                      keephaven_guard_dog_0_1_0="Metadata-Version: 2.4\nName: keephaven-guard-dog\nVersion: 0.1.0\n")
            p = self.run_it(d)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("3 packages in", p.stdout)
        self.assertIn("| scrapling | 0.5 | BSD License |", p.stdout)
        self.assertIn("| orjson | 3.13.0 | MPL-2.0 AND (Apache-2.0 OR MIT) |", p.stdout)
        self.assertIn("| odd | 1 | see its .dist-info folder |", p.stdout)
        self.assertNotIn("keephaven-guard-dog |", p.stdout)  # Guard Dog's own license is the top of the file

    def test_a_package_it_cant_read_stops_the_build(self):
        with tempfile.TemporaryDirectory() as d:
            self.site(d, good_1="Metadata-Version: 2.1\nName: good\nVersion: 1\nLicense: MIT\n", broken_2=None)
            p = self.run_it(d)
        self.assertEqual(p.returncode, 1)
        self.assertIn("STOP: can't read broken_2.dist-info", p.stderr)
        self.assertEqual(p.stdout, "")

    def test_an_empty_environment_stops_the_build(self):
        with tempfile.TemporaryDirectory() as d:
            p = self.run_it(d)
        self.assertEqual(p.returncode, 1)


METAINFO = DEB / "co.keephaven.GuardDog.metainfo.xml"


@unittest.skipUnless(DEB.is_dir(), "checks the source tree's packaging files")
class AppStream(unittest.TestCase):
    """Software (Debian) and App Center (Ubuntu) list, find and remove only apps that have AppStream data."""

    def test_one_version_everywhere(self):
        """The .deb takes its version from __init__.py; Software shows the newest <release>: a half bump ships a
        0.1.1 package that says 0.1.0."""
        import re
        import tomllib
        import xml.etree.ElementTree as ET
        root = DEB.parent.parent
        code = re.search(r'__version__ = "(.*)"', (root / "guard_dog" / "__init__.py").read_text()).group(1)
        project = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
        newest = ET.parse(METAINFO).getroot().find("releases/release").get("version")
        self.assertEqual((project, newest), (code, code))
        self.assertIn(f"keephaven-guard-dog_{code}_amd64.deb", (root / "README.md").read_text())
        self.assertIn(f"keephaven-guard-dog_{code}_arm64.deb", (root / "README.md").read_text())

    def test_names_the_app_the_menu_entry_launches(self):
        import xml.etree.ElementTree as ET
        root = ET.parse(METAINFO).getroot()
        self.assertEqual(root.get("type"), "desktop-application")
        self.assertEqual(root.findtext("id"), METAINFO.name.removesuffix(".metainfo.xml"))
        launch = root.find("launchable")
        self.assertEqual((launch.get("type"), launch.text), ("desktop-id", "keephaven-guard-dog.desktop"))
        self.assertTrue((DEB / launch.text).is_file())
        # without it Software lists the app but its Uninstall does nothing: nothing ties the entry to the package
        package = [l.split(": ", 1)[1] for l in (DEB / "control.in").read_text().splitlines() if l.startswith("Package: ")]
        self.assertEqual(root.findtext("pkgname"), package[0])
        self.assertEqual(root.findtext("project_license"), "AGPL-3.0-only")
        self.assertEqual(root.findtext("metadata_license"), "CC0-1.0")
        self.assertEqual(root.findtext("developer/name"), "Keephaven LLC")
        comment = [l.split("=", 1)[1] for l in (DEB / launch.text).read_text().splitlines() if l.startswith("Comment=")]
        self.assertEqual(root.findtext("summary"), comment[0])
        self.assertNotIn("@", METAINFO.read_text())

    def test_its_description_is_the_approved_readme_wording(self):
        import xml.etree.ElementTree as ET
        readme = " ".join((ROOT / "README.md").read_text().split())
        for el in ET.parse(METAINFO).getroot().find("description").iter():
            if el.tag in ("p", "li"):
                self.assertIn(" ".join(el.text.split()), readme)

    def test_the_build_ships_and_validates_it(self):
        text = BUILD_DEB.read_text()
        self.assertIn('"$SRC/packaging/deb/co.keephaven.GuardDog.metainfo.xml" '
                      '"$PKG/usr/share/metainfo/co.keephaven.GuardDog.metainfo.xml"', text)
        self.assertIn('appstreamcli validate --no-net --pedantic "$PKG/usr/share/metainfo/co.keephaven.GuardDog.metainfo.xml"',
                      text)


@unittest.skipUnless(DEB.is_dir(), "checks the source tree's packaging files")
class RemoveRoot(unittest.TestCase):
    """bin/remove-root, run by pkexec: removes the package (data kept), says 3 when another program holds the lock."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "calls.log"
        src = (DEB / "remove-root").read_text()
        self.script = self.root / "remove-root"
        self.script.write_text(src.replace("PATH=/usr/sbin:/usr/bin:/sbin:/bin", f"PATH={self.bin}:/usr/bin:/bin"))

    def tearDown(self):
        self.tmp.cleanup()

    def apt(self, output, rc):
        (self.bin / "apt-get").write_text(f'#!/bin/sh\necho "apt-get $* FRONTEND=$DEBIAN_FRONTEND" >> "{self.log}"\n'
                                          f"printf '%s\\n' '{output}'\nexit {rc}\n")
        (self.bin / "apt-get").chmod(0o755)

    def run_it(self, *args):
        return subprocess.run(["/bin/sh", str(self.script), *args], capture_output=True, text=True,
                              env={"PATH": "/nowhere", "IFS": "x"})

    def test_removes_only_this_package_without_waiting_for_a_lock(self):
        self.apt("Removing keephaven-guard-dog (0.1.0) ...", 0)
        r = self.run_it("--purge", "; rm -rf /")  # arguments are ignored
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "removed")
        self.assertEqual(self.log.read_text().splitlines(),
                         ["apt-get -o DPkg::Lock::Timeout=0 remove -y keephaven-guard-dog FRONTEND=noninteractive"])

    def test_another_program_holding_the_lock_is_3(self):
        for message in ("E: Could not get lock /var/lib/dpkg/lock-frontend. It is held by process 4242 (packagekitd)",
                        "E: Unable to acquire the dpkg frontend lock (/var/lib/dpkg/lock-frontend), is another process using it?"):
            self.apt(message, 100)
            r = self.run_it()
            self.assertEqual(r.returncode, 3, message)
            self.assertEqual(r.stdout.strip(), "another program is installing or updating software right now")

    def test_any_other_failure_is_1_with_aptss_own_line(self):
        self.apt("E: Sub-process /usr/bin/dpkg returned an error code (1)", 100)
        r = self.run_it()
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stdout.strip(), "E: Sub-process /usr/bin/dpkg returned an error code (1)")

    def test_the_real_script_trusts_nothing_and_is_read_whole_before_it_runs(self):
        src = (DEB / "remove-root").read_text()
        for line in ("set -eu", "PATH=/usr/sbin:/usr/bin:/sbin:/bin", "umask 077", "remove -y keephaven-guard-dog"):
            self.assertIn(line, src)
        self.assertNotIn("$1", src)
        self.assertNotIn("$@", src)
        self.assertNotIn("purge", src.split("main() {", 1)[1])  # a removal keeps the data; only apt purge deletes it
        self.assertEqual(src.rstrip().splitlines()[-2:], ["main", "exit $?"])  # apt deletes this file while it runs
        self.assertTrue(os.access(DEB / "remove-root", os.X_OK))


@unittest.skipUnless(DEB.is_dir(), "checks the source tree's packaging files")
class RemoveApp(unittest.TestCase):
    """bin/remove-app, the menu entry: asks, hands the removal to pkexec, and says how it went in a dialog."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "calls.log"
        src = (DEB / "remove-app").read_text()
        self.script = self.root / "remove-app"
        self.script.write_text(src.replace("PATH=/usr/bin:/bin", f"PATH={self.bin}:/usr/bin:/bin"))

    def tearDown(self):
        self.tmp.cleanup()

    def stand_ins(self, answer, inner=None, pkexec_rc=None):
        """zenity says yes (0) or no (1) to the question; pkexec forwards remove-root's output and exit code
        (inner) or fails on its own (pkexec_rc: 126 dialog dismissed, 127 not authorised)."""
        (self.bin / "zenity").write_text(f'#!/bin/sh\nfor a in "$@"; do printf "%s|" "$a"; done >> "{self.log}"; '
                                         f'echo >> "{self.log}"\ncase "$1" in --question) exit {answer};; esac\nexit 0\n')
        if pkexec_rc is not None:
            body = f"exit {pkexec_rc}"
        else:
            out, rc = inner
            body = f"printf '%s\\n' '{out}'\nexit {rc}"
        (self.bin / "pkexec").write_text(f'#!/bin/sh\necho "pkexec $*" >> "{self.log}"\n{body}\n')
        for tool in ("zenity", "pkexec"):
            (self.bin / tool).chmod(0o755)

    def run_it(self):
        r = subprocess.run(["/bin/sh", str(self.script)], capture_output=True, text=True, env={"PATH": "/nowhere"})
        return r, self.log.read_text().splitlines()

    def dialog(self, calls):
        return calls[-1]

    def test_cancel_at_the_question_removes_nothing(self):
        self.stand_ins(1, inner=("removed", 0))
        r, calls = self.run_it()
        self.assertEqual(r.returncode, 0)
        self.assertEqual(len(calls), 1)  # only the question; pkexec never ran
        self.assertIn("--question|", calls[0])
        self.assertIn("--text=Remove Keephaven Guard Dog from this computer? Your people and findings stay.|", calls[0])

    def test_removed(self):
        self.stand_ins(0, inner=("removed", 0))
        r, calls = self.run_it()
        self.assertEqual(calls[1], "pkexec /opt/keephaven-guard-dog/bin/remove-root")
        self.assertIn("--info|", self.dialog(calls))
        self.assertIn("Keephaven Guard Dog is removed. Your people and findings are still on this computer", self.dialog(calls))

    def test_password_not_given_dismissed_or_not_authorised(self):
        for rc in (126, 127):  # 126: the password dialog was dismissed; 127: not an admin, or pkexec failed
            self.log.unlink(missing_ok=True)
            self.stand_ins(0, pkexec_rc=rc)
            r, calls = self.run_it()
            self.assertIn("--info|", self.dialog(calls), rc)
            self.assertIn("--text=Not removed: the computer's password wasn't given.|", self.dialog(calls), rc)

    def test_lock_held_comes_through_pkexec_as_3(self):
        self.stand_ins(0, inner=("another program is installing or updating software right now", 3))
        r, calls = self.run_it()
        self.assertIn("--error|", self.dialog(calls))
        self.assertIn("--text=Not removed: another program is installing or updating software right now. "
                      "Close it or wait for it to finish, then try again.|", self.dialog(calls))

    def test_other_failure_shows_aptss_line(self):
        self.stand_ins(0, inner=("E: Sub-process /usr/bin/dpkg returned an error code (1)", 1))
        r, calls = self.run_it()
        self.assertIn("--error|", self.dialog(calls))
        self.assertIn("--text=Not removed: E: Sub-process /usr/bin/dpkg returned an error code (1)|", self.dialog(calls))

    def test_the_real_script_never_reads_opt_after_the_removal(self):
        src = (DEB / "remove-app").read_text()
        self.assertEqual(src.rstrip().splitlines()[-2:], ["main", "exit $?"])
        after = src.split("pkexec /opt/keephaven-guard-dog/bin/remove-root", 1)[1]
        self.assertNotIn("/opt", after)
        self.assertEqual(src.count("--no-markup"), src.count("zenity --"))  # apt's text can't turn into markup
        self.assertTrue(os.access(DEB / "remove-app", os.X_OK))


@unittest.skipUnless(DEB.is_dir(), "checks the source tree's packaging files")
class RemoveDeclared(unittest.TestCase):
    def test_menu_entry_policy_and_depends(self):
        entry = (DEB / "keephaven-guard-dog-remove.desktop").read_text()
        self.assertIn("Exec=/opt/keephaven-guard-dog/bin/remove-app\n", entry)
        cats = lambda t: [l for l in t.splitlines() if l.startswith("Categories=")]
        self.assertEqual(cats(entry), cats((DEB / "keephaven-guard-dog-reset-password.desktop").read_text()))
        policy = (DEB / "co.keephaven.guard-dog.remove.policy").read_text()
        self.assertIn('<action id="co.keephaven.guard-dog.remove">', policy)
        self.assertIn("<message>Remove Keephaven Guard Dog. Your people and findings stay.</message>", policy)
        self.assertEqual(policy.count("<allow_active>auth_admin</allow_active>"), 1)
        self.assertNotIn(">yes<", policy)
        self.assertIn(">/opt/keephaven-guard-dog/bin/remove-root</annotate>", policy)
        depends = [l for l in (DEB / "control.in").read_text().splitlines() if l.startswith("Depends: ")][0]
        self.assertIn(" zenity,", depends)
        self.assertIn(" pkexec | policykit-1,", depends)
        for name in ("remove-app", "remove-root", "keephaven-guard-dog-remove.desktop", "co.keephaven.guard-dog.remove.policy"):
            text = (DEB / name).read_text()
            self.assertNotIn("@", text, name)
            self.assertNotIn("dafarusd", text, name)

    def test_the_build_installs_and_checks_them(self):
        text = BUILD_DEB.read_text()
        self.assertIn('"$SRC/packaging/deb/remove-app" "$SRC/packaging/deb/remove-root" "$PKG/opt/keephaven-guard-dog/bin/"', text)
        self.assertIn('"$SRC/packaging/deb/keephaven-guard-dog-remove.desktop" "$PKG/usr/share/applications/"', text)
        self.assertIn('"$SRC/packaging/deb/co.keephaven.guard-dog.remove.policy" "$PKG/usr/share/polkit-1/actions/"', text)
        self.assertIn('for f in "$PKG"/usr/share/polkit-1/actions/*.policy; do', text)
        self.assertIn("isn't a script in this package", text)

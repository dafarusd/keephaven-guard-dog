"""The packaged edition's hidden-browser download (guard_dog/browser.py) against a local server and a small fake zip:
checked, unpacked in one step, and nothing half-done left behind whatever goes wrong."""
import hashlib
import http.server
import io
import os
import stat
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from guard_dog import browser


def fake_zip(extra=None):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        i = zipfile.ZipInfo("chrome-linux64/chrome")
        i.external_attr = (stat.S_IFREG | 0o755) << 16
        z.writestr(i, "#!/bin/sh\necho chrome\n")
        i = zipfile.ZipInfo("chrome-linux64/locales/en-US.pak")
        i.external_attr = (stat.S_IFREG | 0o644) << 16
        z.writestr(i, "x" * 1000)
        for info, data in extra or []:
            z.writestr(info, data)
    return buf.getvalue()


class Server:
    def __init__(self, routes):
        self.routes = routes  # path -> bytes | ("redirect", to) | ("stall", seconds)

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(h):
                r = self.routes.get(h.path)
                if isinstance(r, tuple) and r[0] == "redirect":
                    h.send_response(307)
                    h.send_header("Location", r[1])
                    h.end_headers()
                    return
                if isinstance(r, tuple) and r[0] == "stall":
                    h.send_response(200)
                    h.send_header("Content-Length", "1000")
                    h.end_headers()
                    h.wfile.write(b"x" * 10)
                    h.wfile.flush()
                    time.sleep(r[1])
                    return
                h.send_response(200)
                h.send_header("Content-Length", str(len(r)))
                h.end_headers()
                h.wfile.write(r)

            def log_message(h, *a):
                pass
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class Download(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.browsers = self.dir / "browsers"
        self.pinfile = self.dir / "browser.pin"
        self.patches = [mock.patch.object(browser, "PIN", self.pinfile),
                        mock.patch.dict(os.environ, {"PLAYWRIGHT_BROWSERS_PATH": str(self.browsers)})]
        for p in self.patches:
            p.start()
        browser.INSTALL.update(state="", note="", got=0, size=0)
        self.servers = []

    def tearDown(self):
        for s in self.servers:
            s.close()
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def serve(self, routes):
        s = Server(routes)
        self.servers.append(s)
        return s

    def write_pin(self, url, data, size=None, sha=None):
        self.pinfile.write_text("# header\n" + " ".join([url, sha or hashlib.sha256(data).hexdigest(),
                                                       str(size if size is not None else len(data)), "1248", "156.0.8078.4",
                                                       "chromium-1248", "chrome-linux64"]) + "\n")

    def run_install(self):
        browser.install()
        for _ in range(500):
            if browser.INSTALL["state"] != "downloading":
                break
            time.sleep(0.01)
        return browser.INSTALL

    def nothing_left(self):
        left = sorted(p.name for p in self.browsers.iterdir()) if self.browsers.exists() else []
        self.assertEqual(left, [])

    def test_good_download_unpacks_in_one_step(self):
        data = fake_zip()
        s = self.serve({"/z.zip": data})
        self.write_pin(s.base + "/z.zip", data)
        self.assertFalse(browser.installed())
        st = self.run_install()
        self.assertEqual(st["state"], "done", st)
        self.assertEqual(st["got"], len(data))
        d = self.browsers / "chromium-1248"
        self.assertTrue((d / "INSTALLATION_COMPLETE").is_file() and (d / "DEPENDENCIES_VALIDATED").is_file())
        self.assertTrue(os.access(d / "chrome-linux64" / "chrome", os.X_OK))
        self.assertFalse(os.access(d / "chrome-linux64" / "locales" / "en-US.pak", os.X_OK))
        self.assertEqual(sorted(p.name for p in self.browsers.iterdir()), ["chromium-1248"])  # no zip, no temp
        self.assertTrue(browser.installed())

    def test_wrong_fingerprint_is_refused_and_nothing_left(self):
        data = fake_zip()
        s = self.serve({"/z.zip": data})
        self.write_pin(s.base + "/z.zip", data, sha="0" * 64)
        st = self.run_install()
        self.assertEqual((st["state"], st["note"]), ("failed", "the download doesn't match the browser this Guard Dog was built with"))
        self.nothing_left()

    def test_short_or_long_downloads_are_refused(self):
        data = fake_zip()
        s = self.serve({"/z.zip": data})
        self.write_pin(s.base + "/z.zip", data, size=len(data) + 5)
        self.assertEqual(self.run_install()["state"], "failed")
        self.nothing_left()
        self.write_pin(s.base + "/z.zip", data, size=len(data) - 5)
        st = self.run_install()
        self.assertEqual(st["note"], "the download is bigger than the browser this Guard Dog was built with")
        self.nothing_left()

    def test_unsafe_entries_are_refused(self):
        for name, mode in (("../evil", stat.S_IFREG | 0o644), ("/abs/evil", stat.S_IFREG | 0o644),
                           ("chrome-linux64/link", stat.S_IFLNK | 0o777)):
            i = zipfile.ZipInfo(name)
            i.external_attr = mode << 16
            data = fake_zip([(i, "/etc/passwd")])
            s = self.serve({"/z.zip": data})
            self.write_pin(s.base + "/z.zip", data)
            st = self.run_install()
            self.assertEqual(st["state"], "failed", name)
            self.assertIn("unsafe entry", st["note"])
            self.nothing_left()
            self.assertFalse((self.dir / "evil").exists())

    def test_not_enough_room_says_how_much(self):
        data = fake_zip()
        s = self.serve({"/z.zip": data})
        self.write_pin(s.base + "/z.zip", data)
        with mock.patch.object(browser.shutil, "disk_usage", return_value=mock.Mock(free=300 << 20)):
            st = self.run_install()
        self.assertEqual(st["note"], "there's 300 MB free where it goes; it needs about 700 MB")
        self.nothing_left()

    def test_a_stalled_connection_ends_in_a_failure(self):
        s = self.serve({"/z.zip": ("stall", 3)})
        self.write_pin(s.base + "/z.zip", b"x" * 1000, size=1000)
        with mock.patch.object(browser, "READ_TIMEOUT", 0.5):
            st = self.run_install()
        self.assertEqual(st["state"], "failed")
        self.assertIn("timed out", st["note"].lower())
        self.nothing_left()

    def test_a_redirect_is_followed_and_the_fingerprint_still_decides(self):
        data = fake_zip()
        s = self.serve({"/z.zip": ("redirect", "/real.zip"), "/real.zip": data})
        self.write_pin(s.base + "/z.zip", data)
        self.assertEqual(self.run_install()["state"], "done")

    def test_a_second_press_while_downloading_starts_nothing(self):
        data = fake_zip()
        s = self.serve({"/z.zip": data})
        self.write_pin(s.base + "/z.zip", data)
        with mock.patch.object(browser.threading, "Thread") as t:
            browser.install()
            browser.install()
        self.assertEqual(t.call_count, 1)

    def test_a_restart_mid_download_ends_clean(self):
        data = fake_zip()
        s = self.serve({"/z.zip": data})
        self.write_pin(s.base + "/z.zip", data)
        self.browsers.mkdir()
        (self.browsers / ".download-1248.zip").write_bytes(b"half")
        (self.browsers / ".unpack-1248" / "chrome-linux64").mkdir(parents=True)
        (self.browsers / "keep-me").write_text("someone else's")
        browser.clean_leftovers()
        self.assertEqual(sorted(p.name for p in self.browsers.iterdir()), ["keep-me"])  # only its own named things
        self.assertEqual(self.run_install()["state"], "done")

    def test_without_a_pin_it_stays_out_of_the_way(self):
        self.assertIsNone(browser.pin())
        self.assertFalse(browser.installed())
        browser.clean_leftovers()
        browser.install()
        self.assertEqual(browser.INSTALL["state"], "failed")

    def test_folders_in_the_zip_and_a_leftover_temp_are_handled(self):
        i = zipfile.ZipInfo("chrome-linux64/lib/")
        i.external_attr = (stat.S_IFDIR | 0o755) << 16
        data = fake_zip([(i, "")])
        s = self.serve({"/z.zip": data})
        self.write_pin(s.base + "/z.zip", data)
        (self.browsers / ".unpack-1248" / "old").mkdir(parents=True)  # left by a crash between starts
        self.assertEqual(self.run_install()["state"], "done")
        self.assertTrue((self.browsers / "chromium-1248" / "chrome-linux64" / "lib").is_dir())
        self.assertFalse((self.browsers / "chromium-1248" / "old").exists())

    def test_a_zip_without_the_browser_in_it_is_refused(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("something-else/readme.txt", "hi")
        data = buf.getvalue()
        s = self.serve({"/z.zip": data})
        self.write_pin(s.base + "/z.zip", data)
        st = self.run_install()
        self.assertEqual(st["note"], "the download doesn't hold the browser where it should")
        self.nothing_left()

    def test_an_existing_browser_folder_is_never_overwritten(self):
        data = fake_zip()
        s = self.serve({"/z.zip": data})
        self.write_pin(s.base + "/z.zip", data)
        (self.browsers / "chromium-1248").mkdir(parents=True)
        st = self.run_install()
        self.assertEqual(st["note"], "chromium-1248 is already there")
        self.assertEqual(sorted(p.name for p in self.browsers.iterdir()), ["chromium-1248"])

    def test_the_owners_proxy_is_used(self):
        data = fake_zip()
        s = self.serve({"/z.zip": data})
        self.write_pin(s.base + "/z.zip", data)
        seen = []
        real = browser.urllib.request.build_opener
        with mock.patch.object(browser.urllib.request, "build_opener", side_effect=lambda *h: seen.extend(h) or real()):
            browser.install(proxy="http://127.0.0.1:3128")
            for _ in range(500):
                if browser.INSTALL["state"] != "downloading":
                    break
                time.sleep(0.01)
        self.assertEqual(seen[0].proxies, {"http": "http://127.0.0.1:3128", "https": "http://127.0.0.1:3128"})
        self.assertEqual(browser.INSTALL["state"], "done")

    def test_failed_is_only_shown_after_the_clean_up(self):
        """arm64 build, 2026-10-30: three times slower, it saw "failed" while the temp folder was still there."""
        i = zipfile.ZipInfo("../evil")
        i.external_attr = (stat.S_IFREG | 0o644) << 16
        data = fake_zip([(i, "x")])
        s = self.serve({"/z.zip": data})
        self.write_pin(s.base + "/z.zip", data)
        real = browser.shutil.rmtree
        with mock.patch.object(browser.shutil, "rmtree", side_effect=lambda p: (time.sleep(0.5), real(p))):
            st = self.run_install()
            self.assertEqual(st["state"], "failed")
            self.nothing_left()

import unittest

from guard_dog import web


class Setup(unittest.TestCase):
    def test_window(self):
        web.STARTED = __import__("time").time()
        web.TAILSCALE = True
        for ip in ("127.0.0.1", "192.168.1.20", "10.0.0.5", "100.64.0.12", "fd7a:115c:a1e0::1"):
            self.assertTrue(web.setup_open(ip), ip)
        for ip in ("8.8.8.8", "2001:4860::8888", "garbage"):
            self.assertFalse(web.setup_open(ip), ip)
        self.assertFalse(web.setup_open("169.254.3.4"))
        web.TAILSCALE = False  # behind an ISP's carrier-grade NAT, 100.64/10 is strangers
        self.assertFalse(web.setup_open("100.64.0.12"))
        self.assertTrue(web.setup_open("192.168.1.20"))
        web.STARTED -= web.SETUP_WINDOW + 1
        self.assertFalse(web.setup_open("192.168.1.20"))


if __name__ == "__main__":
    unittest.main()

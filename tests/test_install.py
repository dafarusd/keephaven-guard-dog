"""install.sh end to end against stand-ins for python3 -m venv, pip, scrapling, systemctl and loginctl:
nothing is installed and no real service is touched. The fake service listens on a free port when started."""
import os
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

FAKE_PYTHON = r'''#!/bin/sh
case "$2" in *ensurepip*) exit 0 ;; esac  # "has venv": the machine running the tests may not, and it isn't the point
if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then
  mkdir -p "$3/bin"
  for t in pip keephaven-guard-dog; do printf '#!/bin/sh\nexit 0\n' > "$3/bin/$t"; chmod +x "$3/bin/$t"; done
  printf '#!/bin/sh\necho "Installing Installing browsers..."\n[ -n "$FAIL_BROWSER" ] && { echo "Host system is missing dependencies"; exit 1; }\nexit 0\n' > "$3/bin/scrapling"
  chmod +x "$3/bin/scrapling"
  exit 0
fi
exec REAL_PYTHON "$@"
'''

# The fake service: "restart" stops the old listener and starts a new one, which writes a fresh setup code first
# in network mode (as web.build does), after START_DELAY seconds.
FAKE_SYSTEMCTL = r'''#!/bin/sh
echo "$*" >> "$LOG"
S="$HOME/.fake-service"
case "$*" in
  *is-active*) [ -f "$S.pid" ] && kill -0 "$(cat "$S.pid")" 2>/dev/null; exit $? ;;
  *restart*)
    [ -f "$S.pid" ] && kill "$(cat "$S.pid")" 2>/dev/null
    NET=0; grep -q -- "--host 0.0.0.0" "$HOME/.config/systemd/user/keephaven-guard-dog.service" && NET=1
    REAL_PYTHON -c "
import os, socket, sys, time
time.sleep(float(os.environ.get('START_DELAY', '0')))
d = os.path.join(os.environ['HOME'], '.local/share/keephaven-guard-dog'); os.makedirs(d, exist_ok=True)
if $NET and not os.path.exists(os.path.join(d, 'password-set')):
    open(os.path.join(d, 'setup-code.txt'), 'w').write('ab12-cd34-ef56\n')
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind(('127.0.0.1', int(os.environ['GD_PORT']))); s.listen()
while True: s.accept()[0].close()
" > /dev/null 2>&1 &
    echo $! > "$S.pid" ;;
esac
exit 0
'''


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Install(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        bin_ = self.home / "fakebin"
        bin_.mkdir()
        real = subprocess.run(["sh", "-c", "command -v python3"], capture_output=True, text=True).stdout.strip()
        for name, body in (("python3", FAKE_PYTHON), ("systemctl", FAKE_SYSTEMCTL)):
            (bin_ / name).write_text(body.replace("REAL_PYTHON", real))
            (bin_ / name).chmod(0o755)
        for name, body in (("loginctl", "#!/bin/sh\nexit 0\n"), ("hostname", "#!/bin/sh\necho 192.168.1.50\n"),
                           ("sudo", "#!/bin/sh\necho sudo should not be needed; exit 1\n")):
            (bin_ / name).write_text(body)
            (bin_ / name).chmod(0o755)
        self.log = self.home / "systemctl.log"
        self.env = dict(os.environ, HOME=str(self.home), PATH=f"{bin_}:{os.environ['PATH']}", LOG=str(self.log),
                        GD_PORT=str(free_port()), GD_WAIT="10")
        self.env.pop("XDG_DATA_HOME", None)

    def tearDown(self):
        pid = self.home / ".fake-service.pid"
        if pid.exists():
            subprocess.run(["kill", pid.read_text().strip()], capture_output=True)
        self.tmp.cleanup()

    def run_install(self, *args, **env):
        p = subprocess.run(["bash", str(ROOT / "install.sh"), *args], capture_output=True, text=True,
                           env=dict(self.env, **env), timeout=60)
        return p.returncode, p.stdout + p.stderr

    def unit(self):
        return (self.home / ".config/systemd/user/keephaven-guard-dog.service").read_text()

    def test_local_install_starts_and_says_where(self):
        code, out = self.run_install()
        self.assertEqual(code, 0, out)
        self.assertIn("--host 127.0.0.1", self.unit())
        self.assertIn(f"Done. Open http://127.0.0.1:{self.env['GD_PORT']} on this machine.", out)
        self.assertNotIn("Restarting Guard Dog", out)  # nothing was running yet
        self.assertNotIn("Installing Installing", out)  # the browser step's own chatter stays out unless it fails
        calls = self.log.read_text().splitlines()
        self.assertEqual([c for c in calls if "is-active" not in c],
                         ["--user daemon-reload", "--user enable keephaven-guard-dog.service",
                          "--user restart keephaven-guard-dog.service"])

    def test_switching_to_network_restarts_the_running_copy_and_prints_the_code(self):
        """VM 2026-10-30: --network on a running install left the old local-only copy serving until a reboot."""
        self.assertEqual(self.run_install()[0], 0)
        old = (self.home / ".fake-service.pid").read_text()
        code, out = self.run_install("--network", START_DELAY="1.5")
        self.assertEqual(code, 0, out)
        self.assertIn("--host 0.0.0.0", self.unit())
        self.assertIn("Restarting Guard Dog (a running scan stops and runs again at the next check).", out)
        self.assertNotEqual((self.home / ".fake-service.pid").read_text(), old)  # a new process, new settings
        self.assertIn(f"Done. Open http://192.168.1.50:{self.env['GD_PORT']}", out)
        self.assertIn("If it asks for a setup code: ab12-cd34-ef56", out)
        self.assertNotIn("journalctl", out)
        self.assertEqual(self.log.read_text().count("restart"), 2)

    def test_an_old_code_isnt_printed(self):
        d = self.home / ".local/share/keephaven-guard-dog"
        d.mkdir(parents=True)
        (d / "setup-code.txt").write_text("0000-0000-0000\n")
        os.utime(d / "setup-code.txt", (time.time() - 3600, time.time() - 3600))
        (d / "password-set").write_text("")  # a password is set: the new copy writes no code
        code, out = self.run_install("--network")
        self.assertEqual(code, 0, out)
        self.assertNotIn("0000-0000-0000", out)
        self.assertNotIn("setup code", out)

    def test_still_starting_names_the_file_never_a_command(self):
        code, out = self.run_install("--network", START_DELAY="30", GD_WAIT="2")
        self.assertEqual(code, 0, out)
        self.assertIn("Guard Dog is still starting; open http://192.168.1.50", out)
        self.assertIn(f"If it asks for a setup code, it's in {self.home}/.local/share/keephaven-guard-dog/setup-code.txt", out)
        self.assertNotIn("journalctl", out)

    def test_a_browser_that_wont_install_shows_why_and_stops(self):
        code, out = self.run_install(FAIL_BROWSER="1")
        self.assertEqual(code, 1)
        self.assertIn("Host system is missing dependencies", out)
        self.assertIn("The browser didn't install (details above).", out)
        self.assertFalse(self.log.exists())  # never got as far as the service


if __name__ == "__main__":
    unittest.main()

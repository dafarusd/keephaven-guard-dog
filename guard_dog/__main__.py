"""keephaven-guard-dog [--data DIR] [--host 127.0.0.1] [--port 8642]"""
import argparse
import errno
import os
import sys
import threading
from pathlib import Path

from . import __version__, browser, scanner, web
from .db import DB


def default_data():
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home())) / "Keephaven Guard Dog"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Keephaven Guard Dog"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "keephaven-guard-dog"


RESET = "reset-password"  # written by the admin-only "Reset Guard Dog password" menu entry (packaging/deb)


def password_reset(db):
    """An admin asked to reset the dashboard password: clear it once (people, findings and everything else stay).
    The request is the marker file's mtime: one that can't be removed is noted and ignored, never acted on again
    until a new one is written."""
    f = db.dir / RESET
    try:
        when = str(f.stat().st_mtime_ns)
    except OSError:
        return False
    if db.get("reset_handled") == when:
        return False
    db.set("password_hash", "")
    db.set("reset_handled", when)
    if db.get("network") == "1":  # reopened by the owner once the new password is set, not before
        db.set("network", "0")
        db.alert(None, "Dashboard closed to the network again: its password was reset")
    print("The dashboard password was reset by an admin; set a new one on the dashboard.", flush=True)
    try:
        f.unlink()
    except OSError as ex:
        print(f"Couldn't remove {f.name} ({type(ex).__name__}); it's ignored until a new reset is asked for.", flush=True)
    return True


def main():
    ap = argparse.ArgumentParser(prog="keephaven-guard-dog", description="Find, clean up and watch where a person shows up online.")
    ap.add_argument("--data", type=Path, default=default_data(), help="where Guard Dog keeps its files (e.g. on an SSD)")
    ap.add_argument("--host", default="127.0.0.1", help="127.0.0.1 = this machine only; 0.0.0.0 = your network too (asks for a password)")
    ap.add_argument("--port", type=int, default=8642)
    ap.add_argument("--allow-host", action="append", default=[], metavar="NAME",
                    help="another name this machine is reached by (network mode), e.g. picloud")
    ap.add_argument("--require-password", action="store_true",
                    help="ask for a password on 127.0.0.1 too (other accounts on this computer can open it)")
    ap.add_argument("--network-switch", action="store_true",
                    help="let Settings open the dashboard to the network (the packaged edition; --host stays the default)")
    ap.add_argument("--version", action="version", version=__version__)
    a = ap.parse_args()
    os.umask(0o077)  # everything Guard Dog writes is for this user only
    scanner.quiet_logs()
    db = DB(a.data)
    os.chmod(a.data, 0o700) if os.name == "posix" else None
    password_reset(db)
    browser.clean_leftovers()  # a download a restart cut short: offered again, not left half-done
    # Scans start only once the dashboard is listening: at boot the address may not exist yet (Tailscale still
    # starting), and a process that can't serve exits, leaving any scan it started marked as running.
    try:
        web.serve(db, a.host, a.port, a.allow_host,
                  ready=lambda: threading.Thread(target=scanner.scheduler, args=(db,), daemon=True).start(),
                  require_password=a.require_password, switch=a.network_switch)
    except OSError as ex:  # one line a person can read in the log, not a traceback
        if ex.errno == errno.EADDRNOTAVAIL:
            sys.exit(f"{a.host} isn't on this machine yet (still connecting?); it's tried again in 15 s.")
        if ex.errno == errno.EADDRINUSE:
            sys.exit(f"Port {a.port} is already in use by another program; it's tried again in 15 s.")
        raise


if __name__ == "__main__":
    main()

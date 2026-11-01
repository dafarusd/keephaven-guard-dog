"""The packaged edition's hidden browser: downloaded once from Settings, checked against the fingerprint the package
was built with, unpacked where Playwright looks for it. The install.sh edition uses `scrapling install` instead; this
module is only active when /opt/keephaven-guard-dog/browser.pin exists (it ships with the .deb)."""
import hashlib
import os
import shutil
import stat
import sys
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

PIN = Path(sys.prefix).parent / "browser.pin"   # the venv is /opt/keephaven-guard-dog/venv
NEED_MB = 700        # the zip (about 200 MB), the unpacked browser (about 400 MB) and room to spare
READ_TIMEOUT = 60    # s without a byte and the download gives up instead of hanging with the switch off
CHUNK = 1 << 20
INSTALL = {"state": "", "note": "", "got": 0, "size": 0}  # "", "downloading", "done", "failed"
_lock = threading.Lock()


def pin():
    """{url, sha256, size, revision, version, dir, subdir} or None (not the packaged edition)."""
    try:
        line = next(ln for ln in PIN.read_text().splitlines() if ln.strip() and not ln.startswith("#"))
    except (OSError, StopIteration):
        return None
    url, sha, size, rev, version, d, sub = line.split()
    return dict(url=url, sha256=sha, size=int(size), revision=rev, version=version, dir=d, subdir=sub)


def root():
    return Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or Path.home() / ".cache" / "ms-playwright")


def leftovers(p):
    r = root()
    return r / f".download-{p['revision']}.zip", r / f".unpack-{p['revision']}"


def installed(p=None):
    p = p or pin()
    if not p:
        return False
    d = root() / p["dir"]
    return (d / "INSTALLATION_COMPLETE").is_file() and (d / p["subdir"] / "chrome").is_file()


def clean_leftovers():
    """At start: a download or unpack that a restart cut short leaves its own named file and folder; remove them so
    Settings offers the download again."""
    p = pin()
    if not p:
        return
    z, tmp = leftovers(p)
    z.unlink(missing_ok=True)
    if tmp.is_dir():
        shutil.rmtree(tmp)


def unpack(zpath, dest):
    """Every member under dest, exec bits kept; absolute paths, '..' and symlinks refused (zipfile would write a
    symlink as a plain file)."""
    with zipfile.ZipFile(zpath) as z:
        for info in z.infolist():
            name = info.filename
            parts = Path(name).parts
            mode = info.external_attr >> 16
            if name.startswith("/") or ".." in parts or stat.S_ISLNK(mode):
                raise ValueError(f"the download holds an unsafe entry ({name[:60]})")
        for info in z.infolist():
            target = dest / info.filename
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out, CHUNK)
            mode = (info.external_attr >> 16) & 0o777
            os.chmod(target, (mode or 0o644) & 0o755)


def install(opener=None, proxy=""):
    """The owner pressed Download it: fetch, check, unpack, in the background."""
    p = pin()
    with _lock:
        if INSTALL["state"] == "downloading":
            return
        if not p:
            INSTALL.update(state="failed", note="this edition doesn't download its browser from Settings")
            return
        INSTALL.update(state="downloading", note="", got=0, size=p["size"])  # before the thread: a second press waits
    threading.Thread(target=_go, args=(p, opener, proxy), daemon=True).start()


def _go(p, opener, proxy):
    r = root()
    z, tmp = leftovers(p)
    result = {}
    try:
        r.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(r).free // (1 << 20)
        if free < NEED_MB:
            raise RuntimeError(f"there's {free} MB free where it goes; it needs about {NEED_MB} MB")
        if opener is None:
            handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy})] if proxy else []
            opener = urllib.request.build_opener(*handlers)
        h = hashlib.sha256()
        got = 0
        with opener.open(p["url"], timeout=READ_TIMEOUT) as resp, open(z, "wb") as out:
            while True:
                b = resp.read(CHUNK)
                if not b:
                    break
                got += len(b)
                if got > p["size"]:
                    raise RuntimeError("the download is bigger than the browser this Guard Dog was built with")
                h.update(b)
                out.write(b)
                INSTALL["got"] = got
        if got != p["size"] or h.hexdigest() != p["sha256"]:
            raise RuntimeError("the download doesn't match the browser this Guard Dog was built with")
        if tmp.is_dir():
            shutil.rmtree(tmp)
        tmp.mkdir()
        unpack(z, tmp)
        exe = tmp / p["subdir"] / "chrome"
        if not exe.is_file():
            raise RuntimeError("the download doesn't hold the browser where it should")
        for marker in ("INSTALLATION_COMPLETE", "DEPENDENCIES_VALIDATED"):
            (tmp / marker).write_text("")
        final = r / p["dir"]
        if final.exists():
            raise RuntimeError(f"{final.name} is already there")
        os.rename(tmp, final)  # one step: a half-unpacked browser is never where Playwright looks
        result = dict(state="done", when=time.time())
    except Exception as ex:
        result = dict(state="failed", note=str(ex)[:160] if isinstance(ex, (RuntimeError, ValueError))
                      else f"{type(ex).__name__}: {str(ex)[:120]}")
    finally:
        z.unlink(missing_ok=True)
        if tmp.is_dir():
            shutil.rmtree(tmp)
        # only now: whoever sees "done" or "failed" (Settings, a test) sees the clean-up already finished
        # (the arm64 build, three times slower, caught the old order showing "failed" with the temp folder still there)
        INSTALL.update(result or dict(state="failed", note="stopped"))

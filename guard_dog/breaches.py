"""Breach checks. XposedOrNot needs no key; Have I Been Pwned needs the owner's own paid key.
Password check: only the first 5 characters of the SHA-1 leave the machine (HIBP range API)."""
import hashlib
import json
import os
import time
import urllib.parse
import urllib.request

UA = "keephaven-guard-dog/0.1 (+https://github.com/dafarusd)"
_xon_meta = {"ts": 0, "by_id": {}}


def _get(url, headers=None, timeout=30):
    if os.environ.get("GUARD_DOG_NO_NETWORK") == "1":  # tests: any real request is a bug
        raise RuntimeError(f"network call in a no-network run: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _xon_details():
    if time.time() - _xon_meta["ts"] > 86400:
        st, body = _get("https://api.xposedornot.com/v1/breaches")
        if st == 200:
            _xon_meta["by_id"] = {b["breachID"]: b for b in json.loads(body).get("exposedBreaches", [])}
            _xon_meta["ts"] = time.time()
    return _xon_meta["by_id"]


def xposedornot(email):
    st, body = _get("https://api.xposedornot.com/v1/check-email/" + urllib.parse.quote(email))
    if st == 404:
        return []
    if st != 200:
        raise RuntimeError(f"XposedOrNot answered {st}")
    names = (json.loads(body).get("breaches") or [[]])[0]
    meta = _xon_details()
    out = []
    for n in names:
        m = meta.get(n, {})
        out.append(dict(breach=n, domain=m.get("domain", ""), date=(m.get("breachedDate") or "")[:10],
                        exposed=", ".join(m.get("exposedData") or []),
                        password_risk=m.get("passwordRisk", "unknown")))
    return out


def hibp(email, key):
    st, body = _get("https://haveibeenpwned.com/api/v3/breachedaccount/" + urllib.parse.quote(email)
                    + "?truncateResponse=false", {"hibp-api-key": key})
    if st == 404:
        return []
    if st != 200:
        raise RuntimeError(f"Have I Been Pwned answered {st}")
    return [dict(breach=b["Name"], domain=b.get("Domain", ""), date=b.get("BreachDate", ""),
                 exposed=", ".join(b.get("DataClasses", [])),
                 password_risk="exposed" if "Passwords" in b.get("DataClasses", []) else "none")
            for b in json.loads(body)]


def password_seen(password):
    """How many times this password appears in known breaches (0 = never seen)."""
    h = hashlib.sha1(password.encode()).hexdigest().upper()
    st, body = _get("https://api.pwnedpasswords.com/range/" + h[:5], {"Add-Padding": "true"})
    if st != 200:
        raise RuntimeError(f"Pwned Passwords answered {st}")
    for line in body.decode().splitlines():
        suffix, _, n = line.partition(":")
        if suffix == h[5:]:
            return int(n)
    return 0


def password_leaked(b):
    r = (b.get("password_risk") or "").lower()
    return r in ("exposed", "plaintext", "easytocrack") or "password" in (b.get("exposed") or "").lower()

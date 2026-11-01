"""The list that ends the .deb's copy of THIRD-PARTY.md: every package in the app's environment, with its version and
license, read from that build's own .dist-info folders. Run by tools/build-deb.sh with the environment's Python:

    /opt/keephaven-guard-dog/venv/bin/python -I tools/third_party.py SITE_PACKAGES >> THIRD-PARTY.md

It stops (exit 1) if a .dist-info folder can't be read, so a package can't ship without being named.
"""
import sys
from importlib.metadata import PathDistribution
from pathlib import Path

OWN = "keephaven-guard-dog"


def license_of(meta):
    """SPDX expression first, then the license classifiers, then a short License field."""
    if meta.get("License-Expression"):
        return meta["License-Expression"]
    found = [c.split("::")[-1].strip() for c in meta.get_all("Classifier") or [] if c.startswith("License ::")]
    found = [c for c in found if c != "OSI Approved"]
    if found:
        return ", ".join(dict.fromkeys(found))
    text = (meta.get("License") or "").strip()
    if text and "\n" not in text and len(text) <= 80:
        return text
    return "see its .dist-info folder"


def rows(site):
    out, bad = [], []
    for d in sorted(Path(site).glob("*.dist-info")):
        meta = PathDistribution(d).metadata
        if not meta or not meta.get("Name") or not meta.get("Version"):
            bad.append(d.name)
            continue
        if meta["Name"].lower().replace("_", "-") == OWN:
            continue
        out.append((meta["Name"], meta["Version"], license_of(meta)))
    return out, bad


def main(argv):
    out, bad = rows(argv[1])
    if bad or not out:
        print("STOP: can't read " + (", ".join(bad) or f"any package in {argv[1]}"), file=sys.stderr)
        return 1
    print(f"\n## The packages in this build\n\n{len(out)} packages in `/opt/keephaven-guard-dog/venv`, read from their "
          ".dist-info folders when this .deb was built.\n\n| Package | Version | License |\n|---|---|---|")
    for name, version, lic in sorted(out, key=lambda r: r[0].lower()):
        print(f"| {name} | {version} | {lic.replace('|', '/')} |")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

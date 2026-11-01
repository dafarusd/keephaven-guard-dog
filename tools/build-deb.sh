#!/usr/bin/env bash
# Build keephaven-guard-dog_<version>_<arch>.deb. Runs as root on a throwaway Debian build machine (a VM: the app is
# built at its real path, /opt/keephaven-guard-dog, so nothing in it points at a build folder).
#   tools/build-deb.sh ARCH SRC DL OUT COMMIT   ARCH = amd64 | arm64; SRC = a clean export of the repo;
#                                           DL = folder with the pinned Python tarball; OUT = where the .deb goes;
#                                           COMMIT = the commit SRC was exported from, written into BUILD
# The unit tests run against the installed tree with the bundled Python, so the interpreter that ships is the one
# tested. Nothing is downloaded except the locked wheels (pip --require-hashes) and, for the tests only, a browser.
set -euo pipefail
# First, before anything is created: a package that can't say what it was built from isn't a release build
[[ "${5:-}" =~ ^[0-9a-f]{7,40}$ ]] || { echo "STOP: give the source commit (7 to 40 hex characters) as the 5th argument"; exit 1; }
COMMIT="$5"
ARCH="$1"; SRC="$(cd "$2" && pwd)"; DL="$(cd "$3" && pwd)"; OUT="$(mkdir -p "$4" && cd "$4" && pwd)"
APP=/opt/keephaven-guard-dog
PIN="$SRC/packaging/deb/python.pin"
read -r _ TARBALL SHA < <(grep "^$ARCH " "$PIN")
PBS_TAG=$(sed -n 's/^PBS_TAG=//p' "$PIN"); PY_VERSION=$(sed -n 's/^PY_VERSION=//p' "$PIN")
VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$SRC/guard_dog/__init__.py")
LOCK="$SRC/packaging/deb/requirements-lock.txt"
[ -e "$APP" ] && { echo "STOP: $APP already exists on this build machine; use a fresh one"; exit 1; }

echo "== 1/7 Python $PY_VERSION ($PBS_TAG, $ARCH): checking $TARBALL"
echo "$SHA  $DL/$TARBALL" | sha256sum -c - || { echo "STOP: the Python tarball doesn't match the pinned sha256"; exit 1; }
mkdir -p "$APP"
tar -xzf "$DL/$TARBALL" -C "$APP"      # unpacks to $APP/python
# The licenses of what that Python links in (OpenSSL, SQLite, ...): the install_only archive leaves them out
install -d -m 755 "$APP/python/licenses"
install -m 644 "$SRC"/packaging/deb/python-licenses/*.txt "$APP/python/licenses/"
"$APP/python/bin/python3" -c 'import sys, ssl, sqlite3; print(sys.version.split()[0], ssl.OPENSSL_VERSION)'

echo "== 2/7 the app's own environment"
"$APP/python/bin/python3" -m venv "$APP/venv"
"$APP/venv/bin/python" -m pip install -q --disable-pip-version-check --require-hashes --no-deps -r "$LOCK"
WHEELS=$(mktemp -d)   # Guard Dog's own wheel, built in a throwaway environment with a hashed setuptools
"$APP/python/bin/python3" -m venv "$WHEELS/build-env"
"$WHEELS/build-env/bin/python" -m pip install -q --disable-pip-version-check --require-hashes --no-deps \
  -r "$SRC/packaging/deb/build-lock.txt"
"$WHEELS/build-env/bin/python" -m pip wheel -q --disable-pip-version-check --no-deps --no-build-isolation -w "$WHEELS" "$SRC"
"$APP/venv/bin/python" -m pip install -q --disable-pip-version-check --no-deps "$WHEELS"/keephaven_guard_dog-*.whl
# pip notes where it installed the wheel from (a temp file on this build machine): not something to ship
DIST=$(echo "$APP"/venv/lib/python3.12/site-packages/keephaven_guard_dog-*.dist-info)
rm -f -- "$DIST/direct_url.json"
sed -i '/direct_url.json/d' "$DIST/RECORD"
"$APP/venv/bin/python" -m pip check

echo "== 3/7 unit tests with the bundled Python (no network)"
TESTS=$(mktemp -d)
cp -r "$SRC/tests" "$SRC/tools" "$TESTS/"   # not guard_dog/: the tests import the installed copy in $APP
cp "$SRC/install.sh" "$TESTS/"
# The hidden browser for the real-browser tests (never shipped): fetched and checked by the same code the dashboard's
# Download it button runs, against the pin this package carries. Its system libraries are the package's own Depends
# (the first of each alternative), which also proves that list installs on this release.
grep "^$ARCH " "$SRC/packaging/deb/browser.pin" | cut -d' ' -f2- > "$APP/browser.pin"
[ -s "$APP/browser.pin" ] || { echo "STOP: no browser pin for $ARCH"; exit 1; }
DEPS=$(sed -n 's/^Depends: //p' "$SRC/packaging/deb/control.in" | tr ',' '\n' | sed 's/|.*//; s/(.*)//; s/ //g' | grep -v '^$')
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq $DEPS > "$TESTS/deps.log" 2>&1 || { cat "$TESTS/deps.log"; echo "STOP: Depends don't install"; exit 1; }
echo "   Depends: $(echo $DEPS | wc -w) packages, all installed: $(dpkg-query -W -f='${db:Status-Abbrev}\n' $DEPS | grep -c '^ii') ii"
export PLAYWRIGHT_BROWSERS_PATH="$TESTS/browsers"
"$APP/venv/bin/python" -I -c '
import sys, time
from guard_dog import browser
browser.install()
while browser.INSTALL["state"] == "downloading":
    time.sleep(1)
print("browser:", browser.INSTALL["state"], browser.INSTALL.get("note", ""), browser.pin()["version"])
sys.exit(0 if browser.installed() else 1)' || { echo "STOP: the pinned browser didn't download, check or unpack"; exit 1; }
RC=0; ( cd "$TESTS" && GUARD_DOG_NO_NETWORK=1 "$APP/venv/bin/python" -m unittest > "$OUT/tests-$ARCH.log" 2>&1 ) || RC=$?
grep -E "^(Ran|OK|FAILED)" "$OUT/tests-$ARCH.log"
[ "$RC" = 0 ] || { grep -E "^(FAIL|ERROR):" "$OUT/tests-$ARCH.log" | head -20; echo "STOP: the tests failed with the bundled Python"; exit 1; }
unset PLAYWRIGHT_BROWSERS_PATH

echo "== 4/7 clean-up and checks on $APP"
find "$APP" -name "__pycache__" -type d -prune -exec rm -r {} +    # the test run's, made from wherever it ran
"$APP/venv/bin/python" -m compileall -q "$APP/python/lib" "$APP/venv/lib" >/dev/null 2>&1 || true  # /opt is read-only at run time
grep -n "^home" "$APP/venv/pyvenv.cfg"
grep -q "^home = $APP/python/bin$" "$APP/venv/pyvenv.cfg" || { echo "STOP: pyvenv.cfg home isn't $APP/python/bin"; exit 1; }
# Paths of this build machine (its temp folders, the source export, its users' homes) must not be in what ships.
BUILDER_HOME=$(getent passwd "${SUDO_USER:-root}" | cut -d: -f6)
LEAKS=$(grep -rIlE -e "$TESTS" -e "$WHEELS" -e "$SRC" -e "$BUILDER_HOME" -e "(^|[^a-z])/root/" -e "/tmp/tmp\." "$APP" 2>/dev/null \
        | head -20 || true)
[ -z "$LEAKS" ] || { echo "STOP: build-machine paths inside $APP:"; echo "$LEAKS"; exit 1; }
echo "no build-machine paths inside $APP"
# Other /home/ mentions are text inside third-party code (listed so each one is seen, not fatal)
grep -rIl "/home/" "$APP" 2>/dev/null | sed "s|^$APP/||" | while read -r f; do
  echo "  third-party text with /home/: $f: $(grep -ohE '/home/[A-Za-z$]+' "$APP/$f" | sort -u | tr '\n' ' ')"
done
LOCK_SHA=$(sha256sum "$LOCK" | cut -c1-64)
printf 'python %s\npython-build-standalone %s\nlock sha256 %s\nbrowser %s\nsource commit %s\narch %s\n' \
  "$PY_VERSION" "$PBS_TAG" "$LOCK_SHA" "$(cut -d' ' -f5,2 "$APP/browser.pin")" "$COMMIT" "$ARCH" > "$APP/BUILD"
cat "$APP/BUILD"

echo "== 5/7 package tree"
ROOT=$(mktemp -d); PKG="$ROOT/keephaven-guard-dog"
mkdir -p "$PKG/DEBIAN" "$PKG/opt" "$PKG/usr/lib/systemd/system" "$PKG/usr/share/applications" \
         "$PKG/usr/share/doc/keephaven-guard-dog"
cp -a "$APP" "$PKG/opt/"
install -m 644 "$SRC/packaging/deb/keephaven-guard-dog.service" "$PKG/usr/lib/systemd/system/"
install -m 644 "$SRC/packaging/deb/keephaven-guard-dog.desktop" "$PKG/usr/share/applications/"
install -m 644 "$SRC/packaging/deb/keephaven-guard-dog-reset-password.desktop" "$PKG/usr/share/applications/"
# AppStream: without it Software and App Center list no app, so it can't be found or removed there like other apps
install -D -m 644 "$SRC/packaging/deb/co.keephaven.GuardDog.metainfo.xml" "$PKG/usr/share/metainfo/co.keephaven.GuardDog.metainfo.xml"
mkdir -p "$PKG/opt/keephaven-guard-dog/bin" "$PKG/usr/share/polkit-1/actions"
install -m 755 "$SRC/packaging/deb/reset-password" "$PKG/opt/keephaven-guard-dog/bin/"
install -m 644 "$SRC/packaging/deb/co.keephaven.guard-dog.reset-password.policy" "$PKG/usr/share/polkit-1/actions/"
# "Remove Keephaven Guard Dog": App Center (Ubuntu) and Package Install (Raspberry Pi OS) can't remove a .deb
install -m 755 "$SRC/packaging/deb/remove-app" "$SRC/packaging/deb/remove-root" "$PKG/opt/keephaven-guard-dog/bin/"
install -m 644 "$SRC/packaging/deb/keephaven-guard-dog-remove.desktop" "$PKG/usr/share/applications/"
install -m 644 "$SRC/packaging/deb/co.keephaven.guard-dog.remove.policy" "$PKG/usr/share/polkit-1/actions/"
install -m 644 "$SRC/packaging/deb/copyright" "$SRC/NOTICE" "$SRC/THIRD-PARTY.md" "$PKG/usr/share/doc/keephaven-guard-dog/"
# The shipped THIRD-PARTY.md ends with every package in this build's environment, with its version and license
"$APP/venv/bin/python" -I "$SRC/tools/third_party.py" "$APP/venv/lib/python3.12/site-packages" \
  >> "$PKG/usr/share/doc/keephaven-guard-dog/THIRD-PARTY.md" || { echo "STOP: couldn't list the packages' licenses"; exit 1; }
echo "   THIRD-PARTY.md: $(grep -o '^[0-9]* packages in' "$PKG/usr/share/doc/keephaven-guard-dog/THIRD-PARTY.md")"
printf 'keephaven-guard-dog (%s) unstable; urgency=medium\n\n  * Release %s.\n\n -- dafarusd <dafarusd@users.noreply.github.com>  %s\n' \
  "$VERSION" "$VERSION" "$(date -R)" | gzip -9n > "$PKG/usr/share/doc/keephaven-guard-dog/changelog.gz"
install -m 755 "$SRC/packaging/deb/postinst" "$SRC/packaging/deb/prerm" "$SRC/packaging/deb/postrm" "$PKG/DEBIAN/"
chmod -R u=rwX,go=rX "$PKG/opt" "$PKG/usr"   # the Python archive comes group-writable; nothing here needs that
( cd "$PKG" && find opt usr -type f -print0 | sort -z | xargs -0 md5sum ) > "$PKG/DEBIAN/md5sums"   # for dpkg -V
chmod 644 "$PKG/DEBIAN/md5sums"
SIZE=$(du -sk --exclude=DEBIAN "$PKG" | cut -f1)
sed -e "s/@VERSION@/$VERSION/" -e "s/@ARCH@/$ARCH/" -e "s/@SIZE@/$SIZE/" "$SRC/packaging/deb/control.in" > "$PKG/DEBIAN/control"

echo "   checks: maintainer scripts, menu entry, service"
for f in "$PKG"/DEBIAN/postinst "$PKG"/DEBIAN/prerm "$PKG"/DEBIAN/postrm "$PKG"/opt/keephaven-guard-dog/bin/*; do
  sh -n "$f" || { echo "STOP: $f doesn't parse"; exit 1; }
done
for f in "$PKG"/usr/share/applications/*.desktop; do
  desktop-file-validate "$f" || { echo "STOP: $f isn't valid"; exit 1; }
done
for f in "$PKG"/usr/share/polkit-1/actions/*.policy; do
  xmllint --noout "$f" || { echo "STOP: $f isn't valid XML"; exit 1; }
  # each action runs exactly the script this package ships, by its installed path
  p=$(sed -n 's|.*"org.freedesktop.policykit.exec.path">\(.*\)</annotate>.*|\1|p' "$f")
  [ -n "$p" ] && [ -x "$PKG$p" ] || { echo "STOP: $f's exec.path ($p) isn't a script in this package"; exit 1; }
done
appstreamcli validate --no-net --pedantic "$PKG/usr/share/metainfo/co.keephaven.GuardDog.metainfo.xml" \
  || { echo "STOP: the AppStream file doesn't validate"; exit 1; }
grep -q -- "--require-password" "$PKG/usr/lib/systemd/system/keephaven-guard-dog.service" \
  || { echo "STOP: the packaged service doesn't ask for a password"; exit 1; }
# The unit's ExecStart isn't on this build machine yet (only inside the package tree): that one line is expected.
VERIFY=$(SYSTEMD_LOG_COLOR=0 systemd-analyze verify "$PKG/usr/lib/systemd/system/keephaven-guard-dog.service" 2>&1 \
         | grep -v "/opt/keephaven-guard-dog/venv/bin/keephaven-guard-dog" | grep -v "^$" || true)
[ -z "$VERIFY" ] || { echo "STOP: systemd-analyze verify:"; echo "$VERIFY"; exit 1; }
echo "   sh -n, desktop-file-validate, xmllint, appstreamcli validate, systemd-analyze verify: clean"

echo "== 6/7 build the .deb"
DEB="$OUT/keephaven-guard-dog_${VERSION}_${ARCH}.deb"
dpkg-deb --root-owner-group -Zxz --build "$PKG" "$DEB" >/dev/null
ls -l "$DEB"; sha256sum "$DEB"

echo "== 7/7 lintian"
lintian --no-tag-display-limit "$DEB" || true

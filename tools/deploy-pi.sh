#!/usr/bin/env bash
# Deploy one commit to an install.sh-edition machine. Stops before touching the running service if the commit doesn't
# install and import cleanly: staged install first, smoke test, then the real install, smoke test again, then restart.
#   tools/deploy-pi.sh <commit> <user@host>
set -euo pipefail
C="${1:?commit}"; SHA="$(git rev-parse --short "$C")"; H="${2:?ssh target, e.g. user@host}"
VENV='~/.local/share/keephaven-guard-dog-app'
SMOKE='import guard_dog, guard_dog.web, guard_dog.scanner, guard_dog.sweep, guard_dog.removal, guard_dog.optouts as o; print("imports ok, scripts:", sorted(o.SCRIPTS))'
echo "== $SHA: copy"
ssh $H "rm -rf /tmp/gd-src-$SHA /tmp/gd-stage-$SHA && mkdir -p /tmp/gd-src-$SHA"
git archive "$C" | ssh $H "tar -x -C /tmp/gd-src-$SHA"
echo "== staged install + smoke (the running service is untouched)"
ssh $H "$VENV/bin/pip install -q --no-deps --target /tmp/gd-stage-$SHA /tmp/gd-src-$SHA && ls /tmp/gd-stage-$SHA/guard_dog /tmp/gd-stage-$SHA/guard_dog/optouts /tmp/gd-stage-$SHA/guard_dog/data && cd / && $VENV/bin/python -I -c 'import sys; sys.path.insert(0, \"/tmp/gd-stage-$SHA\"); $SMOKE; import guard_dog; assert guard_dog.__file__.startswith(\"/tmp/gd-stage-$SHA\")'" \
  || { echo "STOP: $SHA doesn't install or import; the service was not touched"; exit 1; }
echo "== real install + smoke"
ssh $H "$VENV/bin/pip install -q /tmp/gd-src-$SHA && cd / && $VENV/bin/python -I -c '$SMOKE'" \
  || { echo "STOP: installed but doesn't import; NOT restarting (the old code is still running in memory)"; exit 1; }
echo "== restart"
ssh $H "systemctl --user restart keephaven-guard-dog && sleep 5 && systemctl --user show keephaven-guard-dog -p ActiveState -p SubState -p ActiveEnterTimestamp && rm -rf /tmp/gd-src-$SHA /tmp/gd-stage-$SHA"

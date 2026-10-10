#!/usr/bin/env bash
# One entrypoint that works on REWSL *and* on a fresh Claude Code cloud VM.
#
# WHY THIS EXISTS
# The Python in assets/lms/ was always portable -- every path goes through
# Path.home(), nothing hardcodes /home/ralph. What was NOT portable was the
# *invocation*: REWSL needs a specific venv plus an LD_LIBRARY_PATH for the
# unpacked Playwright libs, and that incantation lived only in prose inside the
# context-loader skill. So the same command could not be written once and run in
# both places. This script owns that difference; callers just run:
#
#     ./lms.sh load [--json]   THE study-session load: every source + mail +
#                              calendar, RECONCILED in one artifact (~30s). What
#                              Gigi runs. `scan` is the anytime deadline-only view.
#     ./lms.sh digest          full scan (Blackboard + Classroom + Facebook)
#     ./lms.sh sweep [COURSE]  EVERY gradebook column, attempt-level,
#                              no due-date filter -- sees due:null work
#                              the digest is blind to. --contents walks
#                              the content tree instead.
#     ./lms.sh outstanding     ONLY the unstarted work, already filtered --
#                              the finished answer to "what do I still have to do"
#     ./lms.sh chat [--space NAME] [--out DIR]
#                              Google Chat course spaces. PROGIT1 and ARTAPP1
#                              announce ONLY here — no other channel sees them.
#     ./lms.sh fb              Facebook only -- NEEDS NO CREDENTIAL
#     ./lms.sh login           restore the Blackboard SAML session
#     ./lms.sh check           is the saved profile still authenticated?
#     ./lms.sh sources         WHICH Blackboard session sources are live?
#                              (cdp = Ralph's real Comet browser, profile =
#                              the saved scraper profile). digest/sweep/cal all
#                              use whichever authenticates; --source=X pins one.
#     ./lms.sh prime           install Playwright + Chromium (cloud only)
#     ./lms.sh env             print which environment was detected
#     ./lms.sh classroom      Google Classroom via the OFFICIAL API (no browser)
#     ./lms.sh classroom-auth --url|--code X|--status|--export
#     ./lms.sh apply          reconcile AND WRITE the calendar with no agent in
#                             the loop. Dry run by default; --apply executes.
#     ./lms.sh cal-auth --url|--code X|--status|--init-client ID SECRET
#
# Extra args pass straight through:  ./lms.sh digest --no-fb
#
# 🔴 THE TRAP THIS SCRIPT WAS BUILT AROUND (found 2026-08-17 by testing)
# The first version decided "chromium is ready" by checking that the browser
# FILE existed on disk. It reported READY on a machine where chromium could not
# start at all -- `libnspr4.so` was missing, so every launch died with exit 127.
# Downloading a browser and being able to run one are different facts. The gate
# here is therefore an ACTUAL LAUNCH, never a file test, and prime() fails loud
# and names the missing libraries rather than returning a false green.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

RE_PY="$HOME/CRM/.venv/bin/python"
RE_LIBS="$HOME/pwlibs/_extracted/usr/lib/x86_64-linux-gnu"
CLOUD_VENV="$HOME/.venv-lms"
CLOUD_LIBS="$HOME/.pwlibs-cloud/_extracted/usr/lib/x86_64-linux-gnu"

# ---------------------------------------------------------------- environment
# REWSL is identified by its existing venv. Anything else is treated as a fresh
# VM that has to be primed. This is a positive test for RE rather than a test
# for "am I in the cloud", because there is no reliable marker for the latter
# and guessing wrong in that direction fails safe -- it just primes.
if [ -x "$RE_PY" ]; then
    ENVKIND=re
    PY="$RE_PY"
    [ -d "$RE_LIBS" ] && export LD_LIBRARY_PATH="$RE_LIBS:${LD_LIBRARY_PATH:-}"
else
    ENVKIND=cloud
    PY="$CLOUD_VENV/bin/python"
    # Set unconditionally: harmless when absent, and required once the no-root
    # fallback below has populated it.
    export LD_LIBRARY_PATH="$CLOUD_LIBS:${LD_LIBRARY_PATH:-}"
fi
PW="$(dirname "$PY")/playwright"

# ------------------------------------------------------------------ readiness
# THE gate. Actually starts a browser and closes it. Anything less is a lie --
# see the trap note at the top of this file.
#
# 🔴 It must launch THE SAME WAY browser.py does: launch_persistent_context with
# headless=False + --headless=new. 2026-09-14 (CLOUD) this probe used the plain
# p.chromium.launch(), which is headless=True, which runs a DIFFERENT BINARY --
# chrome-headless-shell. The cloud image ships headless_shell build 1234 but full
# chromium only as build 1194, so the gate passed on the shell while every real
# call died "Executable doesn't exist at .../chromium-1234/chrome-linux64/chrome".
# `./lms.sh env` printed "browser: launches OK" and the load still reported
# SESSION DEAD.
#
# This is the SAME trap as the old file-exists check, one level in: the thing you
# probe must be the thing you use. browser.py's own docstring already warned that
# headless=True is a different binary -- the gate just wasn't listening.
browser_launches() {
    [ -x "$PY" ] || return 1
    "$PY" - <<'EOF' >/dev/null 2>&1
import sys, tempfile
try:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=tempfile.mkdtemp(),
            headless=False,
            args=["--headless=new", "--no-sandbox"],
        )
        ctx.close()
except Exception:
    sys.exit(1)
EOF
}

missing_deps() {   # prints the missing system packages, empty if none
    [ -x "$PW" ] || return 0
    "$PW" install-deps --dry-run chromium 2>&1 \
        | sed -n 's/^  \([a-z0-9][a-z0-9.+-]*\)$/\1/p'
}

# -------------------------------------------- adopt a preinstalled chromium
# 🆕 2026-09-14. A Claude Code cloud image ships Chromium under
# $PLAYWRIGHT_BROWSERS_PATH (measured 2026-09-13: /opt/pw-browsers/chromium-1194),
# but the venv's Playwright expects a different build AND directory layout, and
# `playwright install` 403s on the default network level. The measured fix was to
# point the path Playwright asks for at the binary the image already has.
# Playwright names that exact path in its launch error, so it is read from there
# rather than hardcoding a build number that changes every release.
# 🔴 The gate is still browser_launches() — this only ever makes a launch possible.
adopt_preinstalled_chromium() {
    local base="${PLAYWRIGHT_BROWSERS_PATH:-/opt/pw-browsers}" have chrome want i
    have="$(ls -d "$base"/chromium-[0-9]* 2>/dev/null | sort -V | tail -1)"
    [ -n "$have" ] || return 1
    chrome="$(find "$have" -maxdepth 3 -type f -name chrome -perm -u+x 2>/dev/null | head -1)"
    [ -n "$chrome" ] || return 1
    # 🔴 This probe must use the REAL launch shape too (see browser_launches).
    # With the old headless=True probe it asked for the headless shell, which the
    # image already has, so it printed nothing, `want` came back empty and adopt
    # returned 1 -- while full chromium was still missing. 2026-09-14 (CLOUD).
    for i in 1 2 3; do      # the driver may ask for full chrome AND the headless shell
        browser_launches && return 0
        want="$("$PY" - <<'EOF' 2>&1 | sed -n "s/.*Executable doesn't exist at \([^ ]*\).*/\1/p" | head -1
import tempfile
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    p.chromium.launch_persistent_context(
        user_data_dir=tempfile.mkdtemp(),
        headless=False,
        args=["--headless=new", "--no-sandbox"],
    ).close()
EOF
)"
        [ -n "$want" ] || return 1
        # Link the DIRECTORY, not the bare binary: chromium loads icudtl.dat and
        # its .pak resources from the directory beside the executable, so a lone
        # symlinked file into an otherwise empty dir starts and then dies.
        if [ -d "$(dirname "$chrome")" ] && [ "$(basename "$want")" = "chrome" ]; then
            mkdir -p "$(dirname "$(dirname "$want")")"
            ln -sfn "$(dirname "$chrome")" "$(dirname "$want")" \
                && echo "prime: linked $(dirname "$want") -> $(dirname "$chrome")" >&2 \
                && continue
        fi
        mkdir -p "$(dirname "$want")" && ln -sfn "$chrome" "$want" || return 1
        echo "prime: linked $want -> $chrome" >&2
    done
    browser_launches
}

# ---------------------------------------------------------------------- prime
# Idempotent: safe to call before every run. On a warm VM (Anthropic snapshots
# the cloud filesystem for ~7 days) the launch probe passes and this returns in
# about a second, so callers may invoke it unconditionally.
prime() {
    if [ "$ENVKIND" = re ]; then
        if browser_launches; then
            echo "prime: REWSL already provisioned - nothing to do" >&2; return 0
        fi
        echo "prime: 🔴 REWSL chromium will NOT launch - do not 'fix' this here," >&2
        echo "       RE's libs live in ~/pwlibs and are managed by hand." >&2
        return 1
    fi

    if browser_launches; then
        echo "prime: already primed (browser launches) - skipping" >&2; return 0
    fi

    echo "prime: cold VM - installing Playwright + Chromium..." >&2
    if [ ! -x "$PY" ]; then
        python3 -m venv "$CLOUD_VENV" || { echo "prime: 🔴 could not create venv" >&2; return 1; }
    fi
    "$CLOUD_VENV/bin/pip" -q install --upgrade pip >/dev/null 2>&1
    "$CLOUD_VENV/bin/pip" -q install playwright \
        || { echo "prime: 🔴 pip install playwright failed" >&2; return 1; }
    # The image's own Chromium first: a ~1 s symlink instead of a download that the
    # default network level refuses. Falls through to the download if it can't.
    if adopt_preinstalled_chromium; then
        echo "prime: OK - adopted the image's preinstalled chromium" >&2; return 0
    fi
    "$PW" install chromium || { echo "prime: 🔴 chromium download failed" >&2; return 1; }

    browser_launches && { echo "prime: OK - browser launches" >&2; return 0; }

    # The browser is on disk but will not start: system libraries are missing.
    # Cloud sessions run as root, so the supported path works there.
    echo "prime: browser present but not launchable - installing system deps..." >&2
    "$PW" install-deps chromium >/dev/null 2>&1 || true
    browser_launches && { echo "prime: OK - browser launches after install-deps" >&2; return 0; }

    # No root (or apt refused). Mirror what REWSL does: download the .debs and
    # unpack them into a private tree, then point LD_LIBRARY_PATH at it. This
    # needs no privileges at all.
    local pkgs; pkgs="$(missing_deps)"
    if [ -z "$pkgs" ]; then
        echo "prime: 🔴 browser will not launch and no missing packages reported." >&2
        echo "       Run '$PW install-deps --dry-run chromium' by hand to see why." >&2
        return 1
    fi
    echo "prime: no root - extracting $(echo "$pkgs" | wc -w) .deb packages without privileges..."
    local work="$HOME/.pwlibs-cloud"
    mkdir -p "$work/_extracted" && ( cd "$work" || exit 1
        apt-get download $pkgs >/dev/null 2>&1 || true
        for d in *.deb; do [ -e "$d" ] && dpkg -x "$d" _extracted 2>/dev/null; done )

    if browser_launches; then
        echo "prime: OK - browser launches using extracted libs in $work"; return 0
    fi

    echo "prime: 🔴 FAILED - chromium still will not launch." >&2
    echo "       Missing system packages:" >&2
    echo "$pkgs" | sed 's/^/         /' >&2
    echo "       Facebook, Blackboard and Classroom scans all need a working browser." >&2
    echo "       Email + Calendar still work; treat LMS results as UNKNOWN, never 'nothing due'." >&2
    return 1
}


# ------------------------------------------------------------------- locking
# 🔴 Chromium allows ONE process per user-data-dir. Two lms.sh runs at once and
# the second dies with "Failed to create a ProcessSingleton", which bb_session
# reports as SESSION DEAD -- a flat lie, because the session is fine and the
# profile is merely busy. It also fires a pointless recovery.
#
# Measured 2026-08-23 by running two sweeps concurrently: run A succeeded, run B
# reported SESSION DEAD. The fallback agent hits this routinely because it runs
# `digest` and `sweep` back to back.
#
# So every profile-touching command serialises on one lock. Waiting is correct
# here; failing is not. Same pattern as the vault's Stop hook.
LOCKFILE="$HOME/.cache/ub-lms/profile.lock"
mkdir -p "$(dirname "$LOCKFILE")" 2>/dev/null
locked() {   # locked <python-script> [args...]
    local script="$1"; shift
    if command -v flock >/dev/null 2>&1; then
        exec flock -w 600 "$LOCKFILE" "$PY" "$script" "$@"
    fi
    exec "$PY" "$script" "$@"   # no flock available: behave as before
}

# ------------------------------------------------------------------ dispatch
cmd="${1:-digest}"; shift || true

case "$cmd" in
    env)
        deps="$(missing_deps | tr '\n' ' ' | sed 's/  *$//')"
        [ -z "$deps" ] && deps="none"
        echo "environment : $ENVKIND"
        echo "python      : $PY"
        # Blackboard freshness. The cron reads school MAIL and is structurally
        # blind to Blackboard (an instructor moving a due date sends no email --
        # exactly what happened 2026-08-19). So the age of the last real pull is
        # the only honest statement about whether the calendar can be trusted.
        "$PY" - <<'PYSTALE'
import json, time
from pathlib import Path
from datetime import datetime, timezone
f = Path.home() / ".local/share/ub-lms-lastverified.json"
if not f.exists():
    print("bb verified : 🔴 NEVER — treat every due date as UNVERIFIED")
else:
    d = json.loads(f.read_text())
    t = datetime.fromisoformat(d["last_verified"])
    hrs = (datetime.now(timezone.utc) - t).total_seconds() / 3600
    mark = "✅" if hrs < 6 else ("⚠" if hrs < 24 else "🔴")
    print(f"bb verified : {mark} {hrs:.1f}h ago ({d.get('by','?')})"
          + ("  — STALE, re-sweep before trusting the calendar" if hrs >= 24 else ""))
PYSTALE
        echo "browser     : $(browser_launches && echo 'launches OK  <-- THIS is the authoritative check' || echo 'WILL NOT LAUNCH')"
        # apt's opinion only. REWSL reports a long list here and still launches
        # fine, because its libs come from ~/pwlibs via LD_LIBRARY_PATH rather
        # than from dpkg. Never read this line as a verdict.
        echo "apt says missing: $deps"
        echo "bb profile  : $([ -d "$HOME/.local/share/ub-blackboard-profile" ] && echo present || echo 'ABSENT - Blackboard/Classroom unavailable')"
        echo "fb profile  : $([ -d "$HOME/.local/share/ub-fb-profile" ] && echo present || echo 'absent - will be created, no credential needed')"
        ;;
    prime)  prime ;;
    # Classroom via the official API needs NO browser, so it deliberately skips
    # prime() -- it is the one scan that works on a VM that failed to prime.
    classroom)      exec "$PY" classroom_api.py  "$@" ;;
    classroom-auth) exec "$PY" classroom_auth.py "$@" ;;
    # The browser fallback for Classroom, used while the API token is absent.
    # Asserts WHICH Google account it read before returning anything.
    classroom-scrape) prime || exit 1; locked classroom_scrape.py "$@" ;;
    digest) prime || exit 1; locked lms_digest.py "$@" ;;
    fb)     prime || exit 1; exec "$PY" fb_fetch.py   "$@" ;;
    # School inbox, filtered to what needs an action. The PERSONAL account is
    # not read here -- that half goes through the Gmail MCP connector, which
    # this process cannot reach. See the context-loader skill.
    warm)   prime || exit 1; locked warm_session.py "$@" ;;
    # No prime: mail_fetch prefers the Gmail API (no browser). Its scrape
    # fallback launches its own browser only if the API is unavailable, so
    # spending a Chromium test-launch here on every call was pure waste on the
    # common path. Keep the lock — the fallback still touches the profile.
    mail)   locked mail_fetch.py "$@" ;;
    # 🔴 WRITES. Dry run by default; nothing leaves without --send, and sending
    # stays opt-in per message no matter what the token is scoped for.
    send)   exec "$PY" mail_send.py "$@" ;;
    login)  prime || exit 1; locked sso_login.py "$@" ;;
    check)  prime || exit 1; locked bb_check.py "$@" ;;
    # Which session sources are actually authenticated. Gated on a real
    # /users/me 200 per source -- never on "a browser started" or "cookies
    # exist" (6 cookies + 401 was measured on 2026-08-22).
    bb-token) exec "$PY" bb_oauth.py "$@" ;;
    sources) prime || exit 1; locked bb_session.py "$@" ;;
    # Full attempt-level sweep with NO due-date filter. The digest only
    # shows a 14-day window, so it structurally cannot see the due:null
    # columns INTRCS1 files everything under. Run this -- not digest --
    # before answering "is there anything else outstanding?".
    sweep)  prime || exit 1; locked bb_sweep.py "$@" ;;
    chat-api) exec "$PY" chat_api.py "$@" ;;
    chat)   prime || exit 1; locked chat_fetch.py "$@" ;;
    # The FINISHED answer to "what do I still have to do" -- only the items with
    # no attempt, already filtered. Built because a stochastic model asked to
    # filter the full sweep dropped rows (4, then 5, then 6 across three runs of
    # the same prompt). Nothing here is left for a reader to decide.
    outstanding) prime || exit 1; locked bb_sweep.py --outstanding "$@" ;;
    # Reconcile Google Calendar against AUTHORITATIVE Blackboard due dates.
    # Prints an action list; does NOT write. See cal_sync.py header for why.
    cal)    prime || exit 1; locked cal_sync.py "$@" ;;
    # Same decisions as `cal`, but it fetches the calendar itself and WRITES
    # the result -- no agent, no MCP, no Claude usage. Dry run by default;
    # --apply executes. It cannot delete, and it cannot poll: the Blackboard
    # session needs a human every ~5h. See cal_apply.py header.
    apply)  prime || exit 1; locked cal_apply.py "$@" ;;
    # One-time OAuth for the calendar-events scope, on the PERSONAL account.
    # Separate token from classroom-auth: different account, different scope.
    cal-auth) exec "$PY" cal_apply.py "$@" ;;
    # 🔴 THE COMPLETE ANSWER to "what do I owe?". Reads gradebook AND
    # announcements AND the content tree AND Facebook, prints a per-course
    # coverage matrix, and EXITS NON-ZERO if any source is UNKNOWN. Findings
    # with no gradebook column are written to school/inbox.md as CANDIDATEs.
    # `outstanding` answers a narrower question and has under-reported twice.
    load)   prime || exit 1; locked study_load.py "$@" ;;
    scan)   prime || exit 1; locked scan.py "$@" ;;
    # Is the vault actually reaching the phone? The Stop hook fails OPEN, so a
    # rejected push leaves no signal anywhere. This is that missing signal.
    sync-health) exec "$PY" sync_health.py "$@" ;;
    # Paint the recurring class timetable graphite so it stops competing with
    # deadlines for attention. Idempotent; NOT locked -- it touches the calendar
    # API only, never the Blackboard browser profile.
    recolor-classes) exec "$PY" recolor_classes.py "$@" ;;
    # Offline regression tests -- no browser, no network, no session needed.
    # Guards the false-✅ bug (column status vs attempt status), the stale
    # due-date bug, and every calendar-reconcile decision. Run after ANY
    # change to how submission status, due dates, or calendar actions are decided.
    test-secrets) exec "$PY" test_no_secret_leak.py "$@" ;;
    # Blackboard session relay, RE → cloud, encrypted through the vault. RE's
    # warm timer exports; a cloud session imports via bb_session. See bb_relay.py.
    relay)  exec "$PY" bb_relay.py "$@" ;;
    # The one Google-token loader: --status (which source answers) and
    # --export classroom|calendar (one-line JSON for a cloud env var).
    creds)  exec "$PY" gcreds.py "$@" ;;
    test)   rc=0
            "$PY" test_classify_attempts.py "$@" || rc=1
            echo
            "$PY" test_authoritative_due.py "$@" || rc=1
            echo
            "$PY" test_cal_sync.py "$@" || rc=1
            echo
            "$PY" test_cal_apply.py "$@" || rc=1
            echo
            "$PY" test_session_recovery.py "$@" || rc=1
            echo
            "$PY" test_scan.py "$@" || rc=1
            echo
            "$PY" test_scan_sources.py "$@" || rc=1
            echo
            "$PY" test_mail_filter.py "$@" || rc=1
            echo
            "$PY" test_study_load.py "$@" || rc=1
            echo
            "$PY" test_gcreds.py "$@" || rc=1
            echo
            # 🔴 The only WRITE path in this package. Its rails are the reason
            # a bad send cannot happen quietly -- never drop it from the suite.
            "$PY" test_mail_send.py "$@" || rc=1
            echo
            "$PY" test_bb_relay.py "$@" || rc=1
            exit $rc ;;
    *) echo "usage: lms.sh {scan|load|sync-health|digest|sweep|outstanding|cal|apply|cal-auth|recolor-classes|fb|mail|send|warm|login|chat|chat-api|check|sources|bb-token|relay|creds|classroom|classroom-scrape|classroom-auth|test|test-secrets|prime|env} [args...]" >&2; exit 2 ;;
esac

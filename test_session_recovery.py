#!/usr/bin/env python3
"""Offline tests for the two guarantees that stop a scan being quietly incomplete.

No browser, no network, no session. Both properties here are about the SHAPE of a
failure, which is exactly the kind of thing that rots unnoticed: a scan that lies
looks identical to a scan that worked.

  1. AUTOMATIC RECOVERY (bb_session.open_session)
     - attempts the silent re-login AT MOST ONCE
     - `probe_all` never triggers it (it asks "what is live now", and would
       otherwise fire the recovery once per source)
     - a FAILED recovery still raises SessionDead — it can never invent a session

  2. NO SILENT DROPS (lms_digest)
     - a course that fails to read is NAMED and the run exits non-zero
     - it is never absorbed into "nothing with a due date in range"
"""
import io
import sys
from contextlib import redirect_stderr
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import bb_session

failed = 0
ran = 0


def check(name, ok, detail=""):
    global failed, ran
    ran += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        failed += 1
        if detail:
            print(f"        {detail}")


class Calls:
    """Counts re-login attempts and forces every source to fail."""

    def __init__(self, recover=False):
        self.n = 0
        self.recover = recover

    def relogin(self, show=None, pw=None):
        self.n += 1
        return (self.recover, "recovered (fake)" if self.recover else "google needs a human")


class FakeCtx:
    """Stands in for a browser context AND a page. Nothing here touches a real
    browser — these tests must run with no Chromium, no display and no network."""

    @property
    def context(self):
        return self

    def set_default_timeout(self, ms):
        pass

    def close(self):
        pass


def _fake_launch(pw, profile, show=None, viewport=None, extra_args=None):
    return FakeCtx(), FakeCtx()


def _stub(monkey):
    """Replace every outward call bb_session makes. Returns the originals."""
    orig = (bb_session._probe, bb_session._cdp_reachable,
            bb_session._relogin, bb_session.launch, bb_session.bb_cookies.inject)
    bb_session._cdp_reachable = lambda timeout=4: False       # no relay
    bb_session._probe = lambda page, source: (None, "/users/me -> 401 (not signed in)")
    bb_session._relogin = monkey.relogin
    bb_session.launch = _fake_launch
    bb_session.bb_cookies.inject = lambda ctx: "stubbed"
    return orig


def _restore(orig):
    (bb_session._probe, bb_session._cdp_reachable,
     bb_session._relogin, bb_session.launch, bb_session.bb_cookies.inject) = orig


def run(monkey, **kw):
    """Call open_session with every source stubbed dead. Returns SessionDead|None."""
    orig = _stub(monkey)
    try:
        with redirect_stderr(io.StringIO()):
            with bb_session.open_session(**kw):
                return None
    except bb_session.SessionDead as e:
        return e
    except Exception as e:
        return e
    finally:
        _restore(orig)


def main():
    global failed
    print("session recovery + silent-drop guards\n")

    # --- 1. recovery is attempted, exactly once ---------------------------
    m = Calls(recover=False)
    err = run(m, prefer="profile")
    check("a dead session attempts the silent re-login", m.n >= 1, f"n={m.n}")
    check("it attempts it AT MOST ONCE", m.n <= 1, f"n={m.n}")

    # 🔴 The one that matters. A recovery that does not recover must NOT be
    # allowed to turn into a reported success.
    check("a FAILED recovery still raises SessionDead",
          isinstance(err, bb_session.SessionDead), repr(err))
    check("the dead-session report still names the source",
          isinstance(err, bb_session.SessionDead) and "401" in err.report(),
          err.report() if isinstance(err, bb_session.SessionDead) else repr(err))
    check("the report says WHY recovery failed",
          isinstance(err, bb_session.SessionDead) and "human" in err.report(),
          err.report() if isinstance(err, bb_session.SessionDead) else repr(err))

    # 🔴 THE CASE THAT CAUGHT A VACUOUS ASSERTION (2026-08-23).
    #     The check above only exercised a recovery that FAILS — which breaks out
    #     of the loop before a second attempt is even possible, so "at most once"
    #     was true for the wrong reason and a mutant survived. The dangerous shape
    #     is a recovery that reports SUCCESS while the session still will not
    #     authenticate: that is the one that can spin.
    m = Calls(recover=True)
    err = run(m, prefer="profile")
    check("a SUCCESSFUL recovery that still fails to authenticate retries once only",
          m.n == 1, f"n={m.n}")
    check("...and still ends in SessionDead, never a loop",
          isinstance(err, bb_session.SessionDead), repr(err))

    # --- 2. opting out ----------------------------------------------------
    m = Calls(recover=False)
    run(m, prefer="profile", allow_relogin=False)
    check("allow_relogin=False never attempts a login", m.n == 0, f"n={m.n}")

    # probe_all asks what is live RIGHT NOW and opens one session per source;
    # recovering inside it would both change the answer and fire twice.
    m = Calls(recover=False)
    orig = _stub(m)
    try:
        with redirect_stderr(io.StringIO()):
            bb_session.probe_all()
    except Exception:
        pass
    finally:
        _restore(orig)
    check("probe_all NEVER triggers a re-login", m.n == 0, f"n={m.n}")

    # --- 3. the digest cannot silently drop a course ----------------------
    src = (Path(__file__).parent / "lms_digest.py").read_text()
    check("digest is on the shared session layer",
          "open_session()" in src and "from bb_session import" in src)
    check("a failed course read is RECORDED, not skipped",
          'unreadable.append(f"{cid} (course fetch' in src, "bare `continue` is back")
    check("failed gradebook columns are recorded too",
          'unreadable.append(f"{code} columns' in src)
    check("failed announcements are recorded too",
          'unreadable.append(f"{code} announcements' in src)
    check("an unreadable course is printed under a loud header",
          "NOT FULLY READ" in src)
    check("a total course-list failure exits non-zero",
          "COURSE LIST UNREADABLE" in src and "return 2" in src)

    # --- 4. concurrent runs must not fake a dead session -------------------
    # 🔴 Chromium allows ONE process per user-data-dir. Two lms.sh runs at once
    # and the second dies with "Failed to create a ProcessSingleton", which
    # bb_session reports as SESSION DEAD — a lie: the session is fine, the
    # profile is busy. Reproduced 2026-08-23 by running two sweeps together
    # (run A clean, run B "SESSION DEAD"), and it is routine for the fallback
    # agent, which runs digest and sweep back to back.
    sh = (Path(__file__).parent / "lms.sh").read_text()
    check("profile commands serialise on a lock", "flock -w" in sh)
    for cmd in ("lms_digest.py", "bb_sweep.py", "cal_apply.py", "cal_sync.py",
                "bb_check.py", "bb_session.py", "sso_login.py"):
        check(f"  {cmd} runs under the lock", f"locked {cmd}" in sh)
    # fb drives a SEPARATE profile (ub-fb-profile) and cannot collide, so
    # locking it would serialise it against the LMS runs for nothing.
    check("fb is deliberately NOT locked (separate profile)",
          'exec "$PY" fb_fetch.py' in sh)

    # --- 5. the finished-answer command ------------------------------------
    # 🔴 A stochastic model asked to FILTER a long list drops rows. Three runs of
    # one prompt over the same sweep returned 4, then 5, then 6 items, dropping a
    # 30-pt UNSUBMITTED task twice. Filtering therefore lives in Python and the
    # model repeats a finished list.
    sw = (Path(__file__).parent / "bb_sweep.py").read_text()
    check("sweep has an --outstanding mode", "outstanding_only" in sw)
    check("outstanding prints a COUNT the reader can check against",
          "OUTSTANDING ITEM(S)" in sw)
    check("no-due-date work is called STILL OWED, never optional",
          "NO DUE DATE SET" in sw and "still owed" in sw)
    check("an empty result is stated positively, not left blank",
          "0 outstanding items" in sw)
    check("outstanding still reports unreadable courses as UNKNOWN",
          "NOT FULLY READ" in sw)
    check("lms.sh exposes it", "outstanding) prime" in sh)
    # 🔴 The model must not have to compare dates to a clock. ox alpha did, on
    # 2026-08-23, and reported "2 overdue" when 3 items were past due —
    # under-flagging a 20-pt lab. Its own detail line said 3. The count is now
    # computed here.
    check("outstanding computes OVERDUE itself", "OVERDUE." in sw and "n_over" in sw)
    check("overdue items sort by REAL date, not the formatted string",
          "u[6]" in sw, "sorting the display string is alphabetical, not chronological")
    check("late-work policy is explicitly NOT claimed",
          "Whether late work is still accepted is NOT in this output" in sw)

    # NEGATIVE CONTROL: prove these string guards can actually fail, so a future
    # rename cannot leave them vacuously passing.
    if "this string is definitely not in lms_digest" in src:
        print("  FAIL  negative control: source guard matches anything")
        failed += 1
    else:
        print("  PASS  negative control: source guard rejects a bogus string")

    print(f"\n{'🔴 ' + str(failed) + ' FAILED' if failed else '✅ ALL PASS'}"
          f"  ({ran} assertions + 1 control)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

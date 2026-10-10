#!/usr/bin/env python3
"""Is the vault actually reaching the phone? Answer it with evidence, not hope.

    ./lms.sh sync-health

🔴 WHY THIS EXISTS
The vault auto-commits and pushes from a Claude Code Stop hook that **fails open
by design**:

    flock -w 25 9 || exit 0
    git pull --rebase --autostash -q && git push -q || git rebase --abort

Both `|| exit 0` and `|| git rebase --abort` swallow the failure. A rejected
push, a lock held by another surface, or a rebase conflict all end the same way:
exit 0, no message, nothing written anywhere. The vault simply stops reaching the
phone and NOTHING says so. Ralph reported "the vault on my phone is out of sync
again" with no error to point at -- that silence is the bug.

This command looks at the repo and reports what is actually true.
"""
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

VAULT = Path(__file__).resolve().parents[3]   # …/vault (assets/lms -> school -> vault)
MNL = timezone(timedelta(hours=8))


def git(*a, cwd=VAULT):
    try:
        r = subprocess.run(("git",) + a, cwd=cwd, capture_output=True,
                           text=True, timeout=60)
        return r.stdout.strip(), r.returncode
    except Exception as e:
        return f"{type(e).__name__}: {e}", 1


def stranded_branches(fetch=True):
    """[(remote_branch, commits_not_in_main)] for every origin branch ahead of main.

    Shared with study_load (2026-09-14) so stranded work sinks the STUDY LOAD,
    not just this rarely-run report: the 2026-09-13 cloud session's 3 commits
    sat on a claude/* branch through an entire RE load before being found by hand.
    """
    if fetch:
        git("fetch", "-q", "--prune", "origin")
    refs, _ = git("for-each-ref", "--format=%(refname:short)", "refs/remotes/origin")
    out = []
    for b in refs.splitlines():
        if b in ("origin/main", "origin/HEAD", "origin") or "->" in b:
            continue
        n, _ = git("rev-list", "--count", f"origin/main..{b}")
        if n.isdigit() and int(n) > 0:
            out.append((b, int(n)))
    return out


def main():
    print(f"# vault sync health — {datetime.now(MNL):%a %d %b %Y %I:%M %p} (Manila)")
    print(f"  repo: {VAULT}")
    problems = []

    remote, _ = git("remote", "get-url", "origin")
    branch, _ = git("rev-parse", "--abbrev-ref", "HEAD")
    print(f"  remote: {remote}")
    print(f"  branch: {branch}")
    if branch != "main":
        problems.append(f"HEAD is on {branch!r}, not main — the phone tracks main, "
                        "so nothing committed here will reach it")

    # Talk to the remote. Without this every count below is stale.
    _, rc = git("fetch", "--quiet", "origin")
    if rc != 0:
        problems.append("git fetch FAILED — cannot reach origin. Every count below "
                        "is from the last successful fetch and may be wrong.")

    ahead, _ = git("rev-list", "--count", "@{u}..HEAD")
    behind, _ = git("rev-list", "--count", "HEAD..@{u}")
    print(f"\n  ahead of origin : {ahead}")
    print(f"  behind origin   : {behind}")
    if ahead.isdigit() and int(ahead) > 0:
        problems.append(f"{ahead} commit(s) NEVER PUSHED. The phone cannot see them. "
                        "This is what a silently-failed Stop hook looks like.")
    if behind.isdigit() and int(behind) > 0:
        problems.append(f"{behind} commit(s) on origin are not here — another surface "
                        "(cloud session, phone, OpenCode) pushed and this clone has not pulled.")

    dirty, _ = git("status", "--porcelain")
    n_dirty = len([l for l in dirty.splitlines() if l.strip()])
    print(f"  uncommitted     : {n_dirty} file(s)")
    if n_dirty:
        problems.append(f"{n_dirty} uncommitted file(s) — not on the phone until a "
                        "commit AND a push both succeed.")

    last, _ = git("log", "-1", "--format=%cI|%s")
    if "|" in last:
        iso, subject = last.split("|", 1)
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(iso)
            hrs = age.total_seconds() / 3600
            print(f"  last commit     : {hrs:.1f} h ago — {subject[:60]}")
            if hrs > 24:
                problems.append(f"last commit was {hrs:.0f} h ago; if you have been "
                                "working, the hook may not be running at all.")
        except Exception:
            print(f"  last commit     : {subject[:60]}")

    # 🔴 A cloud session that commits onto its own branch reaches NOBODY. Two
    # such branches already exist (claude/study-session-*), and the phone tracks
    # main, so anything stranded there is invisible everywhere that matters.
    stray, _ = git("for-each-ref", "--format=%(refname:short)", "refs/remotes/origin")
    strays = [b for b in stray.splitlines()
              if b not in ("origin/main", "origin/HEAD") and "->" not in b]
    if strays:
        print(f"\n  other remote branches: {len(strays)}")
        for b in strays:
            n, _ = git("rev-list", "--count", f"origin/main..{b}")
            mark = "🔴" if (n.isdigit() and int(n) > 0) else "  "
            print(f"    {mark} {b}  ({n} commit(s) not in main)")
            if n.isdigit() and int(n) > 0:
                problems.append(f"{b} holds {n} commit(s) that never reached main — "
                                "a cloud session's work, invisible to the phone.")

    size, _ = git("count-objects", "-vH")
    for line in size.splitlines():
        if line.startswith("size-pack:"):
            print(f"\n  {line}  (every phone clone pays this once)")

    print()
    if problems:
        print(f"🔴 {len(problems)} PROBLEM(S) — the phone is NOT necessarily current:")
        for p in problems:
            print(f"  · {p}")
        print("\n  Usual fix:  cd ~/vault && git add -A && git commit -m sync && "
              "git pull --rebase && git push")
        return 1
    print("✅ In sync with origin/main, nothing uncommitted, no stranded branches.")
    print("   If the phone still looks stale, the problem is on the phone: open")
    print("   Obsidian and pull manually (obsidian-git does not always auto-pull).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

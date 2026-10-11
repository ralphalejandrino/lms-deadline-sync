#!/usr/bin/env python3
"""Prove the school credentials have not leaked into the vault.

    ./lms.sh test-secrets

## Why this exists

The vault auto-commits and pushes to GitHub from a Stop hook at the end of every
turn. A secret written anywhere inside `~/vault` is therefore a
PUBLISHED secret within one turn, and `git` keeps it after the file is deleted.
So the credentials live at `~/.config/ub-school/credentials.json`, outside the
vault, exactly like `~/.config/ub-calendar/` — and this test is the standing
proof that they stayed there.

It checks three things, in increasing order of how badly you want them true:

  1. the credential file's permissions are 0600 and it is outside the vault
  2. no secret VALUE appears anywhere in the vault working tree
  3. no secret VALUE appears anywhere in the vault's git history

## 🔴 The controls matter more than the checks

A grep that finds nothing is indistinguishable from a grep that cannot find
anything — a wrong path, a bad flag, an exception swallowed. So each search is
paired with a positive control: a planted string the search MUST find. If the
control fails, the result is reported as INCONCLUSIVE rather than clean, because
"I searched and found no secrets" is a claim this file has to actually earn.

Skips cleanly (exit 0) when no credential file exists yet, and says it skipped.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

CREDS = Path.home() / ".config/ub-school/credentials.json"
VAULT = Path(__file__).resolve().parents[3]

# Which keys hold something that must never reach the vault. A username or a
# student number is an identifier, not a secret, and both appear in the vault
# legitimately -- see the note in main().
SECRET_KEY = re.compile(r"(password|passwd|secret|token|api[_-]?key|credential)",
                        re.I)

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


def grep_tree(needle):
    """True if `needle` appears anywhere in the vault working tree."""
    r = subprocess.run(["grep", "-rIFq", "--", needle, str(VAULT)],
                       capture_output=True)
    return r.returncode == 0


def grep_history(needle):
    """True if `needle` appears anywhere in the vault's committed history."""
    r = subprocess.run(["git", "-C", str(VAULT), "log", "-S", needle,
                        "--oneline", "--all"], capture_output=True, text=True)
    return bool(r.stdout.strip())


def main():
    print(f"# secret-leak guard — vault {VAULT}")

    if not CREDS.exists():
        print(f"\n  SKIPPED — no credential file at {CREDS}")
        print("  Nothing to leak yet. This is not a pass; re-run once it exists.")
        return 0

    # ---- 1. the file itself -------------------------------------------------
    mode = oct(CREDS.stat().st_mode & 0o777)
    check(f"credentials are 0600 (got {mode})", mode == "0o600")
    check("credentials live OUTSIDE the vault",
          VAULT not in CREDS.resolve().parents,
          f"{CREDS} is inside {VAULT}")

    # 🔴 Only SECRET fields. The first version searched every value in the file
    # and flagged `bb_user_id` — Ralph's student number, which appears all over
    # the vault and its history by design. A guard that cries leak on a
    # non-secret is a guard that gets ignored, which is the same as no guard.
    try:
        blob = json.loads(CREDS.read_text())
        secrets = [str(v) for k, v in blob.items()
                   if isinstance(v, str) and len(v) >= 6
                   and SECRET_KEY.search(k)]
        skipped = [k for k in blob if not SECRET_KEY.search(k)]
        if skipped:
            print(f"  (not secrets, deliberately not searched: {', '.join(skipped)})")
    except Exception as e:
        check("credential file parses as JSON", False, repr(e))
        return 1
    check("at least one secret value found to search for", bool(secrets))

    # ---- 2. positive controls ----------------------------------------------
    # Plant a string in the vault, prove the tree search finds it, remove it.
    canary = "CANARY-9f2b7c1e-do-not-commit"
    tmp = VAULT / ".secret-leak-canary.tmp"
    try:
        tmp.write_text(canary + "\n")
        tree_control = grep_tree(canary)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
    check("CONTROL: tree search can find a planted string", tree_control,
          "the working-tree grep is broken — a clean result here means nothing")
    check("CONTROL: the canary file was removed again", not tmp.exists())

    # A string certain to be in this repo's history, proving -S actually works.
    hist_control = grep_history("blackboard")
    check("CONTROL: history search can find a known string", hist_control,
          "the git -S search is broken — a clean result here means nothing")

    if not (tree_control and hist_control):
        print("\n🔴 INCONCLUSIVE — a control failed, so the clean results below "
              "are not evidence of anything.")
        return 1

    # ---- 3. the actual checks ----------------------------------------------
    for s in secrets:
        check(f"secret (len {len(s)}) is NOT in the vault working tree",
              not grep_tree(s),
              "🔴 A CREDENTIAL IS IN THE VAULT. It pushes to GitHub every turn.")
        check(f"secret (len {len(s)}) is NOT in the vault git history",
              not grep_history(s),
              "🔴 A CREDENTIAL IS IN GIT HISTORY. Deleting the file will not "
              "remove it — the history must be rewritten and the password rotated.")

    print(f"\n{ran - failed}/{ran} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

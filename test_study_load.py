#!/usr/bin/env python3
"""Offline tests for the consolidated load — reconciliation + stale self-correction.

No network. Stubs `scan.gather`, mail, and calendar so the RECONCILIATION logic
is what's under test — the part that moved out of Gigi's LLM into Python. The
assertion that matters: a dated item on no board and no calendar MUST surface in
board_diff, and one already tracked MUST NOT. That is the row-dropping failure
the whole directory exists to prevent, now that a model no longer does it.

Run: ./lms.sh test, or python3 test_study_load.py
"""
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import study_load  # noqa: E402
import scan        # noqa: E402

# build() now checks the real vault for stranded branches (2026-09-14). A unit
# test must never shell out to git or the network; the gate tests below swap in
# their own fakes where the check itself is under test.
study_load._stranded = lambda: []

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


def main():
    print("== reconciliation matchers ==")
    board = "- [ ] MATHMW1 — Nature Pattern Hunt (100 pts)\n- [ ] HUMCOM1 empathy map"
    check("exact-ish title on the board -> matched",
          study_load._on_board("Nature Pattern Hunt", board))
    check("distinctive words match even if not verbatim",
          study_load._on_board("Natures Pattern Hunt activity", board))
    check("unrelated title -> NOT matched",
          not study_load._on_board("SOCORN2 Orientation Summary", board))
    check("empty title -> NOT matched (must not match everything)",
          not study_load._on_board("", board))
    check("one shared common word is not enough",
          not study_load._on_board("the map", "empathy diagram"),
          "'map' alone shouldn't match; needs 2 distinctive tokens")
    check("calendar summary match works the same way",
          study_load._on_calendar("Quiz Software Licensing",
                                   ["📌 DUE: INTRCS1 — Quiz: Software and Software Licensing"]))

    print("\n== board_diff: the row-drop guard ==")
    # Stub the whole gather + mail + calendar so only reconciliation runs.
    scan.gather = lambda since, no_fb=False, on_status=None: {
        "items": [
            {"course": "INTRCS1", "title": "Laboratory Activity_1", "due": "2026-08-27T03:00:00Z",
             "submitted": False, "state": "no attempt"},
            {"course": "MATHMW1", "title": "Nature Pattern Hunt", "due": "2026-08-25T15:59:00Z",
             "submitted": False, "state": "no attempt"},
            {"course": "HUMCOM1", "title": "Done Quiz", "due": "2026-08-20T00:00:00Z",
             "submitted": True, "state": "TURNED_IN"},
        ],
        "candidates": [], "coverage": {"INTRCS1": {s: scan.OK for s in scan.SOURCES}},
        "notes": [], "courses": [], "source": "profile",
        "submitted": [{"course": "NSTPRO1", "title": "Quiz 1", "state": "TURNED_IN"}]}
    m = types.ModuleType("mail_fetch"); m.fetch_api = lambda: ([], None)
    sys.modules["mail_fetch"] = m
    c = types.ModuleType("cal_apply")
    c.fetch_events = lambda: [
        {"summary": "📌 DUE: MATHMW1 — Nature Pattern Hunt", "start": "2026-08-25T15:59:00+08:00"}]
    sys.modules["cal_apply"] = c
    # tasks.md that lists the lab but NOT the pattern hunt
    study_load.TASKS = Path("/tmp/_tl_tasks.md")
    study_load.TASKS.write_text("- [ ] INTRCS1 Laboratory Activity_1 (20 pts) overdue\n")

    art, dead = study_load.build(since=None, no_fb=True)
    titles = [b["title"] for b in art["board_diff"]]
    check("item on the board is NOT in board_diff (Lab Activity_1)",
          "Laboratory Activity_1" not in titles, f"diff={titles}")
    check("item on the CALENDAR but not board is NOT in board_diff (Pattern Hunt)",
          "Nature Pattern Hunt" not in titles,
          "it's on the calendar stub, so it's tracked")
    check("a SUBMITTED item never reaches board_diff",
          "Done Quiz" not in titles)
    # The counter-signal: submitted work is surfaced so a mail/announcement
    # mention can be reconciled as done, not re-raised (the NSTPRO1 false alarm).
    sub_titles = [s["title"] for s in art.get("submitted", [])]
    check("submitted work is carried in the artifact (counter-signal)",
          "Quiz 1" in sub_titles, f"submitted={sub_titles}")

    # Now remove it from BOTH board and calendar -> it must surface.
    c.fetch_events = lambda: []
    study_load.TASKS.write_text("- [ ] something unrelated\n")
    art2, _ = study_load.build(since=None, no_fb=True)
    t2 = [b["title"] for b in art2["board_diff"]]
    check("dated item on NO board and NO calendar -> surfaces in board_diff",
          "Nature Pattern Hunt" in t2 and "Laboratory Activity_1" in t2, f"diff={t2}")
    # 🗂 2026-09-27 (CC-APP) reorg: a CLOSED item moved to archive/tasks-history by
    # scripts/prune-state.py is still "on the board" — it must not resurface.
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="_tl_hist_"))
    (tmp / "archive" / "tasks-history").mkdir(parents=True)
    (tmp / "archive" / "tasks-history" / "2026-09.md").write_text(
        "- [x] INTRCS1 Laboratory Activity_1 (20 pts) submitted\n")
    live = study_load.TASKS
    study_load.TASKS = tmp / "tasks.md"
    study_load.TASKS.write_text("- [ ] something unrelated\n")
    art3, _ = study_load.build(since=None, no_fb=True)
    t3 = [b["title"] for b in art3["board_diff"]]
    check("item moved to archive/tasks-history is NOT in board_diff",
          "Laboratory Activity_1" not in t3 and "Nature Pattern Hunt" in t3, f"diff={t3}")
    import shutil
    shutil.rmtree(tmp)
    study_load.TASKS = live

    try:
        study_load.TASKS.unlink()
    except FileNotFoundError:
        pass

    print("\n== ledger persistence guard ==")
    # build() without persist MUST NOT write the shared ledger — otherwise every
    # test run (which stubs gather) would clobber the real watermark. main()
    # passes persist=True; tests never do.
    led = scan.LEDGER
    before = led.read_text() if led.exists() else None
    study_load.TASKS = Path("/tmp/_tl_tasks2.md"); study_load.TASKS.write_text("x\n")
    study_load.build(since=None, no_fb=True)              # persist defaults False
    after = led.read_text() if led.exists() else None
    check("build(persist=False) leaves the ledger untouched", before == after)
    try:
        study_load.TASKS.unlink()
    except FileNotFoundError:
        pass

    print("\n== the completeness gate ==")
    # 🔴 The bug this section exists for (2026-09-02): the gate read ONLY
    # `unknown_cells`, which comes from the per-course coverage matrix. The
    # calendar and school mail are fetched OUTSIDE that matrix, so when the
    # calendar OAuth token expired on ~08-29 every load returned `calendar: null`
    # AND EXITED 0 for four days. A source that can fail without moving the exit
    # code is not being checked at all.
    healthy_gather = lambda since, no_fb=False, on_status=None: {
        "items": [], "candidates": [],
        "coverage": {"INTRCS1": {s: scan.OK for s in scan.SOURCES}},
        "notes": [], "courses": [], "source": "profile", "submitted": []}
    scan.gather = healthy_gather
    study_load.TASKS = Path("/tmp/_tl_tasks3.md")
    study_load.TASKS.write_text("x\n")

    def build_with(cal_fn, mail_fn):
        c2 = types.ModuleType("cal_apply"); c2.fetch_events = cal_fn
        sys.modules["cal_apply"] = c2
        m2 = types.ModuleType("mail_fetch"); m2.fetch_api = mail_fn
        sys.modules["mail_fetch"] = m2
        return study_load.build(since=None, no_fb=True)

    def boom():
        raise RuntimeError("RefreshError invalid_grant")

    # --- NEGATIVE CONTROL, first and most important ---------------------------
    # Without this, a gate hardcoded to `return 1` would pass every other check
    # in this section. A test whose failure path and clean path look the same is
    # not a test.
    art, dead = build_with(lambda: [], lambda: ([], None))
    check("NEGATIVE CONTROL — all sources healthy -> complete, exit 0",
          art["complete"] and art["degraded"] == []
          and study_load.exit_code(art, dead) == 0,
          f"complete={art['complete']} degraded={art['degraded']} "
          f"exit={study_load.exit_code(art, dead)}")

    # --- the calendar failure that actually happened ---------------------------
    art, dead = build_with(boom, lambda: ([], None))
    check("dead calendar -> NOT complete, exit 1 (the 4-day silent failure)",
          not art["complete"] and study_load.exit_code(art, dead) == 1,
          f"complete={art['complete']} exit={study_load.exit_code(art, dead)}")
    check("dead calendar names itself in `degraded`",
          any("calendar" in d for d in art["degraded"]), f"degraded={art['degraded']}")
    check("counts.degraded agrees with the list",
          art["counts"]["degraded"] == len(art["degraded"]))

    # --- the same hole, mail side ----------------------------------------------
    art, dead = build_with(lambda: [], lambda: (None, "no token at /x"))
    check("dead school mail -> NOT complete, exit 1",
          not art["complete"] and study_load.exit_code(art, dead) == 1)
    check("dead school mail names itself in `degraded`",
          any("mail" in d for d in art["degraded"]), f"degraded={art['degraded']}")

    # --- 2026-09-27: the load must agree with the calendar ---------------------
    # PROGIT1 Tracing Activity (hand-marked ✅ DONE on the calendar) and the
    # INTRCS1 L&D twin were listed OVERDUE by the load while cal_sync held them.
    past = "2026-09-15T10:00:00.000Z"

    def gi(col, title, submitted=False, course="INTRCS1", cid="_9005_1", pts=20.0):
        return {"course": course, "cid": cid, "column": col, "title": title,
                "due": past, "pts": pts, "attempts": ["Completed"] if submitted else [],
                "submitted": submitted, "state": "x", "source": "gradebook"}

    ld = "2627A_Learning and Discovery Report - To be pass every before exam"
    items6 = [gi("_9001_1", ld, submitted=True), gi("_9002_1", ld),
              gi("_9009_1", "M Lec FA 1 (Tracing)", course="PROGIT1", cid="_4_1"),
              gi("_9010_1", "M Lab Quiz 1", course="PROGIT1", cid="_4_1", pts=40.0)]
    scan.gather = lambda since, no_fb=False, on_status=None: dict(
        healthy_gather(since), items=items6)
    done_ev = {"id": "e1", "summary": "✅ DONE: PROGIT1 — Tracing Activity",
               "description": "bb:_4_1:_9009_1", "start": past}
    art, _ = build_with(lambda: [done_ev], lambda: ([], None))
    ov = [i["title"] for i in art["overdue"]]
    check("held ✅ DONE item leaves `overdue`...", "M Lec FA 1 (Tracing)" not in ov, str(ov))
    check("...and is LISTED under held_by_you, not dropped",
          [h["title"] for h in art["held_by_you"]] == ["M Lec FA 1 (Tracing)"],
          str(art["held_by_you"]))
    check("twin leaves `overdue` and is listed under twins, naming its sibling",
          ld not in ov and len(art["twins"]) == 1 and "_9001_1" in art["twins"][0]["why"],
          str(art["twins"]))
    check("control: genuinely missed work STAYS overdue", ov == ["M Lab Quiz 1"], str(ov))
    check("counts agree with the lists",
          art["counts"]["overdue"] == 1 and art["counts"]["held_by_you"] == 1
          and art["counts"]["twins"] == 1, str(art["counts"]))
    art, _ = build_with(boom, lambda: ([], None))
    check("dead calendar -> nothing is held (fails SAFE: stays overdue)",
          "M Lec FA 1 (Tracing)" in [i["title"] for i in art["overdue"]]
          and not art["complete"], str(art["overdue"]))
    scan.gather = healthy_gather

    # --- stranded vault work (2026-09-14) --------------------------------------
    study_load._stranded = lambda: [("origin/claude/study-session-x", 3)]
    art, dead = build_with(lambda: [], lambda: ([], None))
    check("stranded branch -> NOT complete, exit 1",
          not art["complete"] and study_load.exit_code(art, dead) == 1,
          f"complete={art['complete']} degraded={art['degraded']}")
    check("stranded branch names itself in `degraded`",
          any("claude/study-session-x" in d for d in art["degraded"]), f"{art['degraded']}")

    def _git_boom():
        raise OSError("git not available")
    study_load._stranded = _git_boom
    art, dead = build_with(lambda: [], lambda: ([], None))
    check("a branch check that CANNOT RUN is not silently clean",
          not art["complete"], f"degraded={art['degraded']}")
    study_load._stranded = lambda: []

    # --- the ORIGINAL gate must still work -------------------------------------
    scan.gather = lambda since, no_fb=False, on_status=None: {
        "items": [], "candidates": [],
        "coverage": {"INTRCS1": {s: scan.UNKNOWN for s in scan.SOURCES}},
        "notes": [], "courses": [], "source": "profile", "submitted": []}
    art, dead = build_with(lambda: [], lambda: ([], None))
    check("coverage UNKNOWN still trips the gate even with calendar+mail healthy",
          not art["complete"] and study_load.exit_code(art, dead) == 1,
          "the pre-existing unknown_cells behaviour must be preserved")
    scan.gather = healthy_gather

    # --- session dead outranks everything --------------------------------------
    check("dead session -> exit 2, not 1",
          study_load.exit_code({"complete": False}, True) == 2)
    check("a caller that ignores `complete` cannot get 0 by accident",
          study_load.exit_code({}, False) == 1,
          "a missing `complete` key must read as INCOMPLETE, never clean")
    try:
        study_load.TASKS.unlink()
    except FileNotFoundError:
        pass

    print("\n== stale-course self-correction (classroom_api) ==")
    import classroom_api as ca

    class _Svc:
        def __init__(self, when): self._w = when
        def courses(self): return self
        def courseWork(self):
            return types.SimpleNamespace(list=lambda **k: types.SimpleNamespace(
                execute=lambda: {"courseWork": [{"title": "Old Thing", "updateTime": self._w}]}))
        def announcements(self):
            return types.SimpleNamespace(list=lambda **k: types.SimpleNamespace(
                execute=lambda: {"announcements": []}))

    check("stale course with NEWER activity -> flagged",
          ca._has_activity_since(_Svc("2026-09-01T00:00:00Z"), "cid", "2026-08-29")
          is not None)
    check("stale course with OLDER activity -> silent",
          ca._has_activity_since(_Svc("2026-08-01T00:00:00Z"), "cid", "2026-08-29")
          is None)

    print(f"\n{ran - failed}/{ran} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

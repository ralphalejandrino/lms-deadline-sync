# lms-deadline-sync

Reads every channel my university uses to hand out work (Blackboard Learn, Google Classroom,
Google Chat course spaces, the school mailbox and the student-body Facebook pages), reconciles
every dated deadline into **one** artifact, and keeps Google Calendar in sync with it.

I built it because deadlines at my university are scattered across five channels. Instructors
rename and re-date gradebook columns in place, and some announce exams only in a group chat. A
missed item usually wasn't hidden. It was in a channel I hadn't checked that day.

## The rules it is built around

- **A failed pull is `UNKNOWN`, never "nothing due".** An empty result and an unreadable source
  look identical to a reader, so every source reports a coverage cell, and the load exits non-zero
  while any cell is unknown or a source (calendar, mail) is degraded.
- **Reconciliation lives in code, not in a reader's judgement.** `study_load.py` merges every source
  into one JSON artifact: overdue, upcoming, already-submitted, board diff, coverage. Nothing
  downstream re-derives it.
- **Calendar events are matched by a stable marker, not by title.** Each event carries a
  `bb:<courseId>:<columnId>` marker, because instructors rename columns in place.
- **Deletes are proposed, never executed.** A due date that disappears looks the same as an item
  that was removed, so `apply` prints `PROPOSE_DELETE` and leaves it for a person.
- **A human's call is never overwritten.** An event a person marked done stays done (`HELD`), even
  when the gradebook disagrees.

## Layout

| File | Job |
|---|---|
| `lms.sh` | the one entrypoint: `load`, `scan`, `sweep`, `apply`, `mail`, `chat`, `fb`, `test`, … |
| `study_load.py` | runs every source and reconciles them into one artifact |
| `scan.py`, `bb_sweep.py`, `bb_fetch.py` | Blackboard gradebook, announcements and content tree |
| `bb_session.py`, `bb_cookies.py`, `warm_session.py`, `sso_login.py` | keeping a Google-SAML Blackboard session alive |
| `bb_relay.py` | encrypted session hand-off to a cloud machine (key never in the repo) |
| `classroom_api.py`, `chat_api.py`, `mail_fetch.py` | Google Classroom, Chat and Gmail through the official APIs |
| `fb_fetch.py` | public Facebook pages |
| `cal_sync.py`, `cal_apply.py` | the calendar diff and its writer (CREATE / UPDATE / ADOPT / HELD / TWIN) |
| `mail_send.py` | sending as the school account, dry-run by default |
| `test_*.py` | 14 test files, including a check that no secret can be written into the repo |

## Running it

```bash
./lms.sh test               # the whole suite
./lms.sh load --json        # every source, reconciled; exit 1 = incomplete, 2 = session dead
./lms.sh apply              # calendar dry run
./lms.sh apply --apply      # write CREATE / UPDATE / ADOPT
```

Configuration comes from the environment: `LMS_SCHOOL_ACCOUNT` (the student Google account),
`LMS_MAIL_SUBJECT` (subject prefix for mail to instructors) and `LMS_CDP_URL` (a Chromium
DevTools endpoint, if you reuse a real browser session). Google OAuth tokens are created with
`./lms.sh classroom-auth` and `./lms.sh cal-auth` and stay outside the repo.

## Limits, stated plainly

- It targets **one university's** Blackboard instance (`ubaguio.blackboard.com`) and its Google
  Workspace. Other schools would need their own host and SSO flow.
- Blackboard sign-in is Google SAML. A warm-up timer keeps the session alive, but renewing a dead
  one still needs a person at the Google sign-in.
- Names of instructors and classmates in tests and comments are replaced with placeholders
  (`Instructor A`, …), and gradebook IDs with synthetic ones.

## License

Proprietary, all rights reserved. See [LICENSE](LICENSE). Published for reading, not for reuse.

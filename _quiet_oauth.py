"""Silence the cosmetic 'Not all requested scopes were granted' line.

Google deliberately grants a scope set that omits `classroom.coursework.me`
(covered by `student-submissions.me`), and google-auth logs a WARNING about the
mismatch on every credential refresh. It is expected and handled
(OAUTHLIB_RELAX_TOKEN_SCOPE), but it is NOT harmless: on 2026-08-29 a study load
READ that stderr line and wrongly concluded a submitted quiz's status was
"unreadable", inventing a leniency question for work already turned in. A model
that sees the line misdiagnoses. So kill it at the source.

Narrow on purpose — only the auth loggers, so googleapiclient/discovery ERRORs
still surface. Import this module for its side effect before building creds.
"""
import logging
import os
import warnings

os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
warnings.filterwarnings("ignore", module="oauthlib")
for _n in ("google.auth", "google.oauth2", "oauthlib",
           "requests_oauthlib", "google_auth_httplib2"):
    logging.getLogger(_n).setLevel(logging.ERROR)

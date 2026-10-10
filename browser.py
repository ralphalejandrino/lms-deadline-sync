#!/usr/bin/env python3
"""One place that decides how these scripts launch Chromium.

## The headless finding (2026-08-12) — this REVERSES the 2026-08-11 note

For a full day the rule here was "MUST RUN HEADED; headless returns 401", and an
Xvfb hunt was started to hide the window. **That was wrong**, and the reason is
subtle enough to write down:

`headless=True` in Playwright does **not** run the Chromium you get headed. It
launches a *different binary* — `chrome-headless-shell` — with a different
fingerprint, which Google's SSO refuses. Passing `headless=False` together with
`args=["--headless=new"]` runs the **real Chromium** in new-headless mode, and
the saved Google/Blackboard session is honoured:

    /users/me           -> 200
    classroom.google.com -> still signed in

So there is no visible window, and Xvfb was never needed. `/tmp/.X11-unix` being
a read-only WSLg tmpfs is real, it just does not matter.

⚠ **Do not "simplify" `headless=False, args=["--headless=new"]` into
`headless=True`.** It looks like the same thing and it is not — it silently
breaks authentication.

`sso_login.py` stays genuinely headed: Ralph has to see the form and type.

## Tab hygiene

A persistent profile restores whatever tabs it had, so these profiles were
accumulating pages run after run (Ralph noticed the tab pile and asked about
RAM). `launch()` closes every page but one, every time.
"""
import os
import sys
from pathlib import Path


def proxy_arg():
    """Playwright `proxy=` for this machine, or None.

    🆕 2026-09-14 (CLOUD). A Claude Code cloud VM has NO direct egress: every
    outbound HTTPS call goes through an agent proxy named in $HTTPS_PROXY, and
    its CA already sits in /usr/local/share/ca-certificates. `curl` and `requests`
    read those env vars on their own; **Chromium does not** — it ignores
    $HTTPS_PROXY entirely and every navigation dies `ERR_CONNECTION_RESET`.

    That reset is what made the 2026-09-14 cloud load report SESSION DEAD while
    the relayed cookies were in fact perfectly good (`/users/me` -> 200 over
    plain urllib, same VM, same minute). A dead network and a dead session are
    indistinguishable from the error message — the same confusion the UNKNOWN
    rule exists to prevent.

    🔴 Never "fix" a proxy TLS complaint with --ignore-certificate-errors. That
    turns a verified channel into an unverified one for every later run, and the
    cert is already trusted at the system level.
    """
    server = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    return {"server": server} if server else None


# Extra flags that only make sense when a re-terminating proxy is in the path.
# Chromium's default ClientHello carries an Encrypted-Client-Hello GREASE
# extension and tries QUIC first; the agent proxy answered both by killing the
# tunnel mid-handshake, which surfaces as a bare net::ERR_CONNECTION_RESET with
# no TLS error to read. Measured in the proxy's own log: "1729 B sent, 39 B
# received, tunnel closed (1006)" against blackboard.ubaguio.edu:443.
# 🔴 These constrain how TLS is NEGOTIATED. They do not disable verification --
# the proxy CA is trusted through the system/NSS store, and must stay that way.
PROXY_COMPAT = [
    "--disable-quic",
    "--disable-features=EncryptedClientHello",
]


# Flags that keep a long-lived persistent profile from hoarding state/RAM.
LEAN = [
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-session-crashed-bubble",
    "--disable-background-networking",
    "--disable-extensions",
    "--disable-sync",
    "--renderer-process-limit=2",
]


def launch(pw, profile: Path, show: bool = None, viewport=None, extra_args=None):
    """Launch a persistent context. Invisible by default.

    show=True  -> a real visible window (interactive login, debugging)
    show=None  -> honour `--show` on the command line, else invisible
    Returns (context, page) with exactly one page open.
    """
    if show is None:
        show = "--show" in sys.argv
    args = list(LEAN) + list(extra_args or [])
    if not show:
        # The real Chromium binary, running headless. NOT headless=True.
        args.append("--headless=new")
    kw = {}
    proxy = proxy_arg()
    if proxy:
        kw["proxy"] = proxy
        args += [a for a in PROXY_COMPAT if a not in args]
    ctx = pw.chromium.launch_persistent_context(
        user_data_dir=str(profile),
        headless=False,
        viewport=viewport or {"width": 1280, "height": 1000},
        args=args,
        **kw,
    )
    pages = ctx.pages or [ctx.new_page()]
    for extra in pages[1:]:          # restored tabs from previous runs
        try:
            extra.close()
        except Exception:
            pass
    return ctx, pages[0]

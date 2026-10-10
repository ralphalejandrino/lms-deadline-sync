#!/usr/bin/env bash
# Setup script for the "Cici — study" CLOUD environment. Source of truth for
# what goes in claude.ai/code → environment settings → Setup script.
#
# Paste the BODY below (everything after `set`) into that field — do not rely on
# this file existing at setup time. The environment snapshot keeps what this
# installs for ~7 days, so later sessions start with it already present.
# Must finish in under ~5 minutes (platform limit). Runbook:
# school/context/cloud-setup.md
#
# The repo-dependent half — adopting the preinstalled Chromium, checking out
# main — runs per session in scripts/cloud-session-start.sh, where the repo
# is guaranteed to be cloned.
set -uo pipefail

VENV="$HOME/.venv-lms"
[ -x "$VENV/bin/python" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" -q install --upgrade pip
"$VENV/bin/pip" -q install playwright google-api-python-client google-auth \
    google-auth-oauthlib cryptography

# Browser: prefer the image's preinstalled build (lms.sh prime adopts it). Only
# download if the image has none — cdn.playwright.dev needs Full network.
if ! ls -d "${PLAYWRIGHT_BROWSERS_PATH:-/opt/pw-browsers}"/chromium-* >/dev/null 2>&1; then
    "$VENV/bin/playwright" install chromium || echo "setup: chromium download failed" >&2
fi
exit 0

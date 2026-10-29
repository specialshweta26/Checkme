"""Checkme computer template: E2B "desktop" + node/npm/pnpm/yarn, python/pip/uv, git, gh, vercel, render, Playwright.

Build with build.py (needs E2B_API_KEY in the environment). See docs/COMPUTER.md.
"""

from pathlib import Path

from e2b import Template

PROVISION = Path(__file__).with_name("provision.sh")

template = (
    Template()
    .from_template("desktop")
    .set_user("root")
    .copy(str(PROVISION.name), "/tmp/checkme-provision.sh")
    .run_cmd("bash /tmp/checkme-provision.sh && rm -f /tmp/checkme-provision.sh")
    .set_user("user")
)

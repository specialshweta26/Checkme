"""Classify a fetched page or a browser tab as blocked by bot protection (shared by every fetch path).

Standalone on purpose (standard library only): the API imports it, and the browser daemon on the cloud computer
imports the same file (uploaded next to it as ``checkme_block_detect.py``), so the server fetch, the real browser
and the read-only fallbacks all agree on what "blocked" means.

``detect_block`` returns ``None`` for a usable page, else a dict::

    {"blocked": True, "kind": ..., "vendor": ..., "interactive": bool, "reason": str}

kind:
- ``captcha``      a puzzle or checkbox a person has to solve (reCAPTCHA, hCaptcha, Turnstile, PerimeterX
                   press-and-hold, DataDome slider, Google /sorry).
- ``challenge``    a passive JavaScript check that a real browser usually clears on its own after a few seconds
                   (Cloudflare "Just a moment", Akamai sec-cpt, AWS WAF, Kasada, Imperva).
- ``denied``       the site refused this network or client outright (Akamai "Access Denied", HTTP 403 pages).
- ``rate_limited`` HTTP 429 or "too many requests".
- ``empty_shell``  only from ``detect_block(..., shell_is_block=True)``: a near-empty JavaScript shell.

Text markers only count on short pages (a long article that mentions "access denied" is not a wall), and HTML
vendor markers only count on error statuses or short pages (many normal pages embed reCAPTCHA for forms).
"""

from __future__ import annotations

import re

SHORT_TEXT = 1500  # visible characters; bot walls are short
THIN_TEXT = 180

# (vendor, kind, markers found in the URL)
_URL_MARKERS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("google", "captcha", ("google.com/sorry", "/sorry/index")),
    ("cloudflare", "challenge", ("/cdn-cgi/challenge-platform", "__cf_chl", "challenges.cloudflare.com")),
    ("datadome", "captcha", ("captcha-delivery.com", "geo.captcha")),
    ("perimeterx", "captcha", ("px-captcha", "/blocked?url=")),
    ("recaptcha", "captcha", ("google.com/recaptcha", "recaptcha/api")),
    ("hcaptcha", "captcha", ("hcaptcha.com",)),
    ("akamai", "challenge", ("/_sec/cp_challenge", "sec-cpt")),
    ("generic", "captcha", ("/captcha", "validatecaptcha", "/errors/validatecaptcha")),
)

# (vendor, kind, markers in the raw HTML) - only on error statuses or short pages.
_HTML_MARKERS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("cloudflare", "challenge", ("_cf_chl_opt", "cf-chl-", "challenge-platform", "cf_chl_prog", "cf-turnstile")),
    ("akamai", "challenge", ("sec-if-cpt-container", "_sec/cp_challenge", "ak_bmsc", "bm-verify")),
    ("akamai", "denied", ("errors.edgesuite.net", "reference&#32;&#35;", "reference #18.")),
    ("perimeterx", "captcha", ("px-captcha", "_pxappid", "perimeterx", "human-challenge")),
    ("datadome", "captcha", ("captcha-delivery.com", "datadome", "dd={'")),
    ("imperva", "challenge", ("_incapsula_resource", "incapsula incident", "imperva")),
    ("aws-waf", "challenge", ("awswafintegration", "gokuprops", "aws-waf-token", "challenge.js")),
    ("kasada", "challenge", ("kpsdk", "ips.js?")),
    ("f5", "challenge", ("tspd", "bobcmn")),
    ("hcaptcha", "captcha", ("hcaptcha.com/1/api.js", "h-captcha")),
    ("recaptcha", "captcha", ("g-recaptcha", "recaptcha/api.js")),
    ("amazon", "captcha", ("/errors/validatecaptcha", "opfcaptcha")),
)

# (kind, phrases in title or visible text) - only on short pages.
_TEXT_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("captcha", (
        "are you a robot", "i'm not a robot", "i am not a robot", "verify you are human", "verify you are a human",
        "please verify you are", "select all images", "press & hold", "press and hold", "complete the security check",
        "please complete the security check", "solve this puzzle", "slide to verify", "type the characters you see",
        "enter the characters you see", "confirm you are human", "human verification",
    )),
    ("challenge", (
        "just a moment", "checking your browser", "checking if the site connection is secure",
        "verifying you are human", "enable javascript and cookies to continue", "ddos protection by",
        "please wait while we verify", "one more step", "attention required", "pardon our interruption",
        "please enable js and disable any ad blocker",
    )),
    ("denied", (
        "access denied", "you don't have permission to access", "you do not have permission to access",
        "request blocked", "the request could not be satisfied", "403 forbidden", "error 1020", "access to this page has been denied",
        "sorry, you have been blocked", "you have been blocked", "this request was blocked", "forbidden",
        "unusual traffic", "unusual activity", "automated queries", "our systems have detected",
        "not available in your region", "is not available in your country",
    )),
    ("rate_limited", ("too many requests", "rate limit exceeded", "429 too many")),
)

_INTERACTIVE = {"captcha"}
_GOOGLE_SORRY = ("unusual traffic", "automated queries", "our systems have detected")
_REASONS = {
    "captcha": "{host} is showing a CAPTCHA (human check).",
    "challenge": "{host} is running a bot check that did not clear.",
    "denied": "{host} refused access from this network or browser (bot protection).",
    "rate_limited": "{host} is rate-limiting requests from this network.",
    "empty_shell": "{host} returned an empty page (it needs a real browser to render).",
}


def host_of(url: str) -> str:
    m = re.match(r"^[a-z][a-z0-9+.-]*://([^/?#:]+)", (url or "").strip(), re.I)
    host = (m.group(1) if m else "").lower()
    return host[4:] if host.startswith("www.") else host


def page_is_thin(text: str) -> bool:
    body = (text or "").strip()
    if len(body) < THIN_TEXT:
        return True
    low = body.lower()
    return any(token in low for token in ("loading", "please wait", "just a moment", "searching")) and len(body) < 1200


def _result(kind: str, vendor: str, url: str) -> dict:
    host = host_of(url) or "the site"
    return {
        "blocked": True,
        "kind": kind,
        "vendor": vendor,
        "interactive": kind in _INTERACTIVE,
        "host": host,
        "reason": _REASONS.get(kind, "{host} blocked the request.").format(host=host),
    }


def detect_block(
    status: int | None = None,
    url: str = "",
    title: str = "",
    text: str = "",
    html: str = "",
    *,
    shell_is_block: bool = False,
) -> dict | None:
    """``None`` when the page looks usable; else what kind of wall it is (see module doc)."""
    url_l = (url or "").lower()
    title_l = (title or "").lower().strip()
    body = (text or "").strip()
    low = body[:6000].lower()
    short = len(body) < SHORT_TEXT
    code = int(status or 0)

    # Cookie/consent interstitials are not walls ("Before you continue to Google").
    if "before you continue" in f"{title_l} {low[:400]}" and "robot" not in low:
        return None
    for vendor, kind, marks in _URL_MARKERS:
        if any(m in url_l for m in marks):
            return _result(kind, vendor, url)
    blob = f"{title_l}\n{low}"
    if short or code in {403, 429, 503}:
        for kind, phrases in _TEXT_MARKERS:
            if any(p in blob for p in phrases):
                vendor = "google" if any(p in blob for p in _GOOGLE_SORRY) else _vendor_from_html(html) or "generic"
                if vendor == "google" and kind == "denied":
                    kind = "captcha"
                return _result(kind, vendor, url)
    if html and (short or code >= 400 or code == 202):
        raw = html[:200000].lower()
        for vendor, kind, marks in _HTML_MARKERS:
            if any(m in raw for m in marks):
                if vendor in {"recaptcha", "hcaptcha"} and not short:
                    continue  # a form on a normal page
                if code == 403 and kind == "challenge" and len(body) < THIN_TEXT:
                    kind = "denied" if vendor == "akamai" else kind
                return _result(kind, vendor, url)
    if code == 429:
        return _result("rate_limited", "generic", url)
    if code in {401, 403, 451} or (code in {202, 503} and short):
        return _result("denied" if code != 202 else "challenge", _vendor_from_html(html) or "generic", url)
    if shell_is_block and page_is_thin(body):
        return _result("empty_shell", "generic", url)
    return None


def _vendor_from_html(html: str) -> str:
    raw = (html or "")[:200000].lower()
    for vendor, _kind, marks in _HTML_MARKERS:
        if any(m in raw for m in marks):
            return vendor
    return ""

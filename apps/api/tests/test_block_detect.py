"""The shared bot-wall detector used by the server fetch, the computer browser and the read-only ladder."""

from app.connectors.block_detect import detect_block, host_of, page_is_thin
from app.connectors.computer_daemon import annotate_page, bot_wall

LONG = "Real product listing with prices and reviews. " * 60


def test_akamai_access_denied_is_denied_not_captcha():
    text = 'Access Denied You don\'t have permission to access "http://www.ajio.com/" on this server. Reference #18.d7'
    block = detect_block(403, "https://www.ajio.com/", "Access Denied", text, "<html><body>Access Denied</body></html>")
    assert block and block["kind"] == "denied" and block["interactive"] is False
    assert block["host"] == "ajio.com" and "ajio.com" in block["reason"]


def test_cloudflare_challenge_is_passive():
    html = '<html><head><title>Just a moment...</title></head><body><script>window._cf_chl_opt={}</script></body></html>'
    block = detect_block(403, "https://example.com/", "Just a moment...", "Checking if the site connection is secure", html)
    assert block["kind"] == "challenge" and block["vendor"] == "cloudflare" and not block["interactive"]


def test_captcha_vendors_are_interactive():
    assert detect_block(200, "https://geo.captcha-delivery.com/captcha/?x", "", "", "")["vendor"] == "datadome"
    px = detect_block(403, "https://shop.example/", "Access to this page has been denied", "Press & Hold to confirm you are a human", '<div id="px-captcha"></div>')
    assert px["kind"] == "captcha" and px["interactive"] is True
    google = detect_block(None, "https://www.google.com/sorry/index", "Sorry", "unusual traffic from your computer network")
    assert google["kind"] == "captcha" and google["vendor"] == "google"
    amazon = detect_block(200, "https://www.amazon.in/errors/validateCaptcha", "Amazon.in", "Type the characters you see in this image")
    assert amazon["kind"] == "captcha"


def test_rate_limit_and_status_only_blocks():
    assert detect_block(429, "https://x.example/", "", LONG)["kind"] == "rate_limited"
    assert detect_block(503, "https://x.example/", "Service Unavailable", "Oops, try again later")["kind"] == "denied"
    assert detect_block(202, "https://x.example/", "", "", "<html><script src=/challenge.js></script></html>")["kind"] == "challenge"


def test_normal_pages_are_not_blocked():
    # A long page that mentions access denied / embeds reCAPTCHA in a form is a normal page.
    assert detect_block(200, "https://news.example/a", "Why you see Access Denied errors", LONG + " access denied ") is None
    html = '<form><div class="g-recaptcha"></div></form>' + "<p>x</p>" * 10
    assert detect_block(200, "https://insure.example/", "Compare plans", LONG, html) is None
    # Consent interstitials are not walls.
    assert detect_block(200, "https://consent.google.com/", "Before you continue to Google", "Before you continue") is None
    assert detect_block(200, "https://x.example/", "Home", "short page") is None


def test_empty_shell_only_when_asked():
    assert detect_block(200, "https://spa.example/", "App", "Loading...") is None
    assert detect_block(200, "https://spa.example/", "App", "Loading...", shell_is_block=True)["kind"] == "empty_shell"
    assert page_is_thin("Loading") and not page_is_thin(LONG)


def test_host_of():
    assert host_of("https://www.Myntra.com/men?x=1") == "myntra.com"
    assert host_of("not a url") == ""


def test_daemon_bot_wall_hands_off_only_captchas():
    denied = bot_wall("https://www.ajio.com/", "Access Denied", "Access Denied. You don't have permission to access this.")
    assert denied["bot_wall"] is True and denied["handoff"] is False and denied["block"]["kind"] == "denied"
    captcha = bot_wall("https://www.google.com/sorry/index", "Sorry", "unusual traffic")
    assert captcha["handoff"] is True and captcha["bot_wall_kind"] == "captcha"
    page = annotate_page({"ok": True, "url": "https://www.ajio.com/", "title": "Access Denied", "text": "Access Denied"})
    assert page["ok"] is False and "other sites" in page["hint"]

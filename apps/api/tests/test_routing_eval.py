"""Offline connector-choice checks for first-party rules. No LLM, no database."""

from app.routing.split import first_party_tag

CASES = [
    ("show my unread inbox", "mail"),
    ("summarize the thread with Priya", "mail"),
    ("delete all promo emails from last year", "mail"),
    ("download the pdf on that invoice", "files"),
    ("am I free Thursday afternoon", "calendar"),
    ("what's on my calendar this week", "calendar"),
    ("reply to Raj saying I can do 3pm", "mail"),
]


def test_eval_first_party_tags():
    misses = []
    for query, tag in CASES:
        hit = first_party_tag(query)
        if hit is None or hit[1] != tag:
            misses.append((query, tag, hit))
    assert misses == []

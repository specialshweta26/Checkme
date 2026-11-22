"""Rules for the tool groups a turn can start without (app/agent/tool_scope.py).

The browser, computer and office rules were about a quarter of the fixed system prompt, sent on every model call even
when those tools were left out of the turn. Now the prompt carries only the rules for the groups the turn shows, and
``use_tools`` hands a group's rules back when the model loads it. The tool list is sticky per chat, so the prompt
stays the same from turn to turn and the provider's prompt cache still hits.
"""

from __future__ import annotations


def fast_browser_hint() -> str:
    try:
        from app.agent import fast_browser

        return fast_browser.prompt_hint()
    except Exception:  # noqa: BLE001
        return ""


def browser_rules() -> str:
    return (
        (
            "Browser operator: execute the user's website task on the live computer yourself. Do not tell them "
            "how to click. When the user wants the items, prices, or reviews from a listing, apply the site's own"
            ' filter if it is on the snapshot, then call browser_collect with the numeric range and the words '
            'they used. Do not answer from a clipped snapshot, and do not say the full listing is unavailable. If'
            ' text_omitted is true, the snapshot dropped the middle of the page. If next_offset is set, call '
            'browser_collect again with that offset until every match is in hand. If the result is too_large, '
            'inspect_result and page until every requested row is in hand. Quote only prices and review text the '
            'tool returned, and say when a price can still vary by size. Reviews are the ones shown on the page, '
            'not a full export. Loop until done: inspect the snapshot (tree, verified, alerts, tables) → '
            'browser_act everything you can already see to do → read the new snapshot → continue. '
        )
        + fast_browser_hint()
        + (
            'Identify controls by role and name first, then label, then visible text; computer_act x,y last. Fill'
            ' whole forms from the snapshot: one fill per ordinary field. Comboboxes and type-ahead lists '
            "(airports, places, search suggestions): type or select/pick with that field's ref and the value — do"
            ' not fill, and do not batch several type-ahead fields in one browser_act; pick each matching option '
            'before the next field. If the option has no ref, computer_observe then computer_act (click the '
            'suggestion, or press ArrowDown then Enter). Never request_takeover to pick a dropdown option. Dates,'
            ' checkboxes, radios, and page file pickers are your job (upload with path= a computer file; download'
            ' saves the file). Dates: fill the date field itself with YYYY-MM-DD (the browser converts it); click'
            ' through a calendar only if that fails, and check the fields show the dates before Apply. Menus that'
            ' open on mouse-over: hover, then click the item. Controls that toggle or move you on (a sort header,'
            ' Next) get one click per browser_act: read the result before the next click. Never count, total or '
            'compare across pages or long lists in your head: use browser_gather, then gathered_query, and report'
            " those figures. After every step, confirm it worked from the result: the control's state in the "
            "element tree, check.page_changed, navigated, and the page text. Don't call a step done or failed "
            "until you've looked. A page result's dialogs lists pop-ups that appeared: alerts are closed and "
            "confirm or prompt pop-ups are declined automatically. If the user's request calls for accepting one,"
            " send accept_dialog (text = the prompt's answer) followed by the step that raises it. Each page "
            "result's so_far lists the steps done this turn, and seen_on_pages holds amounts and references read "
            'directly from those pages. Treat both as verified: report them plainly with their values, even after'
            " you've left the page, and don't call them unverified. Work through the user's steps in order and "
            "check so_far before any final button, because a skipped step can't be undone after an order is "
            'placed. Once a final step is done, never go back and redo the task to fix something: report what '
            "happened. When you call browser_task, give it every step in the user's own words. When the user asks"
            ' for screenshots or evidence, call browser_screenshot on each page that matters while it is showing,'
            ' and say what each one shows. Each model round is slow, so send every step you can already see in '
            'ONE browser_act: all the fields of a form plus its button, or each item to add. It stops at the '
            'first step that fails, and refs stay valid until the page changes. Only a step whose result decides '
            'the next one needs its own call, and a sort or dropdown always ends the batch: the list may reorder,'
            ' so read it before clicking anything in it. A control that repeats shows what it belongs to in '
            "brackets. A dropdown's line in the element tree lists its options and current choice: use select "
            "with that ref and the option's text, and check the page changed. When a result lists warnings that a"
            ' step may not have done anything, do not repeat it: try another way. When the element tree ends with'
            " '… N more elements', call browser_snapshot with find= the words you need. After each critical step "
            'check verified.alerts and that values stuck; retry once with another strategy if they did not. If a '
            'page tool returns fallback computer_act, call computer_observe then computer_act (click x,y / type /'
            " key / scroll). Prefer the destination site (put dates and filters in that site's URL when the user "
            'gave them) over a Google Search results page for live results, prices, availability, or forms — '
            'Google Search often returns a verification wall to the cloud browser. Cite only what is visible on '
            'the snapshot. If the page has no prices, times, or results yet, say so. Never treat a generic '
            'landing page as a confirmed result. If the page is a CAPTCHA that only a person can solve (bot_wall '
            "with handoff=true, or a 'press and hold' / picture puzzle): first get the answer from other sites if"
            ' that is enough; when the user needs that exact site (to log in, buy, or see their own data), '
            'request_takeover so they solve it on the live screen, and do not invent numbers. Login, 2FA, '
            'captcha, or payment: request_takeover (same as browser_handoff) with a short instruction so the user'
            ' takes control of that same screen. After they finish, snapshot and continue. Do not hand over for '
            'dropdowns, autocomplete, dates, or other page controls — keep acting until the form is filled. A '
            "field that shows a value but won't take typing (a big From / To / date box) opens a picker when "
            'clicked: click it, type into the search box that appears, and click the matching suggestion. For any'
            ' date (a calendar, a date box, a travel or report date) use browser_act pick_date with the date '
            "field's ref and the date as YYYY-MM-DD; it opens the calendar, moves to the month and clicks the "
            "day; don't click calendar days or month arrows one at a time. A form already filled with an earlier "
            'search (the site remembers it) is just a starting point: change every field to what the user asked. '
            'Never hand the screen over because a form is awkward, and never answer from an old search. If a '
            'browser tool says it failed to start or a module is missing after the user used the computer, the '
            'live desktop still has their work: snapshot or observe and continue the remaining steps. Never '
            'declare the job failed because the page browser restarted. Never attach page.png in chat; the '
            'computer screen already shows the tab. Refs (e0, e1) are only valid for the latest snapshot; after a'
            ' page changes use the new ones. If an action returns ok=false, read its error and the fresh '
            'snapshot, then adjust instead of stopping. A search-results link you build yourself (dates, places, '
            'filters in the URL) often will not open: when browser_navigate returns deep_link_failed, open its '
            "fallback_url and use the site's own search form and filters like a person; do not answer from web "
            'search or give up while the site itself still works. '
        )
    )


def computer_rules() -> str:
    return (
        'For apps other than the browser, use computer_windows to find them and computer_ui to read and press'
        ' their controls; click at screen positions only when an app exposes no controls. If an app shows an '
        'unexpected dialog, read it and handle it if your task covers it; otherwise tell the user rather than'
        ' dismiss it. If an app has quit, open it again and check what it shows before carrying on. '
    )


def office_rules() -> str:
    return (
        'To make a spreadsheet or Word document, use create_office_file. To make a presentation or slide '
        'deck, on the website or in the desktop app, use create_presentation and design every slide; do not '
        'use create_office_file for PowerPoint. To work inside Numbers, Pages, Keynote, Excel, Word or '
        "PowerPoint, use office_app first and computer_ui only for what it can't do. "
    )


RULES = {"browser": browser_rules, "computer": computer_rules, "office": office_rules}


def for_group(group: str) -> str:
    fn = RULES.get((group or "").strip().lower())
    return fn() if fn else ""


def for_visible(hidden: list | None = None) -> str:
    """The rules for every group this turn shows. ``hidden=None`` (no scoping) means all of them."""
    left_out = set(hidden or [])
    return "".join(fn() for group, fn in RULES.items() if group not in left_out)

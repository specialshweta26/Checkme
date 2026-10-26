"""Per-agent focus areas picked on a new agent's first screen.

The onboarding card in chat ("What would you most like a hand with?") offers these, in
order, as A–D. Picking one stores ``WorkspaceAgent.focus``; the deep agent then gets the
focus prompt (and, for coding, extra plan-and-build tools) on every turn.
"""

from __future__ import annotations

FOCUS_ORDER = ("email_calendar", "coding", "research", "errands")

FOCUSES: dict[str, dict[str, str]] = {
    "email_calendar": {
        "label": "Email and calendar",
        "title": "email and calendar assistant",
        "answer": "I'd most like a hand with email and calendar.",
        "prompt": (
            "Focus: Email and calendar. Keep the user's inbox and calendar under control: triage what needs a reply, "
            "summarize threads, draft replies in their voice, find times and schedule meetings in their timezone. "
            "Use the connected mail and calendar tools. Never send mail or create invites without the confirmation card. "
            "If no mail or calendar connector is connected, say which one to connect."
        ),
    },
    "coding": {
        "label": "Coding and deployments",
        "title": "software engineer",
        "answer": "I'd most like a hand with coding and deployments.",
        "prompt": (
            "Focus: Coding and deployments. You are a hands-on software engineer. You work on this user's computer: the "
            "`computer` connector is their Daytona cloud sandbox with a shell, files, git and a browser.\n"
            "Always plan first, then build:\n"
            "1) Plan. Before changing anything, call write_todos with a short plan (understand -> change -> test -> ship) and "
            "keep it updated as you go. State the plan to the user in one or two lines, then carry on. Only stop to ask when the "
            "request is ambiguous or risky (deleting data, force-pushing, touching production).\n"
            "2) Check readiness. Call coding_workspace_status once per task. It reports whether the computer, git, GitHub "
            "and a deploy connector are ready, and lists exactly what is missing.\n"
            "3) Build on the computer. Use sandbox_shell for everything on disk: git clone into ~/work/<repo>, read code with "
            "ls, rg, sed -n or cat, edit files with computer write_file or a shell heredoc/python patch, install dependencies, and "
            "run the project's tests, linters and build. Iterate until they pass. Quote real exit codes and the relevant output; "
            "never claim a command ran or passed if it did not. deepagents' own read_file/write_file/ls/edit_file are a scratch "
            "notepad, not the computer: never use them for repository files.\n"
            "4) Ship. Work on a new branch (never commit straight to main or master unless the user asks). Commit with clear "
            "messages. To push and open a pull request: if coding_workspace_status shows git credentials in the sandbox, use "
            "git push and gh pr create in sandbox_shell; otherwise use the connected GitHub tools (search_tools for 'create "
            "branch', 'create or update file' / 'push files', 'create pull request'). Never merge unless the user asks. Share the "
            "PR link.\n"
            "5) Deploy. If a deploy connector (Render, Vercel, Netlify, Fly, Railway, Heroku) is connected, search_tools for "
            "deploys, services and logs to trigger or check a deploy, then report its status, URL and any errors from the logs.\n"
            "If something needed is missing, say exactly what and how to fix it, e.g. 'GitHub is not connected: connect it with "
            "connect_app github so I can push and open the PR' or 'The computer is not available on this server (Daytona is not "
            "configured)'. Do the parts you can (plan, code, tests) before reporting what is blocked."
        ),
    },
    "research": {
        "label": "Research and writing",
        "title": "researcher and writer",
        "answer": "I'd most like a hand with research and writing.",
        "prompt": (
            "Focus: Research and writing. Research on the web with the computer browser (a Google search, then open at least "
            "three relevant results) or web_fetch, and prefer primary and recent sources. Keep a running source list. Cite every "
            "factual claim inline as [1], [2] and end with a Sources list of title, publisher, date and URL. Point out where "
            "sources disagree or are out of date. For writing, ask for audience, length and tone only when they are unclear; "
            "otherwise propose a short outline and then draft. Match the user's voice (run_staff_tool learn_voice when useful). "
            "Deliver long drafts as a downloadable file via the computer (write_file + publish_file) as well as in chat."
        ),
    },
    "errands": {
        "label": "Errands and bookings",
        "title": "errands and bookings assistant",
        "answer": "I'd most like a hand with errands and bookings.",
        "prompt": (
            "Focus: Errands and bookings. Find, compare and book things (tables, tickets, travel, appointments, orders) using "
            "connected apps or the computer browser. Compare options with prices and times in a short table. For logins, 2FA, "
            "captchas or payment use browser_handoff, and always get the user's go-ahead before anything is bought or booked."
        ),
    },
}

UNSET_PROMPT = (
    "Your focus area is not set yet. This user just created you and was asked what they would most like a hand with: "
    "Email and calendar, Coding and deployments, Research and writing, or Errands and bookings. If their message says what "
    "they want help with, call set_my_focus with the closest key (email_calendar, coding, research, errands), then reply "
    "warmly in two or three sentences: confirm what you will focus on, name one or two concrete things you can do, and ask "
    "for the first task (or start it if they already gave one). If it fits none of these, use update_agent to set your title "
    "and instructions instead."
)


def normalize_focus(value: str | None) -> str:
    """Return a focus key, '' to clear, or raise ValueError."""
    raw = (value or "").strip().lower()
    if not raw:
        return ""
    if raw in FOCUSES:
        return raw
    for key, meta in FOCUSES.items():
        if raw == meta["label"].lower():
            return key
    aliases = {
        "email": "email_calendar",
        "calendar": "email_calendar",
        "mail": "email_calendar",
        "code": "coding",
        "coding_deployments": "coding",
        "deployments": "coding",
        "dev": "coding",
        "writing": "research",
        "research_writing": "research",
        "errand": "errands",
        "bookings": "errands",
        "booking": "errands",
    }
    if raw in aliases:
        return aliases[raw]
    raise ValueError(f"Unknown focus '{value}'. Pick one of: {', '.join(FOCUS_ORDER)}.")


def focus_label(key: str | None) -> str:
    meta = FOCUSES.get(key or "")
    return meta["label"] if meta else ""


def focus_prompt(agent) -> str:
    """System-prompt block for this agent's focus ('' for Chief with no focus)."""
    key = (getattr(agent, "focus", "") or "").strip()
    if key in FOCUSES:
        return FOCUSES[key]["prompt"] + "\n"
    if (
        agent is not None
        and (getattr(agent, "kind", "custom") or "custom") != "default"
        and not (getattr(agent, "instructions", "") or "").strip()
    ):
        return UNSET_PROMPT + "\n"
    return ""


def options() -> list[dict]:
    return [
        {"key": key, "letter": chr(ord("A") + i), "label": FOCUSES[key]["label"], "answer": FOCUSES[key]["answer"]}
        for i, key in enumerate(FOCUS_ORDER)
    ]

"""Turn thumbs-downs from the app into agent test cases (docs/QUALITY.md).

    curl -b cookies https://<app>/api/quality/export > feedback.json      # or "Export as test cases" on the Quality page
    cd apps/api && uv run python -m evals.from_feedback feedback.json     # adds them to evals/feedback_cases.json

Each case keeps the request, the turns before it, the bad reply and why it was bad. Before it can run, fill in what
the agent must or mustn't do (``must_call``, ``must_not_call``, ``reply_has``, ``reply_lacks``, as in scenarios.py);
``evals.run`` skips cases that don't say yet, and lists them. Cases are matched by name, so re-importing updates them.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

CASES = Path(__file__).with_name("feedback_cases.json")
CHECKS = ("must_call", "must_not_call", "reply_has", "reply_lacks")


def load_cases(path: Path | None = None) -> list[dict]:
    path = path or CASES
    if not path.exists():
        return []
    data = json.loads(path.read_text() or "[]")
    return data if isinstance(data, list) else data.get("cases", [])


def merge(existing: list[dict], incoming: list[dict]) -> list[dict]:
    """Add new cases; an existing case keeps the checks someone already filled in."""
    by_name = {c["name"]: c for c in existing}
    for case in incoming:
        old = by_name.get(case["name"])
        if old is not None:
            for key in CHECKS:
                if old.get(key):
                    case[key] = old[key]
        by_name[case["name"]] = case
    return list(by_name.values())


def ready(case: dict) -> bool:
    return any(case.get(k) for k in CHECKS)


def scenarios() -> list:
    """Cases with checks filled in, as Scenarios for evals.run."""
    from evals.harness import Scenario

    out = []
    for c in load_cases():
        if not ready(c) or not c.get("message"):
            continue
        out.append(
            Scenario(
                c["name"], c["message"], history=list(c.get("history") or []),
                must_call=list(c.get("must_call") or []), must_not_call=list(c.get("must_not_call") or []),
                reply_has=list(c.get("reply_has") or []), reply_lacks=list(c.get("reply_lacks") or []),
                note=f"From user feedback: {c.get('why') or 'thumbs down'}", max_seconds=float(c.get("max_seconds") or 90),
            )
        )  # fmt: skip
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__)
        return 2
    data = json.loads(Path(argv[0]).read_text())
    incoming = data.get("cases", []) if isinstance(data, dict) else data
    cases = merge(load_cases(), incoming)
    CASES.write_text(json.dumps(cases, indent=2, ensure_ascii=False) + "\n")
    todo = [c["name"] for c in cases if not ready(c)]
    print(f"{len(incoming)} imported, {len(cases)} in {CASES.name}; {len(todo)} still need checks before they run.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

"""Run the agent quality checks against the real model.

    cd apps/api && uv run python -m evals.run                  # all scenarios
    cd apps/api && uv run python -m evals.run "list" --repeat 3   # names containing "list", three times each

Needs OPENAI_API_KEY, and costs a few model calls per scenario. Writes evals/last_run.json.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from evals.harness import failed, run_scenario, summary, to_json
from evals.scenarios import scenarios


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("only", nargs="?", default="", help="run scenarios whose name contains this")
    parser.add_argument("--repeat", type=int, default=1)
    args = parser.parse_args()
    logging.basicConfig(level=logging.ERROR)
    from evals import from_feedback

    every = scenarios() + from_feedback.scenarios()  # feedback cases need their checks filled in first
    waiting = [c["name"] for c in from_feedback.load_cases() if not from_feedback.ready(c)]
    if waiting:
        print(f"{len(waiting)} feedback case(s) skipped until their checks are filled in (evals/feedback_cases.json).")
    chosen = [s for s in every if args.only.lower() in s.name.lower()]
    results = []
    for s in chosen:
        for _ in range(max(1, args.repeat)):
            results.append(await run_scenario(s))
            print(summary([results[-1]]).splitlines()[0], flush=True)
    print("\n" + summary(results))
    Path(__file__).with_name("last_run.json").write_text(to_json(results))
    return 1 if failed(results) else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

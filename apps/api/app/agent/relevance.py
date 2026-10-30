"""Hybrid relevance for research: keyword prefilter, then embeddings, then an optional batched LLM rerank.

Providers come from the app's existing config (OpenAI embeddings / chat via ``app.llm``). With no provider, or when a
provider call fails, everything falls back to the keyword scores, so a crawl never stops on a scoring error.
Nothing here knows about a particular site or task.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass
from typing import Awaitable, Callable, Sequence
from urllib.parse import unquote, urlsplit

log = logging.getLogger(__name__)

EmbedFn = Callable[[list[str]], Awaitable[list[list[float]]]]
LlmFn = Callable[[str], Awaitable[str]]

PREFILTER_MAX = 80  # candidates per page that go on to embeddings
LLM_RERANK_TOP = 20


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def link_text(url: str, anchor: str) -> str:
    """What a link 'says': its anchor plus the readable words of its path."""
    parts = urlsplit(url)
    path = re.sub(r"[/_.\-+=&?#]+", " ", unquote(parts.path + " " + parts.query)).strip()
    path = re.sub(r"\b(html?|php|aspx?)\b", " ", path)
    return f"{anchor.strip()} | {(parts.hostname or '').removeprefix('www.')} {path}".strip(" |")[:300]


@dataclass
class Scorer:
    """Scores candidate links and pages against a goal. ``embed`` / ``llm`` are optional providers."""

    embed: EmbedFn | None = None
    llm: LlmFn | None = None
    mode: str = "auto"  # auto | keywords | embeddings | llm
    # Absolute cosine calibration for page scores (OpenAI text-embedding-3-small: unrelated ~0.1, on-topic ~0.5+).
    cos_floor: float = 0.15
    cos_ceil: float = 0.55
    _goal_vec: list[float] | None = None
    _goal: str = ""
    failures: int = 0

    @property
    def provider(self) -> str:
        if self.mode == "keywords" or self.embed is None:
            return "keywords"
        return "embeddings+llm" if (self.llm is not None and self.mode == "llm") else "embeddings"

    async def _vecs(self, texts: list[str]) -> list[list[float]] | None:
        if self.embed is None or self.mode == "keywords" or not texts or self.failures >= 3:
            return None
        try:
            out = await self.embed(texts)
            return out if len(out) == len(texts) else None
        except Exception as exc:  # noqa: BLE001 - provider down or not configured: keywords only
            self.failures += 1
            log.warning("research embeddings failed (%s); using keyword scores", type(exc).__name__)
            return None

    async def goal_vec(self, goal: str) -> list[float] | None:
        if self._goal_vec is not None and self._goal == goal:
            return self._goal_vec
        got = await self._vecs([goal])
        if got:
            self._goal, self._goal_vec = goal, got[0]
        return self._goal_vec

    def _abs(self, cos: float) -> float:
        return max(0.0, min(1.0, (cos - self.cos_floor) / max(1e-6, self.cos_ceil - self.cos_floor)))

    async def score_links(self, goal: str, cands: list[tuple[str, str]], kw: list[float]) -> list[float]:
        """Hybrid link scores. ``kw`` are the keyword scores (already include penalties); same order as ``cands``."""
        scores = list(kw)
        if not cands:
            return scores
        gv = await self.goal_vec(goal)
        if gv is None:
            return scores
        # Prefilter: keyword order, but keep zero-keyword links too (they are what embeddings rescue).
        order = sorted(range(len(cands)), key=lambda i: -kw[i])[:PREFILTER_MAX]
        vecs = await self._vecs([link_text(*cands[i]) for i in order])
        if vecs is None:
            return scores
        cos = [cosine(gv, v) for v in vecs]
        # Links are short text: calibrate within the page (best link on the page ~1, median and below ~0), blended
        # with the absolute scale so a page of only weak links does not look strong.
        srt = sorted(cos)
        med, top = srt[len(srt) // 2], srt[-1]
        for i, c in zip(order, cos):
            rel = (c - med) / (top - med) if top > med else 0.0
            sem = 0.6 * max(0.0, min(1.0, rel)) + 0.4 * self._abs(c)
            penalty = 0.3 if _penalized(cands[i]) else 1.0
            scores[i] = round(max(kw[i], (0.3 * kw[i] + 0.7 * sem) * penalty), 4)
        if self.mode == "llm" and self.llm is not None:
            scores = await self._llm_rerank(goal, cands, scores)
        return scores

    async def _llm_rerank(self, goal: str, cands: list[tuple[str, str]], scores: list[float]) -> list[float]:
        top = sorted(range(len(cands)), key=lambda i: -scores[i])[:LLM_RERANK_TOP]
        if len(top) < 2:
            return scores
        lines = "\n".join(f"{n}. {link_text(*cands[i])}" for n, i in enumerate(top))
        prompt = (
            "Rate how likely following each link helps with the goal, 0 to 10. Reply as JSON "
            '{"scores": [numbers in the same order]}.\n'
            f"Goal: {goal}\nLinks:\n{lines}"
        )
        try:
            raw = await self.llm(prompt)  # type: ignore[misc]
            vals = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))["scores"]  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001
            self.failures += 1
            log.warning("research LLM rerank failed (%s); keeping embedding scores", type(exc).__name__)
            return scores
        out = list(scores)
        for i, v in zip(top, vals):
            try:
                out[i] = round(0.4 * scores[i] + 0.6 * max(0.0, min(1.0, float(v) / 10)), 4)
            except (TypeError, ValueError):
                continue
        return out

    async def score_pages(self, goal: str, texts: list[str], kw: list[float]) -> tuple[list[float], list[list[float] | None]]:
        """Hybrid page scores plus each page's embedding (kept for the research memory)."""
        gv = await self.goal_vec(goal)
        if gv is None:
            return list(kw), [None] * len(texts)
        vecs = await self._vecs([t[:2000] or " " for t in texts])
        if vecs is None:
            return list(kw), [None] * len(texts)
        out = [round(0.4 * k + 0.6 * self._abs(cosine(gv, v)), 4) for k, v in zip(kw, vecs)]
        return out, list(vecs)


def _penalized(cand: tuple[str, str]) -> bool:
    from app.agent.research import _LOW_VALUE

    return bool(_LOW_VALUE.search(cand[0]) or _LOW_VALUE.search(cand[1] or ""))


def default_scorer(mode: str = "auto") -> Scorer:
    """Use the app's configured providers when there are any; otherwise keyword scoring."""
    from app.config import get_settings

    mode = (mode or "auto").lower()
    if mode not in {"auto", "keywords", "embeddings", "llm"}:
        mode = "auto"
    if mode == "keywords" or not get_settings().openai_api_key:
        return Scorer(mode="keywords")
    from app.llm import chat_complete, embed_texts

    async def llm(prompt: str) -> str:
        resp = await chat_complete([{"role": "user", "content": prompt}], response_format={"type": "json_object"}, max_tokens=400)
        return resp.choices[0].message.content or ""

    return Scorer(embed=embed_texts, llm=llm, mode=mode)

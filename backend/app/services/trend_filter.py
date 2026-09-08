"""The cheap layer that decides what is worth an LLM call.

Google Trends is dominated by sport, television and celebrity news. Sending all
of it to a model would spend most of the budget proving that a cricket match is
not a startup story. This module answers that question for free.

Three outcomes, and the middle one matters most:

* ``HIGH_PRIORITY`` - a known startup/technology signal is present.
* ``POSSIBLE``      - nothing matched either way. **Not** a rejection: an
                      unknown company name looks exactly like this, so it goes
                      to the model to decide.
* ``LOW_PRIORITY``  - a known noise signal and nothing to offset it. Discarded.

The asymmetry is deliberate. A false LOW_PRIORITY silently loses a story, so
only an explicit noise match can produce one; absence of evidence produces
POSSIBLE instead.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from app.models.schemas import TrendPriority

log = logging.getLogger(__name__)

# Startup-ecosystem signals, grouped so a match can name a category without
# asking the model. Lowercase; matched on word boundaries.
STARTUP_KEYWORDS: dict[str, list[str]] = {
    "Artificial Intelligence": [
        "ai", "artificial intelligence", "machine learning", "llm", "chatgpt",
        "openai", "anthropic", "claude", "gemini", "copilot", "deepseek",
        "hugging face", "nvidia", "google ai", "microsoft ai", "agi",
        "ai agents", "generative ai", "perplexity", "midjourney",
    ],
    "Funding": [
        "funding", "funded", "raises", "raised", "seed round", "series a",
        "series b", "series c", "venture capital", "vc", "valuation",
        "unicorn", "ipo", "investment", "investor", "term sheet",
        "y combinator", "accelerator", "angel investor",
    ],
    "Startups": [
        "startup", "startups", "founder", "co-founder", "entrepreneur",
        "entrepreneurship", "bootstrapped", "incubator", "pitch deck",
    ],
    "Technology": [
        "technology", "tech", "software", "hardware", "developer", "api",
        "open source", "github", "programming", "semiconductor", "chip",
        "quantum", "robotics", "automation", "cloud", "data centre",
        "data center", "app launch", "product launch",
    ],
    "SaaS": ["saas", "b2b", "subscription", "platform", "enterprise software"],
    "Fintech": [
        "fintech", "payments", "upi", "neobank", "digital bank", "crypto",
        "bitcoin", "ethereum", "blockchain", "stablecoin", "trading app",
    ],
    "Business": [
        "acquisition", "acquires", "merger", "layoffs", "revenue", "earnings",
        "stock", "shares", "market cap", "e-commerce", "ecommerce", "economy",
        "ceo", "cfo", "expansion",
    ],
    "Sector Tech": [
        "healthtech", "edtech", "deeptech", "cybersecurity", "biotech",
        "climate tech", "agritech", "insurtech", "spacetech", "ev",
    ],
}

# Obvious noise for a startup newsletter. Only an explicit match here can
# produce LOW_PRIORITY.
NOISE_KEYWORDS: list[str] = [
    # sport
    "vs", "match", "score", "scorecard", "innings", "wicket", "goal",
    "fixtures", "highlights", "world cup", "premier league", "ipl", "nfl",
    "nba", "cricket", "football", "tennis", "olympics", "tournament",
    # entertainment
    "movie", "film", "trailer", "box office", "episode", "season finale",
    "netflix series", "bigg boss", "reality show", "tv show", "actor",
    "actress", "singer", "album", "concert", "celebrity", "gossip", "meme",
    "viral video", "birthday", "wedding", "divorce", "rumour", "rumor",
    "scandal", "arrested", "obituary", "died", "death",
    # other low-value
    "horoscope", "lottery", "powerball", "weather forecast", "recipe",
    "exam result", "admit card", "sarkari",
]


def _compile(words: list[str]) -> re.Pattern:
    """Word-boundary alternation, so 'ai' never matches inside 'said'."""
    escaped = sorted((re.escape(w) for w in words), key=len, reverse=True)
    return re.compile(r"(?<!\w)(?:" + "|".join(escaped) + r")(?!\w)", re.IGNORECASE)


_NOISE_RE = _compile(NOISE_KEYWORDS)
_CATEGORY_RES: dict[str, re.Pattern] = {
    category: _compile(words) for category, words in STARTUP_KEYWORDS.items()
}


def match_category(topic: str) -> Optional[str]:
    """The first startup category whose keywords appear in `topic`."""
    for category, pattern in _CATEGORY_RES.items():
        if pattern.search(topic):
            return category
    return None


def classify(topic: str) -> tuple[TrendPriority, Optional[str]]:
    """Classify one topic. Returns `(priority, matched_category)`.

    A startup keyword wins over a noise keyword: "Nvidia vs AMD" contains "vs"
    but is plainly a technology story.
    """
    text = (topic or "").strip()
    if not text:
        return TrendPriority.LOW_PRIORITY, None

    category = match_category(text)
    if category:
        return TrendPriority.HIGH_PRIORITY, category
    if _NOISE_RE.search(text):
        return TrendPriority.LOW_PRIORITY, None
    return TrendPriority.POSSIBLE, None


def prefilter(topics: list[str]) -> dict[str, tuple[TrendPriority, Optional[str]]]:
    """Classify many topics, and log what the stage saved."""
    verdicts = {topic: classify(topic) for topic in topics}

    counts = {priority: 0 for priority in TrendPriority}
    for priority, _ in verdicts.values():
        counts[priority] += 1

    log.info(
        "pre-filter: %d topics -> %d high, %d possible, %d discarded",
        len(topics),
        counts[TrendPriority.HIGH_PRIORITY],
        counts[TrendPriority.POSSIBLE],
        counts[TrendPriority.LOW_PRIORITY],
    )
    return verdicts

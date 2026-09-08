"""The trend provider interface.

Everything above this line in the stack - the service, the API, the tests -
speaks `BaseTrendProvider` and `TrendItem`. Nothing else knows that today's
source happens to be an RSS feed.

That indirection is the point: Google Trends has no stable public API, so the
implementation behind this interface is expected to change. When it does, only
one file changes with it.
"""

from __future__ import annotations

import abc
import logging

from app.models.schemas import TrendItem

log = logging.getLogger(__name__)


class TrendProviderError(Exception):
    """The provider could not deliver trends.

    Raised for a network failure, a bad HTTP status, or an unparseable
    response. The API layer turns this into a 503 - it is an upstream problem,
    not a client mistake.
    """


class BaseTrendProvider(abc.ABC):
    """One external source of trending topics."""

    name: str = "provider"

    @abc.abstractmethod
    async def fetch_trends(self, region: str, limit: int) -> list[TrendItem]:
        """Return up to `limit` normalised trends for `region`.

        Implementations must return `TrendItem`s and must raise
        `TrendProviderError` rather than leaking transport exceptions.

        Returning fewer than `limit` is normal and not an error: sources have
        as many trends as they have. Padding the list would be fabrication.
        """

    async def safe_fetch_trends(self, region: str, limit: int) -> list[TrendItem]:
        """Fetch, converting any failure into an empty list.

        For callers that aggregate several regions and would rather lose one
        of them than the whole response. Callers that need to distinguish
        "no trends" from "source down" must use `fetch_trends`.
        """
        try:
            return await self.fetch_trends(region, limit)
        except TrendProviderError as exc:
            log.warning("%s failed for region=%s: %s", self.name, region, exc)
            return []

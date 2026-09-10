"""The news provider interface.

The sibling of `BaseTrendProvider`: everything above this line speaks
`BaseNewsProvider` and `NewsArticle`, so Apify or any other source can be added
later without the ingestion pipeline learning about it.

A provider's whole job is *fetch and normalise*. It does not deduplicate,
validate or persist - those belong to the pipeline, which can see every
provider's output at once and is the only place able to compare them.
"""

from __future__ import annotations

import abc
import logging

from app.models.schemas import NewsArticle

log = logging.getLogger(__name__)


class NewsProviderError(Exception):
    """The provider could not deliver articles.

    A transport failure, a bad status, or an unparseable response. The API
    layer turns this into a 503 - it is upstream's problem, not the caller's.
    """


class BaseNewsProvider(abc.ABC):
    """One external source of news articles."""

    name: str = "news_provider"
    #: Whether using this source costs money. The pipeline runs free sources
    #: first and only tops up from metered ones when it came up short.
    is_metered: bool = False

    @abc.abstractmethod
    async def fetch(self, query: str, region: str, limit: int) -> list[NewsArticle]:
        """Return up to `limit` normalised articles matching `query`.

        Implementations must raise `NewsProviderError` rather than leaking
        transport exceptions. Returning fewer than `limit` is normal.
        """

    async def safe_fetch(self, query: str, region: str, limit: int) -> list[NewsArticle]:
        """Fetch, turning any provider failure into an empty list.

        For the pipeline, which queries many topics and would rather lose one
        than the whole run.
        """
        try:
            return await self.fetch(query, region, limit)
        except NewsProviderError as exc:
            log.warning("%s failed for %r: %s", self.name, query, exc)
            return []

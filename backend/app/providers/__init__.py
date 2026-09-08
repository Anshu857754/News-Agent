"""Trend providers.

External data sources live here and nowhere else. The rest of the application
depends on `BaseTrendProvider`, never on a specific feed or scraper, so a
source can be swapped without touching the service layer.
"""

from app.providers.base import BaseTrendProvider, TrendProviderError
from app.providers.google_trends import GoogleTrendsProvider

__all__ = ["BaseTrendProvider", "TrendProviderError", "GoogleTrendsProvider"]

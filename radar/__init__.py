"""Evidence-first Radar Low-Ticket MVP built on MetaAdsCollector."""

from .models import RadarConfig, RadarReport
from .pipeline import analyze_ads, collect_and_analyze

__all__ = ["RadarConfig", "RadarReport", "analyze_ads", "collect_and_analyze"]

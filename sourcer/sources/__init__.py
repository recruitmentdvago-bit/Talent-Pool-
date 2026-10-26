"""Source registry. `sources.enabled` in settings.yaml decides what runs."""
from __future__ import annotations

from ..config import cfg
from .base import BaseSource, FetchContext, FetchResult, Source
from .csv_source import CsvSource
from .linkedin_browser import LinkedInBrowserSource
from .linkedin_talent import LinkedInTalentSource
from .pdf_inbox import PdfInboxSource
from .provider import ProviderSource, ProxycurlSource

REGISTRY: dict[str, type] = {
    "pdf_inbox": PdfInboxSource,
    "csv": CsvSource,
    "provider": ProviderSource,
    "proxycurl": ProxycurlSource,
    "linkedin_talent": LinkedInTalentSource,
    "linkedin_browser": LinkedInBrowserSource,
}

__all__ = ["REGISTRY", "BaseSource", "FetchContext", "FetchResult", "Source",
           "build_enabled", "get"]


def get(name: str) -> BaseSource:
    if name not in REGISTRY:
        raise KeyError(f"unknown source '{name}'. known: {', '.join(sorted(REGISTRY))}")
    return REGISTRY[name]()


def build_enabled(names: list[str] | None = None) -> list[BaseSource]:
    names = names or list(cfg().get("sources.enabled", ["pdf_inbox", "csv"]))
    out = []
    for n in names:
        try:
            out.append(get(n))
        except Exception:
            continue
    return out

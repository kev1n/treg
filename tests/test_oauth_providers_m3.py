"""Milestone 3 — the rest of the Google family, Slack, and X.

X is the interesting one: it rejects an authorization code exchanged without a PKCE verifier, and
rejects the client secret in the request body. Both quirks are captured on the pending connect at
start time so the callback exchanges the code exactly the way the consent URL was built.
"""

from __future__ import annotations

import json
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import pytest
from httpx import AsyncClient
from sqlmodel import select

from treg import oauth
from treg import oauth_providers as P
from treg.config import get_settings
from treg.infra.db import session_maker
from treg.models import PendingOAuth


@pytest.fixture
def all_apps(monkeypatch):
    for k in ("GOOGLE", "SLACK", "X", "TIKTOK"):
        monkeypatch.setenv(f"TREG_{k}_CLIENT_ID", f"{k.lower()}-cid")
        monkeypatch.setenv(f"TREG_{k}_CLIENT_SECRET", f"{k.lower()}-csec")
    monkeypatch.setenv("TREG_META_CLIENT_ID", "meta-cid")
    monkeypatch.setenv("TREG_META_CLIENT_SECRET", "meta-csec")
    monkeypatch.setenv("TREG_INSTAGRAM_CLIENT_ID", "instagram-cid")
    monkeypatch.setenv("TREG_INSTAGRAM_CLIENT_SECRET", "instagram-csec")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _q(payload: dict) -> dict:
    return parse_qs(urlsplit(payload["consent_url"]).query)


# ---- registry shape ----------------------------------------------------------------------
def test_every_provider_is_registered():
    assert set(P.REGISTRY) == {
        "google-search-console", "google-analytics", "google-business-profile", "google-tag-manager",
        "google-ads", "youtube", "linkedin", "slack", "x", "tiktok",
        "facebook", "instagram", "meta-ads",
        # API-key providers (auth_kind="key")
        "apollo", "pdl", "akta", "hunter", "crunchbase", "tikhub", "brightdata", "semrush", "justoneapi",
        "scrapecreators",
        "dataforseo", "seranking", "moz", "majestic", "serpstat", "exa",
        "cloro",
        "lusha", "coresignal", "diffbot", "thecompaniesapi", "leadmagic", "fiber-ai",
        "companyenrich", "oceanio", "tomba", "predictleads", "findymail", "branddev",
        "icypeas", "leadsforge", "influencersclub", "crustdata", "aviato", "millionverifier",
        "spyfu", "apify", "meta-ad-library", "serpapi",
        "coingecko", "polygon", "finnhub", "twelvedata", "fmp", "eodhd", "marketstack", "tiingo",
        "microsoft-ads", "snapchat-ads", "tiktok-ads", "pinterest-ads",
        # BYOK token providers
        "minimax", "openrouter", "replicate",
    }

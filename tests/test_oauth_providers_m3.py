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
        "icypeas", "leadsforge", "influencersclub", "crustdata", "aviato", "contactout",
        "spyfu", "apify", "meta-ad-library", "serpapi",
        "coingecko", "polygon", "finnhub", "twelvedata", "fmp", "eodhd", "marketstack", "tiingo",
        "microsoft-ads", "snapchat-ads", "tiktok-ads", "pinterest-ads",
        # BYOK token providers
        "minimax", "openrouter", "replicate",
    }


def test_default_capability_is_the_broadest():
    """Connect asks for the fullest capability; a narrower one is chosen up front, not bolted on
    afterwards. Every provider's write must be a superset of its read for that to be safe."""
    assert P.GOOGLE_SEARCH_CONSOLE.default_capability == "write"
    assert P.X.default_capability == "write"
    assert P.GOOGLE_ADS.default_capability == "manage"  # it has no read-only mode
    assert P.GOOGLE_TAG_MANAGER.default_capability == "manage"
    for provider in P.REGISTRY.values():
        caps = provider.capabilities
        if "read" in caps and "write" in caps:
            assert set(provider.scopes["read"]) < set(provider.scopes["write"]), provider.service


def test_google_ads_refuses_to_autoprovision():
    """Ads needs a developer-token header too; a bearer-only tool would 401 on first use."""
    assert P.GOOGLE_ADS.can_autoprovision is False
    assert "developer-token" in P.GOOGLE_ADS.extra_credential_note
    assert P.GOOGLE_SEARCH_CONSOLE.can_autoprovision is True


def test_x_write_keeps_offline_access():
    """Without offline.access the token can't be refreshed and every X connection becomes a
    manual-reconnect chore within hours."""
    assert "offline.access" in P.X.scopes_for("write")
    assert "offline.access" in P.X.scopes_for("read")

"""MillionVerifier is registered as a key provider."""

from treg import oauth_providers as P


def test_millionverifier_is_registered():
    assert "millionverifier" in P.REGISTRY
    p = P.REGISTRY["millionverifier"]
    assert p.auth_kind == "key"
    assert p.token_location == "query"
    assert p.token_param == "api"
    assert p.probe_path == "/api/v3/credits"
    assert p.token_reject_field == "error"

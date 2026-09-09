"""Deterministic config sampling and best-effort streaming invitations."""
from types import SimpleNamespace
import hashlib

import pytest
from pydantic import ValidationError

from treg import hints
from treg.config import Settings, get_settings


@pytest.mark.parametrize('kind,field', [('review', 'review_sample_rate'), ('feedback', 'feedback_hint_rate')])
def test_sampling_boundaries_stability_and_salt(monkeypatch, kind, field):
    settings = get_settings()
    monkeypatch.setattr(settings, field, 0)
    assert not hints.sampled(kind, 'call')
    monkeypatch.setattr(settings, field, 1)
    assert hints.sampled(kind, 'call')
    monkeypatch.setattr(settings, field, 0.5)
    for i in range(100):
        ref = str(i)
        expected = int.from_bytes(hashlib.sha256(f'{kind}:{ref}'.encode()).digest()[:8], 'big') < 2**63
        assert hints.sampled(kind, ref) == expected
        assert hints.sampled(kind, ref) == expected


@pytest.mark.parametrize('field', ['review_sample_rate', 'feedback_hint_rate'])
@pytest.mark.parametrize('rate', [-0.1, 1.1, float('nan'), float('inf')])
def test_invalid_rates(field, rate):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: rate})


@pytest.mark.parametrize('catalog,status,headers,sample,expected', [
    ('direct', 200, {}, True, True), ('routed', 201, {}, True, False),
    ('credential', 200, {}, True, False), ('tool', 200, {}, True, False),
    ('own', 200, {}, True, False), ('direct', 200, {}, False, False),
    ('direct', 199, {}, True, False), ('direct', 300, {}, True, False),
    ('direct', 503, {}, True, False),
    ('direct', 200, {'X-Treg-Idempotent-Replay': 'true'}, True, False),
    ('cached', 200, {}, True, False),
    ('direct', 200, {}, 'exception', False),
])
async def test_header_before_stream_without_changing_response(
    clients, monkeypatch, catalog, status, headers, sample, expected,
):
    from treg.routers import call
    from treg.application.call.types import UpstreamResponse

    consumed = []
    async def stream():
        consumed.append('body')
        yield b'exact upstream bytes'
    async def close():
        consumed.append('close')
    async def execute(context, client):
        if catalog in ('direct', 'cached', 'credential', 'tool'):
            context.cached = catalog == 'cached'
            context.marketplace = SimpleNamespace(
                endpoint_id='example.search',
                tier=catalog if catalog in ('credential', 'tool') else 'platform',
            )
        elif catalog == 'own':
            context.target = SimpleNamespace(tool=SimpleNamespace(name='example.search'))
        return UpstreamResponse(status, tuple((k.lower().encode(), v.encode()) for k, v in headers.items()),
                                stream(), close)
    def sampled(kind, ref):
        assert consumed == []
        assert kind == 'review' and ref
        if sample == 'exception':
            raise RuntimeError('optional hook broke')
        return sample
    monkeypatch.setattr(call, 'execute_call', execute)
    monkeypatch.setattr(call.catalog_store, 'load', lambda: SimpleNamespace(by_id={'example.search': {}}))
    monkeypatch.setattr(hints, 'sampled', sampled)
    response = await clients.get('/call/example.search')
    assert response.status_code == status
    assert response.content == b'exact upstream bytes'
    assert (response.headers.get('X-Treg-Review') == 'requested') is expected
    assert consumed == ['body', 'close']

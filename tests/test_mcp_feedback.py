"""Flag failures must never delay or change a tool result."""
import asyncio
import json
import time

import httpx
import pytest

from treg import mcp_feedback as hints
from treg.config import get_settings


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv('TREG_POSTHOG_KEY', 'synthetic-project-key')
    get_settings.cache_clear()
    monkeypatch.setattr(hints, '_rate', 0.0)
    monkeypatch.setattr(hints, '_updated_at', 0.0)
    yield
    get_settings.cache_clear()


@pytest.mark.parametrize('payload', [{'sample_rate': 0.01}, '{"sample_rate":0.01}'])
async def test_flag_payload_refresh_and_disable(configured, payload):
    enabled = True
    def handler(request):
        assert request.url.path == '/flags'
        assert json.loads(request.content) == {
            'api_key': 'synthetic-project-key', 'distinct_id': hints.DISTINCT_ID,
        }
        return httpx.Response(200, json={'flags': {hints.FLAG: {
            'enabled': enabled, 'metadata': {'payload': payload},
        }}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await hints.refresh(client)
        assert hints._rate == 0.01
        enabled = False
        await hints.refresh(client)
        assert hints._rate == 0


@pytest.mark.parametrize('doc', [
    {}, None, {'flags': None}, {'errorsWhileComputingFlags': True},
    {'quotaLimited': ['feature_flags']},
    *[{'flags': {hints.FLAG: {'enabled': True, 'metadata': {'payload': p}}}}
      for p in [None, 'invalid', {}, {'sample_rate': True}, {'sample_rate': -1},
                {'sample_rate': 2}, {'sample_rate': '0.01'}, {'sample_rate': float('inf')}]],
])
async def test_invalid_config_disables_previous_value(configured, monkeypatch, doc):
    monkeypatch.setattr(hints, '_rate', 1.0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, content=json.dumps(doc))
    )) as client:
        await hints.refresh(client)
    assert hints._rate == 0


async def test_network_failure_disables_hints(configured, monkeypatch):
    monkeypatch.setattr(hints, '_rate', 1.0)
    def fail(_):
        raise httpx.ReadTimeout('timeout')
    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
        await hints.refresh(client)
    assert hints._rate == 0


def test_sampling_is_stable_and_stale_config_is_off(configured, monkeypatch):
    monkeypatch.setattr(hints, '_rate', 0.01)
    monkeypatch.setattr(hints, '_updated_at', time.monotonic())
    first = [hints.sampled(str(i)) for i in range(10000)]
    assert first == [hints.sampled(str(i)) for i in range(10000)]
    assert 60 < sum(first) < 140
    monkeypatch.setattr(hints, '_updated_at', time.monotonic() - hints.MAX_AGE_SECONDS - 1)
    assert not any(hints.sampled(str(i)) for i in range(100))


async def test_lifespans_share_poller_and_await_shutdown(configured, monkeypatch):
    started, stopped = asyncio.Event(), asyncio.Event()
    async def poll():
        started.set()
        try:
            await asyncio.Future()
        finally:
            stopped.set()
    monkeypatch.setattr(hints, '_poll', poll)
    async with hints.lifespan():
        await started.wait()
        task = hints._task
        async with hints.lifespan():
            assert hints._task is task
        assert not stopped.is_set()
    assert stopped.is_set()
    assert hints._task is None
    assert hints._rate == 0

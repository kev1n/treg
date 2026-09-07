"""Global retention without breaking live paid-response replay."""
import pytest
from sqlalchemy import delete, event, func
from sqlmodel import select

from treg.application.call import idempotency
from treg.infra.db import session_maker
from treg.models import IdempotentCall
from test_marketplace_call import EP, _balance, _seed_answer, platform_on  # noqa: F401


async def test_cleanup_preserves_live_replay_and_pending_claims(clients, platform_on):
    for n in range(5):
        await _seed_answer(clients, f"expired-{n}", ttl_s=-3600)
    await _seed_answer(clients, "valid", body=b'{"saved":true}')
    await _seed_answer(clients, "pending", status="pending")
    await _seed_answer(clients, "expired-pending", status="pending", ttl_s=-3600)
    dry = await idempotency.prune_expired_idempotency(dry_run=True)
    assert dry.eligible == 5 and dry.deleted == 0
    result = await idempotency.prune_expired_idempotency(batch_size=2, pause_s=0)
    assert (result.deleted, result.batches, result.complete) == (5, 4, True)
    async with session_maker() as db:
        assert set((await db.scalars(select(IdempotentCall.key))).all()) == {
            "valid", "pending", "expired-pending"}
    before = await _balance(clients)
    replay = await clients.get(f"/call/{EP}?aweme_id=7", headers={"Idempotency-Key": "valid"})
    assert replay.status_code == 200 and replay.json() == {"saved": True}
    assert replay.headers["X-Treg-Idempotent-Replay"] == "true"
    assert await _balance(clients) == before
    pending = await clients.get(f"/call/{EP}?aweme_id=7", headers={"Idempotency-Key": "pending"})
    assert pending.status_code == 409
    assert (await idempotency.prune_expired_idempotency(pause_s=0)).deleted == 0


async def test_bounded_sweep_resumes_next_run(clients):
    for n in range(5):
        await _seed_answer(clients, f"expired-{n}", ttl_s=-3600)
    result = await idempotency.prune_expired_idempotency(batch_size=2, max_batches=1, pause_s=0)
    assert (result.deleted, result.complete) == (2, False)
    result = await idempotency.prune_expired_idempotency(batch_size=2, pause_s=0)
    assert (result.deleted, result.complete) == (3, True)


async def test_concurrent_cleanup_and_new_rows_do_not_extend_sweep(clients, monkeypatch):
    ids = [await _seed_answer(clients, f"old-{n}", ttl_s=-3600) for n in range(4)]
    original_sleep = idempotency.asyncio.sleep
    first = True

    async def interleave(seconds):
        nonlocal first
        if first:
            first = False
            # Simulate the existing caller-scoped cleanup between committed batches.
            async with session_maker() as db:
                await db.execute(delete(IdempotentCall).where(IdempotentCall.id == ids[2]))
                await db.commit()
            await _seed_answer(clients, "inserted-later", ttl_s=-3600)
        await original_sleep(0)

    monkeypatch.setattr(idempotency.asyncio, "sleep", interleave)
    result = await idempotency.prune_expired_idempotency(batch_size=2, pause_s=0)
    assert result.deleted == 3 and result.complete is True
    async with session_maker() as db:
        assert list((await db.scalars(select(IdempotentCall.key))).all()) == ["inserted-later"]


async def test_lock_timeout_rolls_back_batch(clients):
    async with session_maker() as db:
        if db.bind.dialect.name != "postgresql":
            pytest.skip("Postgres row lock behavior")
    row_id = await _seed_answer(clients, "locked-expired", ttl_s=-3600)
    async with session_maker() as holder:
        await holder.execute(select(IdempotentCall).where(IdempotentCall.id == row_id).with_for_update())
        from sqlalchemy.exc import DBAPIError
        with pytest.raises(DBAPIError):
            await idempotency.prune_expired_idempotency(pause_s=0)
        await holder.rollback()
    result = await idempotency.prune_expired_idempotency(pause_s=0)
    assert result.deleted == 1


async def test_live_pages_do_not_stop_the_expiry_sweep(clients):
    for n in range(4):
        await _seed_answer(clients, f"live-{n}")
    await _seed_answer(clients, "expired-tail", ttl_s=-3600)
    result = await idempotency.prune_expired_idempotency(batch_size=2, pause_s=0)
    assert (result.deleted, result.batches, result.complete) == (1, 3, True)


async def test_counts_accumulate_from_page_metadata_not_aggregate_queries(clients):
    """Regression test for production timeout: counts must not require unbounded scans.

    The prune worker hit statement_timeout twice in production:
    1. First on the initial SELECT with expiry filter + LIMIT (fixed by ID-first pages).
    2. Then on the final COUNT(*) WHERE expired (fixed by accumulating within pages).

    This test verifies that eligible/deleted counts are derived from the bounded
    ID-page iteration, not from separate aggregate queries. The function must
    complete without issuing any COUNT(*) WHERE status='done' AND expires_at<cutoff.

    Contract: `eligible` = sum of expired rows found per metadata page;
    `complete` = whether the fixed upper-ID traversal finished.
    """
    for n in range(6):
        await _seed_answer(clients, f"expired-{n}", ttl_s=-3600)
    for n in range(4):
        await _seed_answer(clients, f"live-{n}")

    async with session_maker() as db:
        engine = db.bind.sync_engine

    def reject_unbounded_count(conn, cursor, statement, parameters, context, executemany):
        sql = statement.lower()
        assert not ("count(" in sql and "idempotentcall" in sql), "unbounded retention COUNT"

    event.listen(engine, "before_cursor_execute", reject_unbounded_count)
    try:
        result = await idempotency.prune_expired_idempotency(batch_size=3, pause_s=0)
    finally:
        event.remove(engine, "before_cursor_execute", reject_unbounded_count)

    assert result.eligible == 6, "eligible must be accumulated from page metadata"
    assert result.deleted == 6, "all eligible rows should be deleted"
    assert result.complete is True, "traversal should complete"

    async with session_maker() as db:
        remaining = (await db.scalars(select(IdempotentCall.key))).all()
    assert set(remaining) == {f"live-{n}" for n in range(4)}


async def test_partial_sweep_exits_nonzero_without_final_count(clients, capsys, monkeypatch):
    """A bounded partial sweep must not attempt a final aggregate count.

    When max_batches is reached before traversal completes, the function returns
    complete=False. The worker exits 1 so the next run continues. This must happen
    without any full-table COUNT that could timeout and turn a successful partial
    sweep into a failed run.
    """
    for n in range(10):
        await _seed_answer(clients, f"expired-{n}", ttl_s=-3600)

    import json
    from types import SimpleNamespace
    from treg.worker import _idempotency_prune
    from cryptography.fernet import Fernet
    from treg.config import get_settings

    # The real worker verifies production key configuration, including on Postgres CI.
    monkeypatch.setenv("TREG_SECRET_KEY", Fernet.generate_key().decode())
    get_settings.cache_clear()
    exit_code = await _idempotency_prune(SimpleNamespace(
        batch_size=2, max_batches=2, pause_seconds=0, dry_run=False,
    ))
    result = SimpleNamespace(**json.loads(capsys.readouterr().out.splitlines()[-1]))
    assert exit_code == 1


    assert result.deleted == 4, "should delete 2 batches × 2 rows"
    assert result.complete is False, "traversal incomplete"
    assert result.batches == 2, "stopped at max_batches"

    async with session_maker() as db:
        count = await db.scalar(select(func.count()).select_from(IdempotentCall))
    assert count == 6, "remaining rows from incomplete sweep"

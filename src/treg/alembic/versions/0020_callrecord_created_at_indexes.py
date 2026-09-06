"""composite (endpoint_id, created_at) and (org_id, created_at) on callrecord — time windows stop
reading whole histories

Revision ID: 0020
Revises: 0019
Create Date: 2026-09-06

Every question asked of `callrecord` is "… since <time>", and no index carried `created_at`. The
planner therefore chose an index for the OTHER column and filtered the date in memory, reading an
endpoint's or an org's entire history to answer a 30-day question. Measured on prod 2026-09-06 at
2.94M rows / 1.68 GB:

    ix_callrecord_endpoint_id_id   570,130 scans   1,603,968,175 tuples read
    ix_callrecord_org_id            70,003 scans     295,077,614 tuples read
    callrecord (sequential)          80,932 scans  27,063,404,479 tuples read

The first is the catalog observation refresh (`domain/catalog/stats.py`, WINDOW_DAYS = 30), the
second the per-member daily counts (`routers/orgs.py`). Both become tight range scans with these
pairs.

This is the fix for the API-pool saturation, not a pool size: the three pools bulkhead CONNECTIONS,
not the single database's CPU, so a scan of this table makes every ordinary 3 ms request query queue
behind it until `pool_timeout` fires and callers get `503 treg_saturated`.

Built CONCURRENTLY on Postgres, exactly as 0016 did on the same table: the preDeploy step runs while
the old build still serves traffic, and a concurrent build never blocks writes to the hot audit
table. `IF NOT EXISTS` is not used deliberately — a build killed mid-flight leaves an INVALID index
that `IF NOT EXISTS` would then silently skip, leaving the scan in place with nothing failing. A
second run failing loudly on "already exists" is the outcome that gets looked at; drop the invalid
index and re-run. SQLite (tests, local) builds plainly — no concurrent mode, no traffic to block.

The expand-safety linter counts the autocommit escape (get_bind/get_context) as non-additive, so
this revision declares a rollback floor pro forma, as 0016 did: the operations themselves are purely
additive — two indexes — and downgrading past it merely drops them.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0020"
down_revision: str | Sequence[str] | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
contract = True  # pro forma — see the rollback floor note; the operations are additive indexes

_INDEXES = (
    ("ix_callrecord_endpoint_id_created_at", ["endpoint_id", "created_at"]),
    ("ix_callrecord_org_id_created_at", ["org_id", "created_at"]),
)


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        # CONCURRENTLY cannot run inside a transaction; alembic opens one by default.
        with op.get_context().autocommit_block():
            for name, columns in _INDEXES:
                op.create_index(name, "callrecord", columns, postgresql_concurrently=True)
    else:
        for name, columns in _INDEXES:
            op.create_index(name, "callrecord", columns)


def downgrade() -> None:
    for name, _ in _INDEXES:
        op.drop_index(name, table_name="callrecord")

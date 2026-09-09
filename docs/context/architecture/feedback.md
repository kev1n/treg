---
title: Feedback - private intake for problems and suggestions
status: shipped
sources:
  - src/treg/feedback_contract.py
  - src/treg/domain/feedback/__init__.py
  - src/treg/domain/feedback/reports.py
  - src/treg/domain/feedback/reviews.py
  - src/treg/hints.py
  - src/treg/config.py
  - src/treg/routers/call.py
  - src/treg/application/feedback.py
  - src/treg/routers/feedback.py
  - src/treg/alembic/versions/0025_feedback.py
  - src/treg/alembic/versions/0026_callreview.py
  - src/treg/web/feedback.md
  - tests/test_feedback.py
  - tests/test_reviews.py
  - tests/test_hints.py
related:
  - architecture/data-model.md
  - architecture/mcp-oauth.md
  - architecture/super-admin.md
  - interface/skill.md
  - interface/cli.md
---

# Feedback

`FeedbackCategory` is the shared four-value vocabulary: `quality`, `pricing`, `friction`, `other`.
`FeedbackIn` accepts only `category`, `message` (trimmed, 1-2,000 characters), optional `call_ids`
(at most 100 bounded opaque references, deduplicated), and optional public `endpoint_id`.
Guidance encourages proactive reports of small annoyances and observed friction even after successful workarounds, without requiring
a proven bug. It directs agents to pass references in `call_ids` (CLI `--call-id`), not only prose,
and to continue the task after reporting an issue once. Extra fields are rejected. Privacy instructions ask callers to replace sensitive values and omit
raw payloads; free-text content is not guaranteed anonymous or automatically sanitized.

`POST /feedback` uses `require_member`, including agent identities and the existing public-demo
write restriction. `application.feedback.submit` commits the report and a per-team rate-limit
hit in one transaction before acknowledging HTTP 201 with `feedback_id` and `status: received`.
The intake allows 30 reports per team per hour. It spends no balance, calls no provider and uses
no best-effort audit writer. Storage failures cannot produce a success acknowledgement.

`call_ids` and `endpoint_id` remain submitted claims. `verified_call_ids` is the subset found in
the submitting team's `CallRecord.call_ref` or `LedgerEntry.call_id`; no cross-team lookup runs.
Missing or delayed audit records do not reject a report. Verified provenance does not establish
that the reported problem is true. Intake does not rank providers or adjust charges.

`GET /feedback/{feedback_id}` returns the report only to its team; other teams receive 404.
`GET /admin/feedback` uses `require_superadmin` and the admin pool, with category filtering and
bounded descending-ID pagination (`limit`, `before`, `next_before`). It returns internal
attribution too. There is no external notification, issue sync, or public feed.
`Feedback` participates in `ORG_SCOPED_MODELS`, so team deletion removes its reports.

CLI `cmd_feedback` sends the same payload to its configured registry, reading a prepared message
from stdin when the message argument is `-` (a terminal is rejected instead of blocking).
`cmd_feedback_get` retrieves a report through the same team-scoped HTTP read. The CLI rejects
empty or oversized messages locally and emits structured errors without echoing rejected input;
transport failures leave submission outcomes explicitly unconfirmed. Both MCP surfaces expose `feedback` with an enum in
their input schema, relay to the same HTTP intake, and declare a non-destructive, non-idempotent
local write. Their existing call permissions and transport boundaries remain distinct.

`skill.md` mentions feedback in its description and links to `{BASE}/feedback.md`, served by
`feedback_md` with the deployment's base URL. Detailed syntax and privacy guidance live in that
one document; CLI help and MCP share `FEEDBACK_DESCRIPTION`. The plugin generator propagates the
short skill instructions to each installation format. Self-hosted submissions stay on the
configured registry.

## Call review storage

`CallReview` (`callreview`, revision 0026) stores one rating per unique `call_id`, with team and
caller identity, server-attributed endpoint/provider, optional `routed_via`, `invited`, request
`client`, usefulness, optional reason and creation time. Endpoint/time has a composite index.
`ORG_SCOPED_MODELS` includes reviews for team deletion. Reviews never create feedback reports.
`ReviewUsefulness` is `useful`, `partly`, `not_useful`, or `not_sure`; shared guidance asks agents
to rate after using the result and continue their task.


`application.feedback.submit_review` owns one transaction. It looks up call references only in
its caller's team audit records; a missing audit record receives a retryable 404, including
ledger-only evidence, which lacks status/provider/cache attribution. Own-tool records receive
400. A routed parent uses its successful child's endpoint/provider when present, retaining the
parent endpoint as `routed_via`; otherwise it retains parent attribution. `invited` is recomputed
from a 2xx, non-cached record with `credential_tier == "platform"` and the current review
sampling rate. Routed and own-key catalog calls can still be reviewed uninvited. Every agent-facing
text says one review per invitation: volunteered reviews are accepted and labelled `invited=false`,
but they are not requested, and a future score must use invited rows only (an agent that reviews
every call of a batch, seen in production on launch day, would otherwise weigh as much as a team). Retries return
the original ID and `already_reviewed`; a unique index also arbitrates concurrent submissions. Its savepoint
stays open until the application commit, avoiding SQLite deferred-BEGIN early commits. The sole writer
is `domain.feedback.reviews`; the moved `reports` module preserves feedback behavior.

`hints.sampled(kind, sample_id)` hashes `kind:sample_id` with SHA-256 into the same 64-bit bucket
construction for both kinds. `TREG_REVIEW_SAMPLE_RATE` and `TREG_FEEDBACK_HINT_RATE` are bounded
0..1 floats, default 0. Sampling is local and deterministic under the current configuration.

`POST /reviews` uses `require_member` and returns 201 (`review_id`, `status: received`) on
insertion or 200 on retry. `ReviewIn` rejects extra fields, requires a bounded `CallReference`
and usefulness enum, and trims an optional 1-200 character reason with feedback's privacy rules.
`GET /admin/reviews` is superadmin-only, uses the admin pool, and provides bounded descending-ID
pagination with optional `endpoint_id`. It is excluded from OpenAPI; there is no team read route.

## Optional review and feedback invitations

`routers.call.call_tool` sets `X-Treg-Review: requested` after constructing the streaming response,
before streaming starts. Phase 1 invites only direct catalog calls served on treg's own platform
key (`context.marketplace` exists and its `tier` is `platform`). These calls qualify only with a
2xx status, no idempotent-replay header, `context.cached == False`, and a sampled call reference.
The service sets `context.cached` from `served_hit` when the archive answers; the hook does not
depend on cache response headers. Routed parents and own-key catalog calls can still be reviewed
uninvited. An own tool never qualifies, even if its name matches a catalog endpoint. The whole hook
is best-effort, has no database or body access, and does not change call service exits or writes.
Plain HTTP gets only the header. Both MCP transports retain `call_id` and use their single hint
slot with priority replay > 402 > review > feedback. Each surface's server `instructions` field
also tells agents to use an invited result first, call `review(call_id, usefulness, reason?)`,
and keep going with the task. Review invites rating after use; feedback remains the existing
proactive-friction text. The upstream body is unchanged.

The config-driven sampler replaces the PostHog flag poller completely; both MCP lifespans only
own their transport lifecycle. Feedback hints remain limited to successful calls without a higher
priority hint; missing call references use a fresh sampling ID without inventing a public call ID.
`mcp_hint_attached` is a best-effort analytics event with `kind`, `surface` and available `call_id`,
never upstream contents or credentials. Attachment does not prove display or reading. No session
reminder cap or adaptive sampling is implemented.

## Known biases

Models lean toward `useful`, ratings often precede actual use despite the instructions, and
models differ in how they use the scale. A score is only meaningful when comparing sibling
endpoints of one capability. Phase 1 collects only: no aggregation, catalog scores, ranking,
team-side read route, dashboard, or adaptive per-endpoint sampling.

## Response-rate query

For PostgreSQL, bind `:window_start`, `:window_end`, `:as_of` as naive UTC timestamps and
`:review_rate` to the configured rate for that window. Split windows when the rate changes:
`invited` reflects the rate at submission, so historical rate changes cannot be reconstructed
from that flag alone. The first audit row per org/reference forms the call cohort. Child refs
contain `:` and are excluded; replays do not write a new call record. Audit is best-effort, so
this denominator is observed eligible calls, not proof every invitation was displayed.

The numerator is invited review rows for that cohort submitted by `:as_of`, grouped by the
original call's `client` (the review request may use a different runtime). Keep the reporting
cutoff explicit to allow delayed ratings. This response rate is the decision input for phase 2.

```sql
WITH calls AS (
  SELECT DISTINCT ON (org_id, call_ref) org_id, call_ref, client
  FROM callrecord
  WHERE created_at >= :window_start AND created_at < :window_end
    AND endpoint_id IS NOT NULL AND credential_tier = 'platform'
    AND status_code >= 200 AND status_code < 300
    AND NOT cached AND call_ref ~ '^[A-Za-z0-9_-]+$'
  ORDER BY org_id, call_ref, id
), hashed AS (
  SELECT calls.*, ('x' || substr(encode(sha256(convert_to('review:' || call_ref,
    'UTF8')), 'hex'), 1, 16))::bit(64)::bigint AS signed_bucket
  FROM calls
), invited_calls AS (
  SELECT * FROM hashed
  WHERE (CASE WHEN signed_bucket < 0
    THEN signed_bucket::numeric + 18446744073709551616
    ELSE signed_bucket::numeric END) < :review_rate * 18446744073709551616
)
SELECT c.client, count(*) AS invited_calls, count(r.id) AS invited_reviews,
       count(r.id)::numeric / NULLIF(count(*), 0) AS response_rate
FROM invited_calls c
LEFT JOIN callreview r ON r.org_id = c.org_id AND r.call_id = c.call_ref
  AND r.invited AND r.created_at <= :as_of
GROUP BY c.client ORDER BY c.client;
```

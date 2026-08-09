# ADR-004: Explicit User and Household Isolation Model

## Problem

Hermes serves multiple users and households through transport sessions,
durable SQLite state, background retrieval, and Qdrant. Reusing an untrusted
transport identifier or omitting a scope predicate can disclose or mutate
another tenant's data.

## Context

Memory OS v2 added strict `user_id` isolation in commit
`40bd8146939493c73c0eecc8d2a6c816ac435454`. Later household-domain decisions
separated Telegram identities, application users, household membership, and
household-owned resources. Telegram callback hardening also established that a
button action belongs to the session and user that created it, not merely to a
caller-supplied identifier.

The system therefore has several legitimate scopes. A personal fact belongs to
an application user. A weekly plan or shopping resource can belong to a
household and require active membership. A Telegram chat or callback identifies
a request channel, but is not itself durable authorization.

## Decision

Make authorization scope explicit at every boundary.

- Resolve transport identity into the normalized application user before
  accessing durable state.
- Treat raw Telegram IDs, chat IDs, callback payload IDs, model-provided IDs,
  and Qdrant payloads as claims, not authority.
- Require user-scoped SQLite predicates for personal state.
- Require verified household membership for household-scoped reads and writes.
- Filter Qdrant queries by normalized `user_id`, then rehydrate every hit from
  SQLite under the same ownership predicate.
- Bind stateful Telegram callbacks to the originating user and session, and
  reject stale or cross-session actions.
- Keep logs and evidence free of raw identifiers and personal or medical data.

## Alternatives Considered

- **Use Telegram user ID as the primary durable identity.** Rejected because a
  transport identifier is not the domain model and does not represent household
  membership or future platforms.
- **Trust identifiers supplied by tools, callbacks, or the model.** Rejected
  because caller-controlled scope is an authorization bypass.
- **Trust Qdrant payload ownership without SQLite revalidation.** Rejected
  because Qdrant is derived and can be stale.
- **Use one global query and filter after retrieval.** Rejected because data can
  cross the isolation boundary before authorization.
- **Share FSM state across users in a chat.** Rejected because callbacks and
  pending actions can be replayed by the wrong participant.

## Consequences (+ and -)

**Positive**

- Tenant isolation is enforced consistently across transport, SQL, and vector
  retrieval.
- Household sharing is explicit rather than inferred from chat context.
- Derived-index drift cannot by itself expose another user's durable fact.
- The model and tools cannot elevate caller-supplied identifiers into authority.

**Negative**

- Features need explicit identity resolution, membership checks, and scoped
  queries.
- Tests must cover cross-user, cross-household, stale-session, and payload
  mismatch cases.
- Background and reconciliation jobs must carry ownership context throughout
  the operation.

# Architecture & Design Decisions

## Overview

**Arcfield** is a durable game economy service that manages player wallets, item purchases, and one-time reward claims. The cardinal rule: **never lose or duplicate a player's money or items**, even under concurrent requests, retries, and hard crashes (`kill -9`).

---

## Technology Choices

### Language: Python 3.12

**Why:** Python is the language I'm most productive in for building HTTP services quickly with high readability. Python 3.12 brings meaningful performance improvements (PEP 684 per-interpreter GIL, faster startup) and is the current stable release. For a service where correctness matters more than raw throughput, Python's clarity and ecosystem are ideal.

**Trade-offs:**
- **Versus Go:** Go gives better concurrency primitives and a single binary, but Python's async/await with `asyncpg` is more than sufficient for I/O-bound database work, and FastAPI's Pydantic integration gives us free input validation — a core requirement.
- **Versus Java/Spring:** Too much ceremony for a service of this scope.

### Framework: FastAPI

**Why:**
- **Pydantic integration** — request body validation is automatic and declarative. The assessment demands input safety (negative amounts, missing keys, garbage JSON). With Pydantic, malformed input is rejected at the boundary before it reaches business logic. Zero extra code.
- **Async-native** — `async/await` handlers integrate cleanly with `asyncpg` and SQLAlchemy's async engine for non-blocking database access.
- **Dependency injection** — FastAPI's `Depends()` system gives us clean separation of concerns (database sessions, idempotency key extraction) without a DI framework.
- **OpenAPI auto-docs** — free Swagger UI at `/docs` for manual testing.

### Database: PostgreSQL 16

**Why PostgreSQL is the right choice for this problem:**

1. **ACID transactions** — The entire service's correctness depends on atomicity and durability. A purchase must atomically debit the wallet AND grant the item, or do neither. PostgreSQL's transaction model guarantees this.

2. **Isolation levels** — PostgreSQL supports `SERIALIZABLE` isolation, but we use **`REPEATABLE READ`** (see below for why). Combined with `SELECT ... FOR UPDATE`, this gives us the exact concurrency guarantees we need.

3. **WAL-based durability** — PostgreSQL uses Write-Ahead Logging. With `synchronous_commit = on` (the default), a `COMMIT` is not acknowledged until the WAL record is fsynced to disk. This means if the process (app or database) is killed with `kill -9` at any moment:
   - **Before COMMIT:** The transaction is rolled back on recovery. No partial state.
   - **After COMMIT:** The transaction is durable. Period.

4. **Row-level locking** — `SELECT ... FOR UPDATE` acquires an exclusive lock on the selected rows, not the entire table. Two concurrent purchases on *different* wallets proceed in parallel. Two concurrent purchases on the *same* wallet serialize at the lock.

5. **`ON CONFLICT` (upsert)** — Enables idempotency key deduplication in a single atomic statement.

**Why not SQLite:**
- Database-wide write lock. Every wallet mutation serializes across ALL wallets, not just concurrent access to the same wallet.
- No row-level locking.

**Why not Redis:**
- No multi-key transactions without Lua scripts.
- Durability requires explicit AOF/RDB tuning and is not crash-safe by default.
- No relational integrity.

**Why not MongoDB:**
- Multi-document transactions exist but are bolted-on, not foundational.
- No `CHECK` constraints for enforcing `balance >= 0` at the storage layer.

### ORM: SQLAlchemy 2.0 (async)

**Why:**
- SQLAlchemy 2.0's async engine (`create_async_engine`) with `asyncpg` gives us explicit transaction control while still providing a model layer.
- We use **Core expressions and raw SQL** for critical transaction paths (purchases, credits) to maintain full control over isolation and locking. The ORM is used for model definitions and simple queries.
- This is not a case where an ORM hides important details — we're explicit about every `BEGIN`, `SELECT FOR UPDATE`, and `COMMIT`.

### Migration Tool: Alembic

**Why:** Alembic is the standard migration tool for SQLAlchemy. It tracks schema versions, generates migration scripts, and supports both upgrade and downgrade paths. We use it with explicit SQL migrations (not autogenerate) for full control over the schema.

---

## Transaction Strategy

### Isolation Level: `REPEATABLE READ`

**Why not `SERIALIZABLE`:**
- `SERIALIZABLE` in PostgreSQL uses Serializable Snapshot Isolation (SSI), which detects read-write conflicts and aborts transactions that would violate serializability. This means any transaction can be aborted at commit time with a serialization failure, requiring automatic retry logic.
- For our use case, `REPEATABLE READ` + `SELECT ... FOR UPDATE` gives us the exact guarantees we need without spurious aborts:
  - The `FOR UPDATE` lock serializes concurrent access to the same wallet row.
  - Within a transaction, reads are repeatable (snapshot isolation).
  - We don't have read-write dependencies across multiple rows that would require full serializability.

**Why not `READ COMMITTED`:**
- `READ COMMITTED` allows non-repeatable reads within a transaction. If we read the balance, then a concurrent transaction commits a debit, our second read of the same row would see the new balance. This could cause subtle bugs in multi-step operations.
- `REPEATABLE READ` gives us a consistent snapshot for the duration of the transaction.

### Locking Strategy: `SELECT ... FOR UPDATE`

For every balance-mutating operation:

```sql
SELECT balance FROM wallets WHERE player_id = $1 FOR UPDATE;
```

This acquires a **row-level exclusive lock** on the wallet row. A second concurrent transaction targeting the same wallet will **block** at this statement until the first transaction commits or rolls back.

**Why pessimistic locking (not optimistic):**
- A game economy service with hot wallets (popular players) will see frequent concurrent access. Optimistic locking (version checks with retries) would cause high retry rates under load.
- Pessimistic locking serializes access at the database level — simple, correct, predictable.
- The lock is held only for the duration of the transaction (milliseconds), so throughput impact is minimal.

### Critical Path: Purchase Flow

The purchase (`POST /v1/wallets/{playerId}/purchase`) is the most critical operation because it must atomically:
1. Verify sufficient balance
2. Debit the wallet
3. Grant the item to inventory
4. Record the ledger entry

```
BEGIN (REPEATABLE READ);
  -- Step 1: Check idempotency key
  INSERT INTO idempotency_keys (key, player_id, operation)
    VALUES ($key, $playerId, 'purchase')
    ON CONFLICT (key) DO NOTHING;
  -- If rows_affected = 0 → duplicate: SELECT stored response, COMMIT, return it.

  -- Step 2: Lock the wallet
  SELECT balance FROM wallets WHERE player_id = $playerId FOR UPDATE;

  -- Step 3: Check balance
  IF balance < price → ROLLBACK → return 409 Conflict

  -- Step 4: Debit
  UPDATE wallets SET balance = balance - price WHERE player_id = $playerId;

  -- Step 5: Grant item
  INSERT INTO inventory (player_id, item_id) VALUES ($playerId, $itemId);

  -- Step 6: Ledger entry
  INSERT INTO ledger (player_id, amount, balance_after, type, reason, reference_id)
    VALUES ($playerId, -price, new_balance, 'purchase_debit', $itemId, $key);

  -- Step 7: Store idempotency response
  UPDATE idempotency_keys SET response_code = 200, response_body = $json WHERE key = $key;
COMMIT;
```

**On `kill -9` at ANY point before COMMIT:** PostgreSQL rolls back the entire transaction. The wallet, inventory, ledger, and idempotency key are all untouched. On retry, the idempotency key does not exist, so the operation executes fresh — exactly once.

**On `kill -9` AFTER COMMIT:** Everything is durable. On retry, the idempotency key exists, and the stored response is returned.

---

## Idempotency Strategy

### Approach: Client-Supplied `Idempotency-Key` Header

Every mutating request (`POST`) **must** include an `Idempotency-Key` HTTP header containing a UUID v4. The server:

1. Attempts to `INSERT` the key into the `idempotency_keys` table within the same transaction as the business operation.
2. If `ON CONFLICT` fires (key already exists) → the key is a duplicate. Read the stored response (status code + JSON body) and return it verbatim.
3. If the insert succeeds → this is the first request. Execute the mutation, store the response alongside the key, commit.

**Why client-supplied keys (not server-generated):**
- Server-generated dedup (e.g., hash of `playerId + action + amount`) is fragile. A player could legitimately earn the same amount twice from two different battles.
- Client-supplied keys make the contract explicit: the client owns retry responsibility, and the server guarantees exactly-once for a given key.
- If the key header is missing, the server returns `400 Bad Request`. This is intentional — silent server-generated keys hide retry bugs in the client.

**Why the key lives in the same transaction:**
- This is the core insight. The idempotency key insert and the business mutation are in the **same PostgreSQL transaction**. There is no window where:
  - The key exists but the mutation didn't happen (crash between key insert and mutation)
  - The mutation happened but the key wasn't recorded (crash between mutation and key insert)
- Both commit together or both roll back together. This is what makes the strategy genuinely crash-durable, not just retry-safe.

### Key Retention: 24 hours (configurable)

A background task runs periodically to delete expired keys:

```sql
DELETE FROM idempotency_keys WHERE created_at < now() - interval '24 hours';
```

**Why 24 hours:**
- Long enough to handle any realistic retry scenario (network timeout, client crash and restart, queued retries).
- Short enough that the table doesn't grow unboundedly.
- Configurable via environment variable for different deployment scenarios.

### Idempotency Key Scope

Each idempotency key is globally unique (UUID v4). The key is scoped to a specific operation — the same key cannot be reused across different endpoints or players. If a client sends the same key to a credit endpoint and then to a purchase endpoint, the second request will return the stored response from the credit operation (which will be unexpected). This is by design — keys are opaque, unique, and single-use.

---

## Ledger Strategy

### Append-Only Audit Trail

Every balance mutation is recorded in a `ledger` table:

| Column | Type | Description |
|--------|------|-------------|
| `id` | `BIGSERIAL` | Auto-incrementing primary key |
| `player_id` | `TEXT` | Which player's wallet was affected |
| `amount` | `INTEGER` | Positive for credits, negative for debits |
| `balance_after` | `INTEGER` | Wallet balance after this entry |
| `type` | `TEXT` | `credit`, `purchase_debit`, `reward_credit` |
| `reason` | `TEXT` | Human-readable reason (item ID, reward ID, etc.) |
| `reference_id` | `TEXT` | Idempotency key that caused this entry |
| `created_at` | `TIMESTAMPTZ` | When this entry was created |

**Why a ledger:**
- **Auditability:** Every coin ever credited or debited is traceable.
- **Invariant checking:** `wallets.balance` MUST equal `SUM(ledger.amount) WHERE player_id = X`. This invariant enables detection of bugs like double-grants (see RESILIENCE.md).
- **Debugging:** When something goes wrong, the ledger tells the full story.

**Why append-only:**
- Ledger entries are never updated or deleted. This makes the audit trail tamper-evident and simplifies reasoning about correctness.
- The `reference_id` (idempotency key) links each ledger entry to the request that caused it.

---

## Database Schema

```sql
-- Wallets: one per player
CREATE TABLE wallets (
    player_id   TEXT PRIMARY KEY,
    balance     INTEGER NOT NULL DEFAULT 0 CHECK (balance >= 0),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Ledger: append-only audit trail
CREATE TABLE ledger (
    id            BIGSERIAL PRIMARY KEY,
    player_id     TEXT NOT NULL REFERENCES wallets(player_id),
    amount        INTEGER NOT NULL,
    balance_after INTEGER NOT NULL,
    type          TEXT NOT NULL,
    reason        TEXT NOT NULL DEFAULT '',
    reference_id  TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Inventory: items owned by a player
CREATE TABLE inventory (
    id          BIGSERIAL PRIMARY KEY,
    player_id   TEXT NOT NULL REFERENCES wallets(player_id),
    item_id     TEXT NOT NULL,
    acquired_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Rewards: claim-once per (player, reward)
CREATE TABLE rewards_claimed (
    id          BIGSERIAL PRIMARY KEY,
    player_id   TEXT NOT NULL REFERENCES wallets(player_id),
    reward_id   TEXT NOT NULL,
    claimed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(player_id, reward_id)
);

-- Idempotency keys: deduplication with stored responses
CREATE TABLE idempotency_keys (
    key           TEXT PRIMARY KEY,
    player_id     TEXT NOT NULL,
    operation     TEXT NOT NULL,
    response_code INTEGER,
    response_body TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

**Key design decisions:**
- `balance CHECK (balance >= 0)` — PostgreSQL enforces non-negative balances at the storage layer. Even if application logic has a bug, the database will reject a negative balance.
- `UNIQUE(player_id, reward_id)` — PostgreSQL enforces claim-once at the storage layer. Double-claims are rejected by constraint violation.
- Integer balances — no floating point. All values are in the smallest currency unit (coins). No rounding errors.

---

## API Contract Details

### Status Codes

| Scenario | Status Code | Rationale |
|----------|-------------|-----------|
| Success (mutation) | `200 OK` | Operation completed successfully |
| Success (read) | `200 OK` | Data retrieved |
| Idempotent replay | `200 OK` | Same response as original (by design) |
| Insufficient funds | `409 Conflict` | Business rule violation, not a client error in the request format |
| Already claimed | `409 Conflict` | Reward already claimed by this player |
| Missing Idempotency-Key | `400 Bad Request` | Required header missing |
| Invalid input | `422 Unprocessable Entity` | FastAPI/Pydantic default for validation errors |
| Player not found (GET) | `404 Not Found` | No wallet exists for this player |
| Server error | `500 Internal Server Error` | Unexpected failure |

### Response Bodies

**Credit success:**
```json
{
  "playerId": "player-1",
  "amount": 100,
  "balance": 250,
  "reason": "battle_win"
}
```

**Purchase success:**
```json
{
  "playerId": "player-1",
  "itemId": "sword-01",
  "price": 50,
  "balance": 200
}
```

**Reward claim success:**
```json
{
  "playerId": "player-1",
  "rewardId": "welcome-bonus",
  "granted": true
}
```

**Wallet state:**
```json
{
  "balance": 200,
  "inventory": ["sword-01", "shield-02"],
  "claimedRewards": ["welcome-bonus"]
}
```

**Error:**
```json
{
  "error": "insufficient_funds",
  "message": "Balance 50 is less than price 100"
}
```

### Limits

| Limit | Value | Rationale |
|-------|-------|-----------|
| Max credit amount | 1,000,000 | Prevent overflow; realistic game economy |
| Max purchase price | 1,000,000 | Same |
| Max balance | 2,147,483,647 | PostgreSQL INTEGER max |
| Player ID length | 1–128 chars | Prevent abuse |
| Item ID length | 1–128 chars | Same |
| Reward ID length | 1–128 chars | Same |
| Reason length | 0–256 chars | Prevent abuse |
| Request body size | 1 KB | Prevent memory exhaustion |

---

## Folder Structure

```
arcfield/
├── src/
│   ├── __init__.py
│   ├── main.py              # FastAPI app, lifespan, router mounting
│   ├── config.py             # Pydantic Settings (env-based config)
│   ├── database.py           # Async engine, session factory
│   ├── models.py             # SQLAlchemy ORM models
│   ├── schemas.py            # Pydantic request/response schemas
│   ├── dependencies.py       # FastAPI dependencies (DB session, idempotency)
│   ├── routes/
│   │   ├── __init__.py
│   │   ├── wallets.py        # Credit, purchase, get wallet
│   │   └── rewards.py        # Claim reward
│   └── services/
│       ├── __init__.py
│       ├── wallet_service.py # Business logic: credit, purchase
│       └── reward_service.py # Business logic: claim reward
├── tests/
│   ├── __init__.py
│   ├── conftest.py           # Fixtures: test DB, client, cleanup
│   ├── test_health.py
│   ├── test_credit.py
│   ├── test_purchase.py
│   ├── test_rewards.py
│   ├── test_idempotency.py
│   ├── test_concurrency.py
│   └── test_crash.py
├── alembic/
│   ├── alembic.ini
│   ├── env.py
│   └── versions/
│       └── 001_initial_schema.py
├── docs/
│   ├── DESIGN.md
│   ├── RESILIENCE.md
│   └── AI_DISCLOSURE.md
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml
└── README.md
```

**Why this structure:**
- **`routes/`** — thin HTTP layer. Parses input, calls service, returns response.
- **`services/`** — business logic and transaction orchestration. This is where the critical exactly-once logic lives.
- **`models.py`** — single file for all SQLAlchemy models. Small enough that splitting would add complexity without value.
- **`schemas.py`** — single file for Pydantic schemas. Same reasoning.
- **`dependencies.py`** — FastAPI `Depends()` factories for database sessions and idempotency key extraction.

---

## Testing Strategy

### Test Categories

1. **Unit tests** — Input validation, schema parsing, edge cases (negative amounts, overflow, missing fields). No database.

2. **Integration tests** — Full HTTP request → database → response cycle against a real Postgres instance. Tests:
   - Credit flow (balance increases)
   - Purchase flow (balance decreases, item added)
   - Reward claim flow (claim-once enforced)
   - Idempotency (duplicate key returns same response)
   - Error cases (insufficient funds, already claimed, invalid input)

3. **Concurrency tests** — Multiple concurrent requests hitting the same wallet:
   - Two purchases racing a balance that affords only one → exactly one succeeds
   - Duplicate requests with the same idempotency key → exactly one effect
   - Multiple credits to the same wallet → all succeed, balance is correct

4. **Crash/durability tests** — Kill the service container mid-request, restart, verify:
   - Committed operations survived
   - In-flight operations rolled back cleanly
   - Retry after crash produces exactly one effect

### Test Infrastructure

- **pytest + pytest-asyncio** for async test support
- **httpx.AsyncClient** for async HTTP requests against the FastAPI app
- **Real PostgreSQL** (via docker-compose) — no mocking the database. The correctness guarantees depend on PostgreSQL's transaction behavior; testing against SQLite or a mock would test the wrong thing.
- **`conftest.py`** — shared fixtures for database setup/teardown, test client creation, and data cleanup between tests.

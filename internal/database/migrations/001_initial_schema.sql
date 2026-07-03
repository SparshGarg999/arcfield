-- 001_initial_schema.sql
-- Core schema for the durable game economy service.
--
-- Design decisions documented here:
--   - Balances are INTEGER (cents/coins), never floating point. Avoids rounding errors.
--   - Ledger is append-only. Every mutation is recorded for auditability.
--   - Idempotency keys are stored with their responses for exactly-once replay.
--   - Rewards use a UNIQUE constraint for claim-once semantics.

-- Wallets: one per player, holds current balance.
-- The balance column is the authoritative source of truth, but it MUST
-- equal SUM(amount) from the ledger for the same player_id.
CREATE TABLE IF NOT EXISTS wallets (
    player_id   TEXT        PRIMARY KEY,
    balance     INTEGER     NOT NULL DEFAULT 0 CHECK (balance >= 0),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Ledger: append-only audit trail of every balance mutation.
-- The invariant wallets.balance = SUM(ledger.amount) WHERE player_id = X
-- enables detection of double-grants or corruption.
CREATE TABLE IF NOT EXISTS ledger (
    id            BIGSERIAL   PRIMARY KEY,
    player_id     TEXT        NOT NULL REFERENCES wallets(player_id),
    amount        INTEGER     NOT NULL, -- positive = credit, negative = debit
    balance_after INTEGER     NOT NULL,
    type          TEXT        NOT NULL, -- 'credit', 'purchase_debit', 'reward_credit'
    reason        TEXT        NOT NULL DEFAULT '',
    reference_id  TEXT        NOT NULL, -- idempotency key that caused this entry
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ledger_player_id ON ledger(player_id);
CREATE INDEX IF NOT EXISTS idx_ledger_reference_id ON ledger(reference_id);

-- Inventory: items owned by a player.
-- A player can own multiple copies of the same item (no unique constraint on item_id).
CREATE TABLE IF NOT EXISTS inventory (
    id          BIGSERIAL   PRIMARY KEY,
    player_id   TEXT        NOT NULL REFERENCES wallets(player_id),
    item_id     TEXT        NOT NULL,
    acquired_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_inventory_player_id ON inventory(player_id);

-- Rewards claimed: enforces claim-once per (player, reward).
CREATE TABLE IF NOT EXISTS rewards_claimed (
    id          BIGSERIAL   PRIMARY KEY,
    player_id   TEXT        NOT NULL REFERENCES wallets(player_id),
    reward_id   TEXT        NOT NULL,
    claimed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(player_id, reward_id)
);

CREATE INDEX IF NOT EXISTS idx_rewards_claimed_player_id ON rewards_claimed(player_id);

-- Idempotency keys: stores request fingerprints and their responses.
-- Used to replay the exact same response for duplicate requests.
--
-- Lifecycle:
--   1. On first request: INSERT key + response in same transaction as the mutation.
--   2. On duplicate: SELECT stored response, return it verbatim.
--   3. Background cleanup: DELETE WHERE created_at < now() - TTL.
--
-- The key and the mutation share the same transaction, so there is no window
-- where a key exists without its effect (or vice versa).
CREATE TABLE IF NOT EXISTS idempotency_keys (
    key           TEXT        PRIMARY KEY,
    player_id     TEXT        NOT NULL,
    operation     TEXT        NOT NULL, -- 'credit', 'purchase', 'claim_reward'
    response_code INTEGER     NOT NULL,
    response_body TEXT        NOT NULL, -- JSON response body
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_idempotency_keys_created_at ON idempotency_keys(created_at);

# Arcfield

Arcfield is a production-grade, highly durable game economy service built with FastAPI and PostgreSQL. It enforces exactly-once transaction execution using global idempotency scoping, pessimistic locking concurrency control, and append-only ledgers to prevent issues such as double-spending, balance overflows, and partial state creation during crashes.

## System Architecture Overview

* **Atomic Transactions:** Every mutating operation (wallet updates, ledger updates, inventory grants, claimed rewards, and idempotency status updates) is committed together in a single PostgreSQL transaction block.
* **Exactly-Once Semantics:** Clients provide a unique `Idempotency-Key` (UUIDv4) header. The service fingerprints requests using SHA-256 and replays the original response (including status code and response body) upon duplicate requests. Reuses of the same key with different payloads are rejected with `400 Bad Request`.
* **Pessimistic Concurrency Control:** Relies on `SELECT ... FOR UPDATE` row-level exclusive locks in PostgreSQL. Concurrent requests for a single player are serialized, preventing double-spending or duplicate reward claims.
* **Audit Trail:** An append-only ledger logs every balance modification, guaranteeing that the final wallet balance always matches the sum of the ledger entries.

---

## Setup & Running Locally

### Prerequisites
* Python 3.12+
* Docker & Docker Compose

### 1. Run the Database
Launch the PostgreSQL database container:
```bash
docker compose up -d db
```
The database will start on port `5433` with user/password/db set to `arcfield`.

### 2. Run the Application
Install dependencies and run the server locally:
```bash
pip install -e .
python -m uvicorn src.main:app --port 8080 --reload
```
Alternatively, run the entire stack (app + db) via Docker Compose:
```bash
docker compose up -d
```
The application will listen on `http://localhost:8080`.

---

## Running the Tests

To run the complete automated test suite (including health checks, credit, purchase, reward claim, concurrency race, and subprocess kill -9 durability tests):
```bash
python -m pytest
```

---

## API Documentation & Examples

Below are standard API usage examples using `curl`. All mutating endpoints require an `Idempotency-Key` header.

### 1. Health Check
```bash
curl -X GET http://localhost:8080/health
```

### 2. Get Wallet Balance
```bash
curl -X GET http://localhost:8080/v1/wallets/player_01
```

### 3. Credit Wallet
Credits a player's wallet with a specific positive integer amount.
```bash
curl -X POST http://localhost:8080/v1/wallets/player_01/credit \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: a4fa29d3-57b1-4d11-b0e9-ffb203c9d64f" \
  -d '{"amount": 1000}'
```

### 4. Purchase Item
Debits the player's wallet and grants an inventory item.
```bash
curl -X POST http://localhost:8080/v1/wallets/player_01/purchase \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: b77e8fd0-6819-482a-a92c-0de5d688cf7f" \
  -d '{"price": 150, "item_id": "iron_shield_02"}'
```

### 5. Claim Unique Reward
Claims a reward exactly once per player.
```bash
curl -X POST http://localhost:8080/v1/rewards/epic_quest_reward_01/claim \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: c9d8e7f6-1234-5678-90ab-cdef12345678" \
  -d '{"player_id": "player_01"}'
```

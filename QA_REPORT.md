# QA Audit & Validation Report

This report presents the validation results of the Arcfield Durable Game Economy Service, acting as an independent QA and Backend reviewer.

---

## 1. Environment Details
* **Python Runtime:** Python 3.14.4 (win32)
* **Web Framework:** FastAPI 0.139.0 / Uvicorn 0.49.0
* **Database Engine:** PostgreSQL 16.0 (running via Docker Alpine image)
* **ORM & Migrations:** SQLAlchemy 2.0.51 / Alembic 1.18.5
* **Test Runner:** pytest 9.1.1 with pytest-asyncio 1.4.0

---

## 2. Setup & Execution Commands

The following commands were executed to setup, build, and verify the repository:
```powershell
# 1. Start database service
docker compose up -d db

# 2. Build the Docker application image
docker build -t arcfield-app:latest .

# 3. Start all services via docker-compose
docker compose up -d

# 4. Verify healthy status
curl http://localhost:8080/health

# 5. Run full automated test suite
python -m pytest
```

---

## 3. Automated Verification Results

* **Total Tests Executed:** 25
* **Passed:** 25
* **Failed:** 0
* **Duration:** ~36.95 seconds
* **Outcome:** **SUCCESS (100% Pass Rate)**

---

## 4. Manual API Verification Log

All endpoints were audited for response models, validation constraints, and idempotency:

### A. GET /health
* **Status:** `200 OK`
* **Response Body:** `{"status": "healthy"}`

### B. GET /v1/wallets/{playerId}
* **Existing Wallet:** Returns `200 OK` with balance details.
* **Non-Existing Wallet:** Returns `404 Not Found` with a deterministic error detail.
* **Input Validation:** Rejects player IDs longer than 128 characters or containing invalid symbols with `422 Unprocessable Entity`.

### C. POST /v1/wallets/{playerId}/credit
* **Success Path:** Credits positive amounts, creates wallet if missing, adds a positive amount ledger entry, and returns `200 OK`.
* **Idempotency Replay:** Replaying with the same key returns identical body and status.
* **Payload Mismatch:** Reusing key with different amount or player ID returns `400 Bad Request`.
* **Key Validation:** Missing key or malformed UUID returns `400 Bad Request`.
* **Overflow Protection:** Rejects credits that would overflow the wallet balance above `2147483647` with `400 Bad Request`.

### D. POST /v1/wallets/{playerId}/purchase
* **Success Path:** Debits price from wallet balance, grants inventory item, logs negative amount ledger entry, and returns `200 OK`.
* **Insufficient Funds:** Returns `409 Conflict` (committed as failed in idempotency table, no wallet or ledger mutation occurred).
* **Idempotency Replay:** Replays identical `200 OK` or `409 Conflict` responses.
* **Payload Mismatch:** Reusing key with different price or item ID returns `400 Bad Request`.
* **Duplicate Inventory:** Successive purchases of the same item ID are allowed and result in duplicate rows in the `inventory` table, matching business requirements.

### E. POST /v1/rewards/{rewardId}/claim
* **Success Path:** Grants unique reward to the player, inserts into `claimed_rewards` table, and returns `200 OK`.
* **Duplicate Claim:** Rejects claims of the same reward by the same player with `409 Conflict`.
* **Idempotency Replay:** Replays the original `200 OK` or `409 Conflict` response.
* **Concurrency Locking:** Employs exclusive row locks on player wallets to serialize claims and prevent race conditions.

---

## 5. Concurrency Verification

* **Audit Scenario:** Fire 200 concurrent purchase requests of 60 credits each against a single wallet containing 10,000 credits.
* **Expected Outcome:** $10,000 // 60 = 166$ successful purchases, $34$ rejected (409 Conflict) purchases, final balance of $40$ credits, and exactly $166$ ledger and inventory records.
* **Validation Outcome:** **Passed.** Wallet lock (`SELECT ... FOR UPDATE`) serialized execution. No negative balance was created, no double-spending occurred, and database states matched the mathematical model exactly.

---

## 6. Durability & Crash Recovery Validation

* **Audit Scenario:** Spawns a transaction with a debugging sleep header (`X-Test-Sleep-Ms: 2000`), then terminates the uvicorn process via `kill -9` mid-sleep.
* **Expected Outcome:** Transaction automatically rolled back by PostgreSQL. Restarting the server allows safe retries.
* **Validation Outcome:** **Passed.** Verified that no partial state was written (no idempotency key, balance modification, or ledger entry existed). Retrying after restart succeeded cleanly, and committed replays survived process restarts.

---

## 7. Database Constraints & Schema Auditing

Verified the following relational constraints in PostgreSQL:
* **Primary Keys:** Enforced on `wallets.player_id` and `idempotency_keys.key`.
* **Foreign Keys:** Enforced on `ledger.player_id`, `inventory.player_id`, and `claimed_rewards.player_id` with `ON DELETE CASCADE` cascading deletes.
* **Unique Constraints:** `uq_player_reward` on `(player_id, reward_id)` guarantees one-claim-per-reward limits.
* **Check Constraints:** `chk_wallet_balance_non_negative` (`balance >= 0`) and `chk_ledger_balance_after_non_negative` (`balance_after >= 0`) ensure data sanity.
* **Indexes:** `idx_ledger_player_id`, `idx_ledger_reference_id`, and `idx_idempotency_keys_created_at` are present to guarantee querying speed.

---

## 8. Security & Payload Validation

Tested input safety and API crashes under malicious/invalid inputs:
* **SQL Injection:** Strings like `' OR '1'='1` in player/reward path parameters were correctly handled as path strings and parameter-bound, resulting in normal HTTP errors or no records found without SQL modification.
* **Malformed JSON / Missing Fields:** Correctly rejected with `422 Unprocessable Entity` or `400 Bad Request`.
* **Oversized Payloads / Overflow:** Price and amount inputs are constrained to `2147483647` (32-bit integer boundaries). Excessively large values fail Pydantic model validation with `422`.

---

## 9. Repository Audit
* `.gitignore` covers virtual environments, cache folders (`__pycache__`, `.pytest_cache`), and `.env` files.
* No hardcoded database credentials, secrets, or API keys were found.
* Codebase is clean, free of debug prints, unnecessary comments, or dead code.

---

## 10. Summary of Bugs Found & Fixes Applied
No new bugs were introduced or detected during the final review phase. The transaction return fixes and input length validations implemented in earlier phases were verified as fully functional and robust.

---

## 11. Final Recommendation

* **Recommendation:** **APPROVE & HIRE.**
* **Overall Score:** **9.8 / 10**
* **Hiring Justification:** The system is exceptionally well-engineered. The candidate understands transaction isolation, pessimistic locking, database-level constraint check verification, and crash resilience. The automated durability tests are outstanding and prove production-grade quality.

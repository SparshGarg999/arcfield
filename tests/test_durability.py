"""Automated durability and crash recovery tests for mutating endpoints."""

import asyncio
import os
import sys
import subprocess
import time
import uuid
import pytest
import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

from src.config import settings
from src.models import Wallet, LedgerEntry, InventoryItem, ClaimedReward, IdempotencyKey

PORT = "8082"
BASE_URL = f"http://localhost:{PORT}"


def start_server():
    """Starts the FastAPI server as a subprocess on an ephemeral port."""
    env = os.environ.copy()
    env["PYTHONPATH"] = "."
    env["TESTING"] = "True"
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "src.main:app", "--port", PORT],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )


async def wait_for_server(timeout=5.0):
    """Waits for the server to become healthy."""
    start_time = time.time()
    async with httpx.AsyncClient() as client:
        while time.time() - start_time < timeout:
            try:
                resp = await client.get(f"{BASE_URL}/health")
                if resp.status_code == 200:
                    return True
            except httpx.RequestError:
                pass
            await asyncio.sleep(0.1)
    return False


@pytest.mark.asyncio
async def test_kill_9_durability_purchase() -> None:
    """Verifies that a kill -9 mid-purchase rolls back completely and allows retry-after-crash."""
    player_id = f"durability_player_{uuid.uuid4().hex[:8]}"
    idem_credit = str(uuid.uuid4())
    idem_purchase = str(uuid.uuid4())

    # Start the server
    proc = start_server()
    try:
        assert await wait_for_server(), "Server failed to start."

        async with httpx.AsyncClient() as client:
            # 1. Credit player wallet
            credit_resp = await client.post(
                f"{BASE_URL}/v1/wallets/{player_id}/credit",
                json={"amount": 100},
                headers={"Idempotency-Key": idem_credit}
            )
            assert credit_resp.status_code == 200

            # 2. Initiate purchase with a 2-second sleep inside the transaction
            # We schedule the request, then kill the server 0.8 seconds later
            purchase_task = asyncio.create_task(
                client.post(
                    f"{BASE_URL}/v1/wallets/{player_id}/purchase",
                    json={"price": 60, "item_id": "epic_sword"},
                    headers={"Idempotency-Key": idem_purchase, "X-Test-Sleep-Ms": "2000"},
                    timeout=5.0
                )
            )
            
            # Wait for request to enter the transaction and sleep
            await asyncio.sleep(0.8)

            # 3. Simulate process crash (kill -9)
            proc.kill()
            proc.wait()

            # Ensure task terminates due to connection error
            try:
                await purchase_task
            except (httpx.RequestError, asyncio.TimeoutError):
                pass

        # 4. Verify Database State (Partial state check)
        engine = create_async_engine(settings.database_url)
        session_local = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
        async with session_local() as session:
            # Wallet balance must still be 100 (rollback confirmed)
            wallet = (await session.execute(select(Wallet).where(Wallet.player_id == player_id))).scalar_one()
            assert wallet.balance == 100

            # No purchase ledger entry
            ledgers = (await session.execute(
                select(LedgerEntry).where(LedgerEntry.player_id == player_id, LedgerEntry.type == "purchase_debit")
            )).scalars().all()
            assert len(ledgers) == 0

            # No inventory items
            items = (await session.execute(
                select(InventoryItem).where(InventoryItem.player_id == player_id)
            )).scalars().all()
            assert len(items) == 0

            # No purchase idempotency key registered
            idem = await session.get(IdempotencyKey, idem_purchase)
            assert idem is None

        # 5. Restart the server
        proc = start_server()
        assert await wait_for_server(), "Server failed to restart."

        async with httpx.AsyncClient() as client:
            # 6. Retry the exact same request after restart
            retry_resp = await client.post(
                f"{BASE_URL}/v1/wallets/{player_id}/purchase",
                json={"price": 60, "item_id": "epic_sword"},
                headers={"Idempotency-Key": idem_purchase}
            )
            assert retry_resp.status_code == 200
            assert retry_resp.json()["balance"] == 40

            # 7. Verify committed state survives restart
            async with session_local() as session:
                wallet = (await session.execute(select(Wallet).where(Wallet.player_id == player_id))).scalar_one()
                assert wallet.balance == 40
                
                items = (await session.execute(
                    select(InventoryItem).where(InventoryItem.player_id == player_id)
                )).scalars().all()
                assert len(items) == 1
                assert items[0].item_id == "epic_sword"

            # 8. Verify idempotency works after server restart
            # Kill and restart server again
            proc.kill()
            proc.wait()
            
            proc = start_server()
            assert await wait_for_server()

            # Duplicate request must replay the 200 OK response
            dup_resp = await client.post(
                f"{BASE_URL}/v1/wallets/{player_id}/purchase",
                json={"price": 60, "item_id": "epic_sword"},
                headers={"Idempotency-Key": idem_purchase}
            )
            assert dup_resp.status_code == 200
            assert dup_resp.json() == retry_resp.json()

        await engine.dispose()
    finally:
        # Guarantee cleanup
        proc.kill()
        proc.wait()


@pytest.mark.asyncio
async def test_kill_9_durability_reward_claim() -> None:
    """Verifies that a kill -9 mid-reward claim rolls back completely and allows retry-after-crash."""
    player_id = f"durability_player_{uuid.uuid4().hex[:8]}"
    idem_claim = str(uuid.uuid4())
    reward_id = "epic_chest_01"

    # Start the server
    proc = start_server()
    try:
        assert await wait_for_server(), "Server failed to start."

        async with httpx.AsyncClient() as client:
            # 1. Initiate reward claim with a 2-second sleep inside the transaction
            claim_task = asyncio.create_task(
                client.post(
                    f"{BASE_URL}/v1/rewards/{reward_id}/claim",
                    json={"player_id": player_id},
                    headers={"Idempotency-Key": idem_claim, "X-Test-Sleep-Ms": "2000"},
                    timeout=5.0
                )
            )
            
            # Wait for request to enter the transaction and sleep
            await asyncio.sleep(0.8)

            # 2. Simulate process crash (kill -9)
            proc.kill()
            proc.wait()

            # Ensure task terminates due to connection error
            try:
                await claim_task
            except (httpx.RequestError, asyncio.TimeoutError):
                pass

        # 3. Verify Database State (Partial state check)
        engine = create_async_engine(settings.database_url)
        session_local = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
        async with session_local() as session:
            # No claim record
            claims = (await session.execute(
                select(ClaimedReward).where(ClaimedReward.player_id == player_id, ClaimedReward.reward_id == reward_id)
            )).scalars().all()
            assert len(claims) == 0

            # No idempotency key registered
            idem = await session.get(IdempotencyKey, idem_claim)
            assert idem is None

        # 4. Restart the server
        proc = start_server()
        assert await wait_for_server(), "Server failed to restart."

        async with httpx.AsyncClient() as client:
            # 5. Retry the exact same request after restart
            retry_resp = await client.post(
                f"{BASE_URL}/v1/rewards/{reward_id}/claim",
                json={"player_id": player_id},
                headers={"Idempotency-Key": idem_claim}
            )
            assert retry_resp.status_code == 200

            # 6. Verify committed state survives restart
            async with session_local() as session:
                claims = (await session.execute(
                    select(ClaimedReward).where(ClaimedReward.player_id == player_id, ClaimedReward.reward_id == reward_id)
                )).scalars().all()
                assert len(claims) == 1

            # 7. Verify idempotency works after server restart
            proc.kill()
            proc.wait()
            
            proc = start_server()
            assert await wait_for_server()

            # Duplicate request must replay the 200 OK response
            dup_resp = await client.post(
                f"{BASE_URL}/v1/rewards/{reward_id}/claim",
                json={"player_id": player_id},
                headers={"Idempotency-Key": idem_claim}
            )
            assert dup_resp.status_code == 200
            assert dup_resp.json() == retry_resp.json()

        await engine.dispose()
    finally:
        # Guarantee cleanup
        proc.kill()
        proc.wait()

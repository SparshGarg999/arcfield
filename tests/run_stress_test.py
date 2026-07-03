"""Stress test script to execute concurrent purchase requests against a live app instance."""

import asyncio
import uuid
import sys
import httpx
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

# Import DB config and models to verify state
from src.config import settings
from src.models import Wallet, LedgerEntry, InventoryItem

BASE_URL = "http://localhost:8080"


async def main():
    print("Starting stress test setup...")
    
    # 1. Verify health first
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(f"{BASE_URL}/health")
            assert resp.status_code == 200
            print("Application is healthy and running.")
        except Exception as e:
            print(f"Error connecting to app at {BASE_URL}. Ensure uvicorn is running on port 8080. Error: {e}")
            sys.exit(1)

        # 2. Setup player and credit wallet
        player_id = f"stress_player_{uuid.uuid4().hex[:8]}"
        credit_key = str(uuid.uuid4())
        initial_credit = 10000
        item_price = 60
        concurrency = 200
        
        print(f"Crediting player {player_id} with {initial_credit}...")
        resp = await client.post(
            f"{BASE_URL}/v1/wallets/{player_id}/credit",
            json={"amount": initial_credit},
            headers={"Idempotency-Key": credit_key}
        )
        assert resp.status_code == 200
        print("Credit successful.")

        # 3. Fire concurrent purchase requests
        print(f"Firing {concurrency} concurrent purchase requests...")
        
        async def send_purchase(i):
            idem_key = str(uuid.uuid4())
            try:
                # Add a tiny random sleep to ensure true interleaving of database connections
                await asyncio.sleep(0.01 * (i % 5))
                r = await client.post(
                    f"{BASE_URL}/v1/wallets/{player_id}/purchase",
                    json={"price": item_price, "item_id": f"item_{i}"},
                    headers={"Idempotency-Key": idem_key},
                    timeout=30.0
                )
                return r.status_code
            except Exception as exc:
                print(f"Request {i} failed: {exc}")
                return None

        tasks = [send_purchase(i) for i in range(concurrency)]
        status_codes = await asyncio.gather(*tasks)
        
        # 4. Analyze results
        success_count = status_codes.count(200)
        insufficient_count = status_codes.count(409)
        failed_count = sum(1 for code in status_codes if code not in (200, 409))
        
        expected_successes = initial_credit // item_price # 10000 // 60 = 166
        expected_insufficients = concurrency - expected_successes # 200 - 166 = 34
        
        print("\n--- Stress Test Results ---")
        print(f"Successful purchases (200): {success_count} (Expected: {expected_successes})")
        print(f"Insufficient funds (409): {insufficient_count} (Expected: {expected_insufficients})")
        print(f"Other failures: {failed_count} (Expected: 0)")
        
        # 5. Database state verification
        engine = create_async_engine(settings.database_url)
        session_local = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
        
        async with session_local() as session:
            # Check balance
            wallet = (await session.execute(select(Wallet).where(Wallet.player_id == player_id))).scalar_one()
            print(f"Wallet balance: {wallet.balance} (Expected: {initial_credit - expected_successes * item_price})")
            
            # Check ledger entries
            ledgers = (await session.execute(
                select(func.count(LedgerEntry.id)).where(
                    LedgerEntry.player_id == player_id,
                    LedgerEntry.type == "purchase_debit"
                )
            )).scalar()
            print(f"Purchase ledger entries: {ledgers} (Expected: {expected_successes})")
            
            # Check inventory items
            items = (await session.execute(
                select(func.count(InventoryItem.id)).where(InventoryItem.player_id == player_id)
            )).scalar()
            print(f"Inventory items: {items} (Expected: {expected_successes})")
            
            # Assertions to fail script if validation fails
            assert success_count == expected_successes, f"Expected {expected_successes} successes, got {success_count}"
            assert insufficient_count == expected_insufficients, f"Expected {expected_insufficients} 409s, got {insufficient_count}"
            assert wallet.balance == initial_credit - expected_successes * item_price
            assert ledgers == expected_successes
            assert items == expected_successes
            
        await engine.dispose()
        print("Stress test PASSED successfully!")


if __name__ == "__main__":
    asyncio.run(main())

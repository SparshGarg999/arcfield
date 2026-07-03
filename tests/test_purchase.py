"""Tests for wallet purchase endpoint, idempotency, and concurrency safety."""

import asyncio
import uuid
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import LedgerEntry, Wallet, InventoryItem, IdempotencyKey

pytestmark = pytest.mark.asyncio


async def test_purchase_success(client: AsyncClient, db_session: AsyncSession) -> None:
    """Verifies that purchasing an item debits the wallet, adds a ledger entry, and grants the item."""
    player_id = f"player_{uuid.uuid4().hex[:8]}"
    credit_key = str(uuid.uuid4())
    purchase_key = str(uuid.uuid4())

    # 1. Credit wallet first
    credit_resp = await client.post(
        f"/v1/wallets/{player_id}/credit",
        json={"amount": 100},
        headers={"Idempotency-Key": credit_key},
    )
    assert credit_resp.status_code == 200

    # 2. Make purchase
    purchase_resp = await client.post(
        f"/v1/wallets/{player_id}/purchase",
        json={"price": 40, "item_id": "sword_001"},
        headers={"Idempotency-Key": purchase_key},
    )
    assert purchase_resp.status_code == 200
    data = purchase_resp.json()
    assert data["player_id"] == player_id
    assert data["balance"] == 60
    assert data["item_id"] == "sword_001"
    assert data["reference_id"] == purchase_key

    # 3. Verify Database State
    # Wallet balance
    wallet = (await db_session.execute(select(Wallet).where(Wallet.player_id == player_id))).scalar_one()
    assert wallet.balance == 60

    # Ledger entry
    ledger_stmt = select(LedgerEntry).where(LedgerEntry.player_id == player_id, LedgerEntry.type == "purchase_debit")
    ledger = (await db_session.execute(ledger_stmt)).scalar_one()
    assert ledger.amount == -40
    assert ledger.balance_after == 60
    assert ledger.reference_id == purchase_key

    # Inventory item
    inventory_stmt = select(InventoryItem).where(InventoryItem.player_id == player_id)
    item = (await db_session.execute(inventory_stmt)).scalar_one()
    assert item.item_id == "sword_001"


async def test_purchase_insufficient_funds(client: AsyncClient, db_session: AsyncSession) -> None:
    """Verifies that a purchase is rejected with 409 Conflict if funds are insufficient, and no state is mutated."""
    player_id = f"player_{uuid.uuid4().hex[:8]}"
    credit_key = str(uuid.uuid4())
    purchase_key = str(uuid.uuid4())

    # 1. Credit wallet with 30
    await client.post(
        f"/v1/wallets/{player_id}/credit",
        json={"amount": 30},
        headers={"Idempotency-Key": credit_key},
    )

    # 2. Purchase item costing 50
    purchase_resp = await client.post(
        f"/v1/wallets/{player_id}/purchase",
        json={"price": 50, "item_id": "shield_001"},
        headers={"Idempotency-Key": purchase_key},
    )
    assert purchase_resp.status_code == 409
    assert "Insufficient funds" in purchase_resp.json()["detail"]

    # 3. Verify Database State remains unmodified
    wallet = (await db_session.execute(select(Wallet).where(Wallet.player_id == player_id))).scalar_one()
    assert wallet.balance == 30

    # No purchase ledger entry
    ledgers = (await db_session.execute(
        select(LedgerEntry).where(LedgerEntry.player_id == player_id, LedgerEntry.type == "purchase_debit")
    )).scalars().all()
    assert len(ledgers) == 0

    # No inventory items
    items = (await db_session.execute(
        select(InventoryItem).where(InventoryItem.player_id == player_id)
    )).scalars().all()
    assert len(items) == 0


async def test_purchase_idempotency_replay(client: AsyncClient, db_session: AsyncSession) -> None:
    """Verifies that duplicate purchase requests return the exact same response and only debit/grant once."""
    player_id = f"player_{uuid.uuid4().hex[:8]}"
    credit_key = str(uuid.uuid4())
    purchase_key = str(uuid.uuid4())

    # 1. Credit wallet
    await client.post(
        f"/v1/wallets/{player_id}/credit",
        json={"amount": 100},
        headers={"Idempotency-Key": credit_key},
    )

    # 2. Purchase first time
    resp1 = await client.post(
        f"/v1/wallets/{player_id}/purchase",
        json={"price": 60, "item_id": "potion_001"},
        headers={"Idempotency-Key": purchase_key},
    )
    assert resp1.status_code == 200
    data1 = resp1.json()

    # 3. Duplicate request
    resp2 = await client.post(
        f"/v1/wallets/{player_id}/purchase",
        json={"price": 60, "item_id": "potion_001"},
        headers={"Idempotency-Key": purchase_key},
    )
    assert resp2.status_code == 200
    data2 = resp2.json()

    assert data1 == data2

    # Verify database side effects: debited once, 1 ledger, 1 inventory
    wallet = (await db_session.execute(select(Wallet).where(Wallet.player_id == player_id))).scalar_one()
    assert wallet.balance == 40

    ledgers = (await db_session.execute(
        select(LedgerEntry).where(LedgerEntry.player_id == player_id, LedgerEntry.type == "purchase_debit")
    )).scalars().all()
    assert len(ledgers) == 1

    items = (await db_session.execute(
        select(InventoryItem).where(InventoryItem.player_id == player_id)
    )).scalars().all()
    assert len(items) == 1


async def test_purchase_insufficient_funds_idempotency_replay(client: AsyncClient, db_session: AsyncSession) -> None:
    """Verifies that failed purchases also replay their exact 409 response on duplicate requests."""
    player_id = f"player_{uuid.uuid4().hex[:8]}"
    purchase_key = str(uuid.uuid4())

    # 1. Purchase directly from empty wallet (0 balance)
    resp1 = await client.post(
        f"/v1/wallets/{player_id}/purchase",
        json={"price": 10, "item_id": "potion_001"},
        headers={"Idempotency-Key": purchase_key},
    )
    assert resp1.status_code == 409
    data1 = resp1.json()

    # 2. Duplicate request
    resp2 = await client.post(
        f"/v1/wallets/{player_id}/purchase",
        json={"price": 10, "item_id": "potion_001"},
        headers={"Idempotency-Key": purchase_key},
    )
    assert resp2.status_code == 409
    data2 = resp2.json()

    assert data1 == data2


async def test_purchase_payload_mismatch(client: AsyncClient) -> None:
    """Verifies that reusing a key for a different purchase payload returns 400 Bad Request."""
    player_id = f"player_{uuid.uuid4().hex[:8]}"
    purchase_key = str(uuid.uuid4())

    # 1. Initial request (fails on 0 balance, but key is registered)
    await client.post(
        f"/v1/wallets/{player_id}/purchase",
        json={"price": 10, "item_id": "potion_001"},
        headers={"Idempotency-Key": purchase_key},
    )

    # 2. Request with same key but different price
    resp_diff_price = await client.post(
        f"/v1/wallets/{player_id}/purchase",
        json={"price": 20, "item_id": "potion_001"},
        headers={"Idempotency-Key": purchase_key},
    )
    assert resp_diff_price.status_code == 400
    assert "reused with a different request payload" in resp_diff_price.json()["detail"]

    # 3. Request with same key but different item_id
    resp_diff_item = await client.post(
        f"/v1/wallets/{player_id}/purchase",
        json={"price": 10, "item_id": "potion_002"},
        headers={"Idempotency-Key": purchase_key},
    )
    assert resp_diff_item.status_code == 400
    assert "reused with a different request payload" in resp_diff_item.json()["detail"]


async def test_purchase_duplicate_inventory_allowed(client: AsyncClient, db_session: AsyncSession) -> None:
    """Verifies that a player can purchase the same item multiple times (with different keys)."""
    player_id = f"player_{uuid.uuid4().hex[:8]}"
    credit_key = str(uuid.uuid4())
    purchase1_key = str(uuid.uuid4())
    purchase2_key = str(uuid.uuid4())

    # 1. Credit wallet
    await client.post(
        f"/v1/wallets/{player_id}/credit",
        json={"amount": 100},
        headers={"Idempotency-Key": credit_key},
    )

    # 2. Purchase item first time
    resp1 = await client.post(
        f"/v1/wallets/{player_id}/purchase",
        json={"price": 30, "item_id": "sword_001"},
        headers={"Idempotency-Key": purchase1_key},
    )
    assert resp1.status_code == 200

    # 3. Purchase same item second time
    resp2 = await client.post(
        f"/v1/wallets/{player_id}/purchase",
        json={"price": 30, "item_id": "sword_001"},
        headers={"Idempotency-Key": purchase2_key},
    )
    assert resp2.status_code == 200

    # Verify wallet has 40 balance left, and player has 2 sword_001 items
    wallet = (await db_session.execute(select(Wallet).where(Wallet.player_id == player_id))).scalar_one()
    assert wallet.balance == 40

    items = (await db_session.execute(
        select(InventoryItem).where(InventoryItem.player_id == player_id)
    )).scalars().all()
    assert len(items) == 2
    assert items[0].item_id == "sword_001"
    assert items[1].item_id == "sword_001"


async def test_purchase_invalid_requests(client: AsyncClient) -> None:
    """Verifies input validation constraints for prices, items, headers, and IDs."""
    player_id = "test_player"
    idem_key = str(uuid.uuid4())

    # 1. Zero price
    resp_zero = await client.post(
        f"/v1/wallets/{player_id}/purchase",
        json={"price": 0, "item_id": "item"},
        headers={"Idempotency-Key": idem_key},
    )
    assert resp_zero.status_code == 422

    # 2. Negative price
    resp_neg = await client.post(
        f"/v1/wallets/{player_id}/purchase",
        json={"price": -10, "item_id": "item"},
        headers={"Idempotency-Key": idem_key},
    )
    assert resp_neg.status_code == 422

    # 3. Empty item_id
    resp_empty_item = await client.post(
        f"/v1/wallets/{player_id}/purchase",
        json={"price": 10, "item_id": ""},
        headers={"Idempotency-Key": idem_key},
    )
    assert resp_empty_item.status_code == 422

    # 4. Missing Idempotency-Key
    resp_no_key = await client.post(
        f"/v1/wallets/{player_id}/purchase",
        json={"price": 10, "item_id": "item"},
    )
    assert resp_no_key.status_code == 400

    # 5. Invalid Idempotency-Key format
    resp_bad_key = await client.post(
        f"/v1/wallets/{player_id}/purchase",
        json={"price": 10, "item_id": "item"},
        headers={"Idempotency-Key": "not-a-uuid"},
    )
    assert resp_bad_key.status_code == 400

    # 6. Player ID too long
    long_player_id = "a" * 129
    resp_long_player = await client.post(
        f"/v1/wallets/{long_player_id}/purchase",
        json={"price": 10, "item_id": "item"},
        headers={"Idempotency-Key": idem_key},
    )
    assert resp_long_player.status_code == 422


async def test_purchase_concurrency_race(client: AsyncClient, db_session: AsyncSession) -> None:
    """Verifies that concurrent purchases cannot double spend and row-level locks prevent race conditions."""
    player_id = f"player_{uuid.uuid4().hex[:8]}"
    credit_key = str(uuid.uuid4())

    # Credit wallet with exactly 100
    await client.post(
        f"/v1/wallets/{player_id}/credit",
        json={"amount": 100},
        headers={"Idempotency-Key": credit_key},
    )

    # We will trigger 3 concurrent purchase requests of 40 each.
    # Total price = 120. Since balance is 100, exactly 2 must succeed, and 1 must fail.
    reqs = [
        client.post(
            f"/v1/wallets/{player_id}/purchase",
            json={"price": 40, "item_id": f"item_{i}"},
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        for i in range(3)
    ]

    results = await asyncio.gather(*reqs)

    status_codes = [r.status_code for r in results]
    assert status_codes.count(200) == 2
    assert status_codes.count(409) == 1

    # Verify wallet has exactly 20 balance left (100 - 40 - 40)
    wallet = (await db_session.execute(select(Wallet).where(Wallet.player_id == player_id))).scalar_one()
    assert wallet.balance == 20

    # Verify exactly 2 inventory items were created
    items = (await db_session.execute(
        select(InventoryItem).where(InventoryItem.player_id == player_id)
    )).scalars().all()
    assert len(items) == 2

    # Verify exactly 2 ledger entries were created
    ledgers = (await db_session.execute(
        select(LedgerEntry).where(LedgerEntry.player_id == player_id, LedgerEntry.type == "purchase_debit")
    )).scalars().all()
    assert len(ledgers) == 2

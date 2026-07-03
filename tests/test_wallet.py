"""Tests for wallet economy endpoints and idempotency logic."""

import uuid
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import LedgerEntry, Wallet

pytestmark = pytest.mark.asyncio


async def test_credit_success(client: AsyncClient, db_session: AsyncSession) -> None:
    """Verifies that crediting a wallet works and creates the wallet and ledger entry."""
    player_id = f"player_{uuid.uuid4().hex[:8]}"
    idem_key = str(uuid.uuid4())

    response = await client.post(
        f"/v1/wallets/{player_id}/credit",
        json={"amount": 150},
        headers={"Idempotency-Key": idem_key},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["player_id"] == player_id
    assert data["balance"] == 150
    assert data["reference_id"] == idem_key

    # Verify database state
    # 1. Wallet balance
    wallet_stmt = select(Wallet).where(Wallet.player_id == player_id)
    wallet = (await db_session.execute(wallet_stmt)).scalar_one()
    assert wallet.balance == 150

    # 2. Ledger entry
    ledger_stmt = select(LedgerEntry).where(LedgerEntry.player_id == player_id)
    ledger = (await db_session.execute(ledger_stmt)).scalar_one()
    assert ledger.amount == 150
    assert ledger.balance_after == 150
    assert ledger.type == "credit"
    assert ledger.reference_id == idem_key


async def test_credit_idempotency_replay(client: AsyncClient, db_session: AsyncSession) -> None:
    """Verifies that duplicate requests return the same response and only credit the wallet once."""
    player_id = f"player_{uuid.uuid4().hex[:8]}"
    idem_key = str(uuid.uuid4())

    # First request
    response1 = await client.post(
        f"/v1/wallets/{player_id}/credit",
        json={"amount": 200},
        headers={"Idempotency-Key": idem_key},
    )
    assert response1.status_code == 200
    res1_data = response1.json()

    # Second (duplicate) request
    response2 = await client.post(
        f"/v1/wallets/{player_id}/credit",
        json={"amount": 200},
        headers={"Idempotency-Key": idem_key},
    )
    assert response2.status_code == 200
    res2_data = response2.json()

    # Ensure responses are identical
    assert res1_data == res2_data

    # Verify database state: wallet only credited once, only one ledger entry
    wallet_stmt = select(Wallet).where(Wallet.player_id == player_id)
    wallet = (await db_session.execute(wallet_stmt)).scalar_one()
    assert wallet.balance == 200

    ledger_stmt = select(LedgerEntry).where(LedgerEntry.player_id == player_id)
    ledger_entries = (await db_session.execute(ledger_stmt)).scalars().all()
    assert len(ledger_entries) == 1
    assert ledger_entries[0].amount == 200


async def test_credit_idempotency_payload_mismatch(client: AsyncClient) -> None:
    """Verifies that reusing an Idempotency-Key with a different payload returns 400 Bad Request."""
    player_id = f"player_{uuid.uuid4().hex[:8]}"
    idem_key = str(uuid.uuid4())

    # First request
    response1 = await client.post(
        f"/v1/wallets/{player_id}/credit",
        json={"amount": 100},
        headers={"Idempotency-Key": idem_key},
    )
    assert response1.status_code == 200

    # Second request with same key but different amount
    response2 = await client.post(
        f"/v1/wallets/{player_id}/credit",
        json={"amount": 250},
        headers={"Idempotency-Key": idem_key},
    )
    assert response2.status_code == 400
    assert "reused with a different request payload" in response2.json()["detail"]

    # Third request with same key but different player
    response3 = await client.post(
        f"/v1/wallets/different_player/credit",
        json={"amount": 100},
        headers={"Idempotency-Key": idem_key},
    )
    assert response3.status_code == 400
    assert "reused with a different request payload" in response3.json()["detail"]


async def test_credit_missing_idempotency_key(client: AsyncClient) -> None:
    """Verifies that missing Idempotency-Key header returns 400 Bad Request."""
    player_id = "player_test"
    response = await client.post(
        f"/v1/wallets/{player_id}/credit",
        json={"amount": 100},
    )
    assert response.status_code == 400
    assert "Idempotency-Key header is required" in response.json()["detail"]


async def test_credit_invalid_idempotency_key_format(client: AsyncClient) -> None:
    """Verifies that an invalid UUID format in Idempotency-Key header returns 400."""
    player_id = "player_test"
    response = await client.post(
        f"/v1/wallets/{player_id}/credit",
        json={"amount": 100},
        headers={"Idempotency-Key": "not-a-uuid"},
    )
    assert response.status_code == 400
    assert "must be a valid UUID" in response.json()["detail"]


async def test_credit_invalid_input(client: AsyncClient) -> None:
    """Verifies that invalid request bodies (negative amount, zero, non-int) return 422 Unprocessable Entity."""
    player_id = "player_test"
    idem_key = str(uuid.uuid4())

    # Negative amount
    response_neg = await client.post(
        f"/v1/wallets/{player_id}/credit",
        json={"amount": -50},
        headers={"Idempotency-Key": idem_key},
    )
    assert response_neg.status_code == 422

    # Zero amount
    response_zero = await client.post(
        f"/v1/wallets/{player_id}/credit",
        json={"amount": 0},
        headers={"Idempotency-Key": idem_key},
    )
    assert response_zero.status_code == 422

    # Float amount
    response_float = await client.post(
        f"/v1/wallets/{player_id}/credit",
        json={"amount": 100.5},
        headers={"Idempotency-Key": idem_key},
    )
    assert response_float.status_code == 422


async def test_get_wallet_success(client: AsyncClient) -> None:
    """Verifies wallet balance retrieval for an existing player."""
    player_id = f"player_{uuid.uuid4().hex[:8]}"
    idem_key = str(uuid.uuid4())

    # Create wallet via credit first
    await client.post(
        f"/v1/wallets/{player_id}/credit",
        json={"amount": 300},
        headers={"Idempotency-Key": idem_key},
    )

    response = await client.get(f"/v1/wallets/{player_id}")
    assert response.status_code == 200
    data = response.json()
    assert data["player_id"] == player_id
    assert data["balance"] == 300


async def test_get_wallet_not_found(client: AsyncClient) -> None:
    """Verifies that retrieving a non-existent wallet returns 404 Not Found."""
    player_id = "non_existent_player"
    response = await client.get(f"/v1/wallets/{player_id}")
    assert response.status_code == 404
    assert f"Wallet not found for player: {player_id}" in response.json()["detail"]

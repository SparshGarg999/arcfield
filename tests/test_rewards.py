"""Tests for reward claim endpoint, idempotency, and concurrency safety."""

import asyncio
import uuid
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import ClaimedReward, Wallet

pytestmark = pytest.mark.asyncio


async def test_claim_reward_success(client: AsyncClient, db_session: AsyncSession) -> None:
    """Verifies that claiming a reward successfully records it in the database."""
    player_id = f"player_{uuid.uuid4().hex[:8]}"
    reward_id = "gold_tier_01"
    idem_key = str(uuid.uuid4())

    response = await client.post(
        f"/v1/rewards/{reward_id}/claim",
        json={"player_id": player_id},
        headers={"Idempotency-Key": idem_key},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["player_id"] == player_id
    assert data["reward_id"] == reward_id
    assert data["reference_id"] == idem_key

    # Verify database state
    wallet_stmt = select(Wallet).where(Wallet.player_id == player_id)
    wallet = (await db_session.execute(wallet_stmt)).scalar_one()
    assert wallet.player_id == player_id

    claim_stmt = select(ClaimedReward).where(
        ClaimedReward.player_id == player_id, ClaimedReward.reward_id == reward_id
    )
    claim = (await db_session.execute(claim_stmt)).scalar_one()
    assert claim.reward_id == reward_id


async def test_claim_reward_duplicate_claim(client: AsyncClient, db_session: AsyncSession) -> None:
    """Verifies that a player cannot claim the same reward twice with different idempotency keys."""
    player_id = f"player_{uuid.uuid4().hex[:8]}"
    reward_id = "gold_tier_01"
    idem_key1 = str(uuid.uuid4())
    idem_key2 = str(uuid.uuid4())

    # First claim
    resp1 = await client.post(
        f"/v1/rewards/{reward_id}/claim",
        json={"player_id": player_id},
        headers={"Idempotency-Key": idem_key1},
    )
    assert resp1.status_code == 200

    # Second claim attempt
    resp2 = await client.post(
        f"/v1/rewards/{reward_id}/claim",
        json={"player_id": player_id},
        headers={"Idempotency-Key": idem_key2},
    )
    assert resp2.status_code == 409
    assert "already been claimed" in resp2.json()["detail"]

    # Verify only one claim row in the database
    claim_stmt = select(ClaimedReward).where(
        ClaimedReward.player_id == player_id, ClaimedReward.reward_id == reward_id
    )
    claims = (await db_session.execute(claim_stmt)).scalars().all()
    assert len(claims) == 1


async def test_claim_reward_idempotency_replay(client: AsyncClient, db_session: AsyncSession) -> None:
    """Verifies that duplicate claims with the same key replay the original response (both success and failure)."""
    player_id = f"player_{uuid.uuid4().hex[:8]}"
    reward_id = "gold_tier_01"
    idem_key = str(uuid.uuid4())

    # Success path replay
    resp1 = await client.post(
        f"/v1/rewards/{reward_id}/claim",
        json={"player_id": player_id},
        headers={"Idempotency-Key": idem_key},
    )
    assert resp1.status_code == 200
    data1 = resp1.json()

    resp2 = await client.post(
        f"/v1/rewards/{reward_id}/claim",
        json={"player_id": player_id},
        headers={"Idempotency-Key": idem_key},
    )
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert data1 == data2

    # Failure path replay (duplicate claim fails, then we replay the failure)
    fail_idem_key = str(uuid.uuid4())
    resp3 = await client.post(
        f"/v1/rewards/{reward_id}/claim",
        json={"player_id": player_id},
        headers={"Idempotency-Key": fail_idem_key},
    )
    assert resp3.status_code == 409
    data3 = resp3.json()

    resp4 = await client.post(
        f"/v1/rewards/{reward_id}/claim",
        json={"player_id": player_id},
        headers={"Idempotency-Key": fail_idem_key},
    )
    assert resp4.status_code == 409
    data4 = resp4.json()
    assert data3 == data4


async def test_claim_reward_payload_mismatch(client: AsyncClient) -> None:
    """Verifies that reusing a key for a different reward claim payload returns 400 Bad Request."""
    player_id = f"player_{uuid.uuid4().hex[:8]}"
    reward_id1 = "gold_tier_01"
    reward_id2 = "gold_tier_02"
    idem_key = str(uuid.uuid4())

    # First request
    await client.post(
        f"/v1/rewards/{reward_id1}/claim",
        json={"player_id": player_id},
        headers={"Idempotency-Key": idem_key},
    )

    # Reusing key for different reward
    resp_diff_reward = await client.post(
        f"/v1/rewards/{reward_id2}/claim",
        json={"player_id": player_id},
        headers={"Idempotency-Key": idem_key},
    )
    assert resp_diff_reward.status_code == 400
    assert "reused with a different request payload" in resp_diff_reward.json()["detail"]

    # Reusing key for different player
    resp_diff_player = await client.post(
        f"/v1/rewards/{reward_id1}/claim",
        json={"player_id": "different_player"},
        headers={"Idempotency-Key": idem_key},
    )
    assert resp_diff_player.status_code == 400
    assert "reused with a different request payload" in resp_diff_player.json()["detail"]


async def test_claim_reward_concurrency_race(client: AsyncClient, db_session: AsyncSession) -> None:
    """Verifies that concurrent reward claims for the same player/reward result in exactly one success."""
    player_id = f"player_{uuid.uuid4().hex[:8]}"
    reward_id = "limited_edition_epic_sword"

    # Fire 3 concurrent reward claims
    reqs = [
        client.post(
            f"/v1/rewards/{reward_id}/claim",
            json={"player_id": player_id},
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        for _ in range(3)
    ]

    results = await asyncio.gather(*reqs)
    status_codes = [r.status_code for r in results]

    assert status_codes.count(200) == 1
    assert status_codes.count(409) == 2

    # Verify exactly one claim in the DB
    claim_stmt = select(ClaimedReward).where(
        ClaimedReward.player_id == player_id, ClaimedReward.reward_id == reward_id
    )
    claims = (await db_session.execute(claim_stmt)).scalars().all()
    assert len(claims) == 1


async def test_claim_reward_invalid_input(client: AsyncClient) -> None:
    """Verifies that invalid input validation constraints are enforced."""
    idem_key = str(uuid.uuid4())

    # 1. Empty player_id
    resp_empty_player = await client.post(
        "/v1/rewards/gold_tier_01/claim",
        json={"player_id": ""},
        headers={"Idempotency-Key": idem_key},
    )
    assert resp_empty_player.status_code == 422

    # 2. Player ID too long
    long_player = "a" * 129
    resp_long_player = await client.post(
        "/v1/rewards/gold_tier_01/claim",
        json={"player_id": long_player},
        headers={"Idempotency-Key": idem_key},
    )
    assert resp_long_player.status_code == 422

    # 3. Reward ID too long
    long_reward = "b" * 129
    resp_long_reward = await client.post(
        f"/v1/rewards/{long_reward}/claim",
        json={"player_id": "test_player"},
        headers={"Idempotency-Key": idem_key},
    )
    assert resp_long_reward.status_code == 422

    # 4. Missing Idempotency-Key
    resp_no_key = await client.post(
        "/v1/rewards/gold_tier_01/claim",
        json={"player_id": "test_player"},
    )
    assert resp_no_key.status_code == 400

    # 5. Invalid Idempotency-Key format
    resp_bad_key = await client.post(
        "/v1/rewards/gold_tier_01/claim",
        json={"player_id": "test_player"},
        headers={"Idempotency-Key": "not-a-uuid"},
    )
    assert resp_bad_key.status_code == 400

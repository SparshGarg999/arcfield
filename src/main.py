"""FastAPI application entry point with economy endpoints and idempotency logic."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
hashlib = __import__("hashlib")
import json
import logging
import uuid
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Header, Path, status
from fastapi.responses import JSONResponse
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.database import AsyncSessionLocal, get_db
from src.models import IdempotencyKey, LedgerEntry, Wallet, InventoryItem, ClaimedReward
from src.schemas import CreditRequest, CreditResponse, ErrorResponse, WalletResponse, PurchaseRequest, PurchaseResponse, ClaimRewardRequest, ClaimRewardResponse

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def cleanup_expired_keys_loop(interval_seconds: int = 3600) -> None:
    """Periodically deletes idempotency keys older than retention hours."""
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            cutoff = datetime.now(timezone.utc) - timedelta(hours=settings.idempotency_retention_hours)
            async with AsyncSessionLocal() as session:
                async with session.begin():
                    stmt = delete(IdempotencyKey).where(IdempotencyKey.created_at < cutoff)
                    res = await session.execute(stmt)
                    logger.info("Cleaned up %d expired idempotency keys.", res.rowcount)
        except asyncio.CancelledError:
            logger.info("Idempotency key cleanup task cancelled.")
            break
        except Exception as e:
            logger.error("Error in idempotency key cleanup task: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifecycle events for FastAPI application."""
    cleanup_task = None
    if not settings.testing:
        # Startup: Start key cleanup task if not in testing mode
        cleanup_task = asyncio.create_task(cleanup_expired_keys_loop())
    yield
    # Shutdown: Cancel key cleanup task
    if cleanup_task:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="Arcfield",
    description="Durable Game Economy Service",
    version="0.1.0",
    lifespan=lifespan,
)


def compute_request_hash(method: str, path: str, body: dict[str, Any]) -> str:
    """Computes a SHA-256 hash representing the unique request payload."""
    serialized_body = json.dumps(body, sort_keys=True)
    payload = f"{method.upper()}:{path}:{serialized_body}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@app.get("/health")
async def health() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "healthy"}


@app.get(
    "/v1/wallets/{playerId}",
    response_model=WalletResponse,
    responses={
        404: {"model": ErrorResponse, "description": "Wallet not found"},
    },
)
async def get_wallet(
    playerId: str = Path(..., max_length=128, pattern=r"^[a-zA-Z0-9_\-]+$"),
    db: AsyncSession = Depends(get_db),
) -> WalletResponse:
    """Retrieves the wallet details (balance) for a given player."""
    stmt = select(Wallet).where(Wallet.player_id == playerId)
    result = await db.execute(stmt)
    wallet = result.scalar_one_or_none()

    if wallet is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Wallet not found for player: {playerId}",
        )

    return WalletResponse(
        player_id=wallet.player_id,
        balance=wallet.balance,
    )


@app.post(
    "/v1/wallets/{playerId}/credit",
    response_model=CreditResponse,
    responses={
        400: {"model": ErrorResponse, "description": "Bad Request"},
        409: {"model": ErrorResponse, "description": "Conflict (In-flight request)"},
    },
)
async def credit_wallet(
    request_data: CreditRequest,
    playerId: str = Path(..., max_length=128, pattern=r"^[a-zA-Z0-9_\-]+$"),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Credits a player's wallet with the specified amount, enforcing idempotency."""
    # 1. Validate Idempotency-Key header is present
    if not idempotency_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key header is required.",
        )

    # Validate Idempotency-Key header is a valid UUID
    try:
        uuid.UUID(idempotency_key)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key must be a valid UUID.",
        ) from e

    # 2. Compute request fingerprint hash
    req_body = request_data.model_dump()
    request_hash = compute_request_hash("POST", f"/v1/wallets/{playerId}/credit", req_body)

    try:
        # 3. Perform idempotency check & business operation in the same transaction
        async with db.begin():
            # Try to insert the idempotency key first
            stmt = (
                pg_insert(IdempotencyKey)
                .values(
                    key=idempotency_key,
                    player_id=playerId,
                    operation="credit",
                    request_hash=request_hash,
                )
                .on_conflict_do_nothing()
                .returning(
                    IdempotencyKey.key,
                    IdempotencyKey.response_code,
                    IdempotencyKey.response_body,
                )
            )
            result = await db.execute(stmt)
            inserted_row = result.first()

            if inserted_row is None:
                # Key already exists. Fetch the committed record to replay or check if in-flight.
                existing = await db.get(IdempotencyKey, idempotency_key)
                if existing is None:
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail="Idempotency conflict resolution failed.",
                    )

                # Validate payload fingerprint match
                if existing.request_hash != request_hash:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Idempotency-Key was reused with a different request payload.",
                    )

                # If code is null, another concurrent request with the same key is currently processing
                if existing.response_code is None:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="A request with this Idempotency-Key is already in progress.",
                    )

                # Replay stored response
                stored_response = json.loads(existing.response_body)
                return JSONResponse(
                    status_code=existing.response_code,
                    content=stored_response,
                )

            # New request: Proceed to credit wallet
            # Ensure the wallet exists (Get or create pattern)
            wallet_insert = (
                pg_insert(Wallet)
                .values(player_id=playerId, balance=0)
                .on_conflict_do_nothing()
            )
            await db.execute(wallet_insert)

            # Select and lock the wallet row for update
            wallet_select = select(Wallet).where(Wallet.player_id == playerId).with_for_update()
            wallet_result = await db.execute(wallet_select)
            wallet = wallet_result.scalar_one()

            # Update the balance
            new_balance = wallet.balance + request_data.amount
            if new_balance > 2147483647:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Wallet balance would overflow maximum allowed value (2147483647).",
                )
            wallet.balance = new_balance

            # Add to append-only ledger
            ledger_entry = LedgerEntry(
                player_id=playerId,
                amount=request_data.amount,
                balance_after=new_balance,
                type="credit",
                reason="Wallet credit",
                reference_id=idempotency_key,
            )
            db.add(ledger_entry)

            # Construct response and serialize
            response_data = CreditResponse(
                player_id=playerId,
                balance=new_balance,
                reference_id=idempotency_key,
            )
            serialized_response = json.dumps(response_data.model_dump())

            # Save response to the idempotency key record
            update_key = (
                update(IdempotencyKey)
                .where(IdempotencyKey.key == idempotency_key)
                .values(
                    response_code=status.HTTP_200_OK,
                    response_body=serialized_response,
                )
            )
            await db.execute(update_key)

            return response_data

    except HTTPException:
        # Re-raise HTTP exceptions to let FastAPI handle them
        raise
    except Exception as e:
        logger.error("Database transaction error: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database transaction failed: {str(e)}",
        ) from e


@app.post(
    "/v1/wallets/{playerId}/purchase",
    response_model=PurchaseResponse,
    responses={
        400: {"model": ErrorResponse, "description": "Bad Request"},
        409: {"model": ErrorResponse, "description": "Conflict / Insufficient Funds / In-flight"},
    },
)
async def purchase_item(
    request_data: PurchaseRequest,
    playerId: str = Path(..., max_length=128, pattern=r"^[a-zA-Z0-9_\-]+$"),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Debits a player's wallet and grants an item, enforcing transactional safety and idempotency."""
    # 1. Validate Idempotency-Key header is present
    if not idempotency_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key header is required.",
        )

    # Validate Idempotency-Key header is a valid UUID
    try:
        uuid.UUID(idempotency_key)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key must be a valid UUID.",
        ) from e

    # 2. Compute request fingerprint hash
    req_body = request_data.model_dump()
    request_hash = compute_request_hash("POST", f"/v1/wallets/{playerId}/purchase", req_body)

    error_to_raise = None
    response_to_return = None

    try:
        # 3. Perform idempotency check & business operation in the same transaction
        async with db.begin():
            # Try to insert the idempotency key first
            stmt = (
                pg_insert(IdempotencyKey)
                .values(
                    key=idempotency_key,
                    player_id=playerId,
                    operation="purchase",
                    request_hash=request_hash,
                )
                .on_conflict_do_nothing()
                .returning(
                    IdempotencyKey.key,
                    IdempotencyKey.response_code,
                    IdempotencyKey.response_body,
                )
            )
            result = await db.execute(stmt)
            inserted_row = result.first()

            if inserted_row is None:
                # Key already exists. Fetch the committed record to replay or check if in-flight.
                existing = await db.get(IdempotencyKey, idempotency_key)
                if existing is None:
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail="Idempotency conflict resolution failed.",
                    )

                # Validate payload fingerprint match
                if existing.request_hash != request_hash:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Idempotency-Key was reused with a different request payload.",
                    )

                # If code is null, another concurrent request with the same key is currently processing
                if existing.response_code is None:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="A request with this Idempotency-Key is already in progress.",
                    )

                # Replay stored response
                stored_response = json.loads(existing.response_body)
                response_to_return = JSONResponse(
                    status_code=existing.response_code,
                    content=stored_response,
                )
                return response_to_return

            # New request: Proceed with purchase
            # Ensure the wallet exists (Get or create pattern)
            wallet_insert = (
                pg_insert(Wallet)
                .values(player_id=playerId, balance=0)
                .on_conflict_do_nothing()
            )
            await db.execute(wallet_insert)

            # Select and lock the wallet row for update
            wallet_select = select(Wallet).where(Wallet.player_id == playerId).with_for_update()
            wallet_result = await db.execute(wallet_select)
            wallet = wallet_result.scalar_one()

            # Check for sufficient funds
            if wallet.balance < request_data.price:
                error_detail = f"Insufficient funds: balance {wallet.balance} is less than price {request_data.price}."
                error_body = {"detail": error_detail}
                serialized_error = json.dumps(error_body)

                # Save 409 response in idempotency key
                update_key = (
                    update(IdempotencyKey)
                    .where(IdempotencyKey.key == idempotency_key)
                    .values(
                        response_code=status.HTTP_409_CONFLICT,
                        response_body=serialized_error,
                    )
                )
                await db.execute(update_key)

                error_to_raise = HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=error_detail,
                )
            else:
                # Debit wallet
                new_balance = wallet.balance - request_data.price
                wallet.balance = new_balance

                # Grant inventory item
                item = InventoryItem(
                    player_id=playerId,
                    item_id=request_data.item_id,
                )
                db.add(item)

                # Add to append-only ledger (with negative amount)
                ledger_entry = LedgerEntry(
                    player_id=playerId,
                    amount=-request_data.price,
                    balance_after=new_balance,
                    type="purchase_debit",
                    reason=f"Purchase of {request_data.item_id}",
                    reference_id=idempotency_key,
                )
                db.add(ledger_entry)

                # Construct successful response
                response_data = PurchaseResponse(
                    player_id=playerId,
                    balance=new_balance,
                    item_id=request_data.item_id,
                    reference_id=idempotency_key,
                )
                serialized_response = json.dumps(response_data.model_dump())

                # Save 200 response in idempotency key
                update_key = (
                    update(IdempotencyKey)
                    .where(IdempotencyKey.key == idempotency_key)
                    .values(
                        response_code=status.HTTP_200_OK,
                        response_body=serialized_response,
                    )
                )
                await db.execute(update_key)
                response_to_return = response_data

    except HTTPException:
        # Re-raise HTTP exceptions to let FastAPI handle them
        raise
    except Exception as e:
        logger.error("Database transaction error during purchase: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database transaction failed: {str(e)}",
        ) from e

    # Raise insufficient funds error if set
    if error_to_raise:
        raise error_to_raise

    return response_to_return


@app.post(
    "/v1/rewards/{rewardId}/claim",
    response_model=ClaimRewardResponse,
    responses={
        400: {"model": ErrorResponse, "description": "Bad Request"},
        409: {"model": ErrorResponse, "description": "Conflict / Reward already claimed / In-flight"},
    },
)
async def claim_reward(
    request_data: ClaimRewardRequest,
    rewardId: str = Path(..., max_length=128, pattern=r"^[a-zA-Z0-9_\-]+$"),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Claims a unique reward for a player, enforcing idempotency and safety constraints."""
    # 1. Validate Idempotency-Key header is present
    if not idempotency_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key header is required.",
        )

    # Validate Idempotency-Key header is a valid UUID
    try:
        uuid.UUID(idempotency_key)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key must be a valid UUID.",
        ) from e

    # 2. Compute request fingerprint hash
    req_body = request_data.model_dump()
    request_hash = compute_request_hash("POST", f"/v1/rewards/{rewardId}/claim", req_body)

    error_to_raise = None
    response_to_return = None

    try:
        # 3. Perform idempotency check & business operation in the same transaction
        async with db.begin():
            # Try to insert the idempotency key first
            stmt = (
                pg_insert(IdempotencyKey)
                .values(
                    key=idempotency_key,
                    player_id=request_data.player_id,
                    operation="claim_reward",
                    request_hash=request_hash,
                )
                .on_conflict_do_nothing()
                .returning(
                    IdempotencyKey.key,
                    IdempotencyKey.response_code,
                    IdempotencyKey.response_body,
                )
            )
            result = await db.execute(stmt)
            inserted_row = result.first()

            if inserted_row is None:
                # Key already exists. Fetch the committed record to replay or check if in-flight.
                existing = await db.get(IdempotencyKey, idempotency_key)
                if existing is None:
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail="Idempotency conflict resolution failed.",
                    )

                # Validate payload fingerprint match
                if existing.request_hash != request_hash:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Idempotency-Key was reused with a different request payload.",
                    )

                # If code is null, another concurrent request with the same key is currently processing
                if existing.response_code is None:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="A request with this Idempotency-Key is already in progress.",
                    )

                # Replay stored response
                stored_response = json.loads(existing.response_body)
                response_to_return = JSONResponse(
                    status_code=existing.response_code,
                    content=stored_response,
                )
                return response_to_return

            # New request: Proceed with claiming reward
            # Ensure the wallet exists (Get or create pattern)
            wallet_insert = (
                pg_insert(Wallet)
                .values(player_id=request_data.player_id, balance=0)
                .on_conflict_do_nothing()
            )
            await db.execute(wallet_insert)

            # Select and lock the wallet row for update to serialize player mutations
            wallet_select = select(Wallet).where(Wallet.player_id == request_data.player_id).with_for_update()
            await db.execute(wallet_select)

            # Check if reward has already been claimed
            claim_select = select(ClaimedReward).where(
                ClaimedReward.player_id == request_data.player_id,
                ClaimedReward.reward_id == rewardId,
            )
            claim_result = await db.execute(claim_select)
            existing_claim = claim_result.scalar_one_or_none()

            if existing_claim is not None:
                error_detail = f"Reward {rewardId} has already been claimed by player {request_data.player_id}."
                error_body = {"detail": error_detail}
                serialized_error = json.dumps(error_body)

                # Save 409 response in idempotency key
                update_key = (
                    update(IdempotencyKey)
                    .where(IdempotencyKey.key == idempotency_key)
                    .values(
                        response_code=status.HTTP_409_CONFLICT,
                        response_body=serialized_error,
                    )
                )
                await db.execute(update_key)

                error_to_raise = HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=error_detail,
                )
            else:
                # Grant reward
                reward = ClaimedReward(
                    player_id=request_data.player_id,
                    reward_id=rewardId,
                )
                db.add(reward)

                # Construct successful response
                response_data = ClaimRewardResponse(
                    player_id=request_data.player_id,
                    reward_id=rewardId,
                    reference_id=idempotency_key,
                )
                serialized_response = json.dumps(response_data.model_dump())

                # Save 200 response in idempotency key
                update_key = (
                    update(IdempotencyKey)
                    .where(IdempotencyKey.key == idempotency_key)
                    .values(
                        response_code=status.HTTP_200_OK,
                        response_body=serialized_response,
                    )
                )
                await db.execute(update_key)
                response_to_return = response_data

    except HTTPException:
        # Re-raise HTTP exceptions to let FastAPI handle them
        raise
    except Exception as e:
        logger.error("Database transaction error during reward claim: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database transaction failed: {str(e)}",
        ) from e

    # Raise duplicate claim error if set
    if error_to_raise:
        raise error_to_raise

    return response_to_return

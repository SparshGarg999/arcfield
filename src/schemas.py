"""Pydantic schemas for request and response validation."""

from pydantic import BaseModel, Field


class CreditRequest(BaseModel):
    """Schema for credit requests."""
    amount: int = Field(
        ...,
        gt=0,
        le=2147483647,
        description="Amount to credit to the wallet, must be a positive integer <= 2147483647.",
        examples=[100],
    )


class CreditResponse(BaseModel):
    """Schema for successful credit response."""
    player_id: str = Field(..., description="The ID of the player whose wallet was credited.")
    balance: int = Field(..., description="The updated balance of the wallet.")
    reference_id: str = Field(..., description="The idempotency key reference associated with the operation.")


class WalletResponse(BaseModel):
    """Schema for wallet details response."""
    player_id: str = Field(..., description="The ID of the player.")
    balance: int = Field(..., description="The current balance of the wallet.")


class PurchaseRequest(BaseModel):
    """Schema for item purchase requests."""
    price: int = Field(
        ...,
        gt=0,
        le=2147483647,
        description="Price of the item, must be a positive integer <= 2147483647.",
        examples=[50],
    )
    item_id: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="ID of the item being purchased.",
        examples=["sword_001"],
    )


class PurchaseResponse(BaseModel):
    """Schema for successful purchase response."""
    player_id: str = Field(..., description="The ID of the player.")
    balance: int = Field(..., description="The updated balance after purchase.")
    item_id: str = Field(..., description="The ID of the purchased item.")
    reference_id: str = Field(..., description="The idempotency key reference associated with the operation.")


class ClaimRewardRequest(BaseModel):
    """Schema for reward claim request."""
    player_id: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="ID of the player claiming the reward.",
        examples=["player_123"],
    )


class ClaimRewardResponse(BaseModel):
    """Schema for successful reward claim response."""
    player_id: str = Field(..., description="The ID of the player.")
    reward_id: str = Field(..., description="The ID of the reward claimed.")
    reference_id: str = Field(..., description="The idempotency key reference associated with the operation.")


class ErrorResponse(BaseModel):
    """Schema for error responses."""
    detail: str = Field(..., description="Detail message explaining the error.")

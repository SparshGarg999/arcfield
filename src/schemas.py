"""Pydantic schemas for request and response validation."""

from pydantic import BaseModel, Field, PositiveInt


class CreditRequest(BaseModel):
    """Schema for credit requests."""
    amount: PositiveInt = Field(
        ...,
        description="Amount to credit to the wallet, must be a positive integer.",
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


class ErrorResponse(BaseModel):
    """Schema for error responses."""
    detail: str = Field(..., description="Detail message explaining the error.")

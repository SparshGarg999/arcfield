"""SQLAlchemy database models for Arcfield."""

from datetime import datetime
from typing import Optional
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy models."""
    pass


class Wallet(Base):
    """Player wallet tracking the balance."""
    __tablename__ = "wallets"

    player_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    balance: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=func.now(),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=func.now(),
        server_default=func.now(),
        onupdate=func.now(),
    )

    # Relationships
    ledger_entries: Mapped[list["LedgerEntry"]] = relationship(
        "LedgerEntry",
        back_populates="wallet",
        cascade="all, delete-orphan",
    )
    inventory_items: Mapped[list["InventoryItem"]] = relationship(
        "InventoryItem",
        back_populates="wallet",
        cascade="all, delete-orphan",
    )
    claimed_rewards: Mapped[list["ClaimedReward"]] = relationship(
        "ClaimedReward",
        back_populates="wallet",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        CheckConstraint("balance >= 0", name="chk_wallet_balance_non_negative"),
    )

    def __repr__(self) -> str:
        return f"<Wallet(player_id={self.player_id!r}, balance={self.balance!r})>"


class LedgerEntry(Base):
    """Append-only audit log for all balance modifications."""
    __tablename__ = "ledger"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    player_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("wallets.player_id", ondelete="CASCADE"),
        nullable=False,
    )
    amount: Mapped[int] = mapped_column(Integer, nullable=False)  # positive = credit, negative = debit
    balance_after: Mapped[int] = mapped_column(Integer, nullable=False)
    type: Mapped[str] = mapped_column(String(64), nullable=False)  # 'credit', 'purchase_debit', 'reward_credit'
    reason: Mapped[str] = mapped_column(String(256), nullable=False, default="", server_default="")
    reference_id: Mapped[str] = mapped_column(String(128), nullable=False)  # Idempotency Key reference
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=func.now(),
        server_default=func.now(),
    )

    # Relationships
    wallet: Mapped["Wallet"] = relationship("Wallet", back_populates="ledger_entries")

    __table_args__ = (
        CheckConstraint("balance_after >= 0", name="chk_ledger_balance_after_non_negative"),
        Index("idx_ledger_player_id", "player_id"),
        Index("idx_ledger_reference_id", "reference_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<LedgerEntry(id={self.id!r}, player_id={self.player_id!r}, "
            f"amount={self.amount!r}, balance_after={self.balance_after!r}, "
            f"type={self.type!r}, reference_id={self.reference_id!r})>"
        )


class InventoryItem(Base):
    """Items purchased or acquired by players. A player can have duplicates of an item."""
    __tablename__ = "inventory"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    player_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("wallets.player_id", ondelete="CASCADE"),
        nullable=False,
    )
    item_id: Mapped[str] = mapped_column(String(128), nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=func.now(),
        server_default=func.now(),
    )

    # Relationships
    wallet: Mapped["Wallet"] = relationship("Wallet", back_populates="inventory_items")

    __table_args__ = (
        Index("idx_inventory_player_id", "player_id"),
    )

    def __repr__(self) -> str:
        return f"<InventoryItem(id={self.id!r}, player_id={self.player_id!r}, item_id={self.item_id!r})>"


class ClaimedReward(Base):
    """Rewards claimed by players. Enforces a one-claim-per-player-per-reward constraint."""
    __tablename__ = "claimed_rewards"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    player_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("wallets.player_id", ondelete="CASCADE"),
        nullable=False,
    )
    reward_id: Mapped[str] = mapped_column(String(128), nullable=False)
    claimed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=func.now(),
        server_default=func.now(),
    )

    # Relationships
    wallet: Mapped["Wallet"] = relationship("Wallet", back_populates="claimed_rewards")

    __table_args__ = (
        UniqueConstraint("player_id", "reward_id", name="uq_player_reward"),
        Index("idx_claimed_rewards_player_id", "player_id"),
    )

    def __repr__(self) -> str:
        return f"<ClaimedReward(player_id={self.player_id!r}, reward_id={self.reward_id!r})>"


class IdempotencyKey(Base):
    """Deduplication store tracking request execution state and replaying original responses."""
    __tablename__ = "idempotency_keys"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    player_id: Mapped[str] = mapped_column(String(128), nullable=False)
    operation: Mapped[str] = mapped_column(String(64), nullable=False)  # 'credit', 'purchase', 'claim_reward'
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)  # SHA-256 hash of method + path + body
    response_code: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    response_body: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=func.now(),
        server_default=func.now(),
    )

    __table_args__ = (
        Index("idx_idempotency_keys_created_at", "created_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<IdempotencyKey(key={self.key!r}, player_id={self.player_id!r}, "
            f"operation={self.operation!r}, response_code={self.response_code!r})>"
        )

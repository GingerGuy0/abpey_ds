from datetime import datetime, timezone
from decimal import Decimal
import enum
from typing import Optional

from sqlalchemy import (
    String, Numeric, DateTime, Boolean, ForeignKey, Index, BigInteger, Text, JSON,
    TypeDecorator
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.sql import func


class UtcDateTime(TypeDecorator):
    """DateTime, який ЗАВЖДИ повертає timezone-aware UTC.

    Навіщо: SQLite не вміє зберігати таймзону. Навіть з DateTime(timezone=True)
    він віддає naive datetime, і будь-яке порівняння з datetime.now(timezone.utc)
    падає з "can't subtract offset-naive and offset-aware datetimes".

    Цей тип:
      - на запису приводить будь-який datetime до UTC (naive вважається UTC);
      - на читанні чіпляє tzinfo=UTC, якщо його немає.

    На Postgres поведінка не змінюється — там timestamptz і так aware.
    Завдяки цьому весь інший код може беззастережно вважати, що дати з БД aware.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class InvoiceStatus(enum.StrEnum):
    pending = "pending"
    confirming = "confirming"
    paid = "paid"
    expired = "expired"
    cancelled = "cancelled"
    underpaid = "underpaid"
    manual_review = "manual_review"


class SubscriptionSource(enum.StrEnum):
    payment = "payment"
    admin_gift = "admin_gift"


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[Optional[str]] = mapped_column(String(255))
    first_name: Mapped[Optional[str]] = mapped_column(String(255))
    language_code: Mapped[Optional[str]] = mapped_column(String(10))
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)
    is_blocked_bot: Mapped[bool] = mapped_column(Boolean, default=False)
    
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, server_default=func.now(), onupdate=func.now()
    )

    subscriptions: Mapped[list["Subscription"]] = relationship(back_populates="user", lazy="selectin")
    invoices: Mapped[list["Invoice"]] = relationship(back_populates="user", lazy="selectin")

    def __repr__(self) -> str:
        return f"<User id={self.id} tg_id={self.tg_id}>"


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    plan: Mapped[str] = mapped_column(String(50))
    source: Mapped[str] = mapped_column(String(50))
    
    starts_at: Mapped[datetime] = mapped_column(UtcDateTime)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    
    invite_link: Mapped[Optional[str]] = mapped_column(String(255))
    invite_link_created_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime)
    
    notified_3d: Mapped[bool] = mapped_column(Boolean, default=False)
    notified_1d: Mapped[bool] = mapped_column(Boolean, default=False)
    kicked_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime)
    
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, server_default=func.now())

    user: Mapped["User"] = relationship(back_populates="subscriptions", lazy="selectin")

    __table_args__ = (
        Index("idx_subscriptions_active_expires", "is_active", "expires_at"),
    )

    def __repr__(self) -> str:
        return f"<Subscription id={self.id} user_id={self.user_id} active={self.is_active}>"


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    plan: Mapped[str] = mapped_column(String(50))
    network: Mapped[str] = mapped_column(String(50))
    
    base_amount_usd: Mapped[Decimal] = mapped_column(Numeric)
    expected_amount: Mapped[Decimal] = mapped_column(Numeric(30, 8))
    expected_units: Mapped[Decimal] = mapped_column(Numeric(40, 0))
    
    pay_address: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(50))
    
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime)
    
    tx_hash: Mapped[Optional[str]] = mapped_column(String(255))
    from_address: Mapped[Optional[str]] = mapped_column(String(255))
    
    received_amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(30, 8))
    received_units: Mapped[Optional[Decimal]] = mapped_column(Numeric(40, 0))
    confirmations: Mapped[Optional[int]] = mapped_column(BigInteger)
    
    paid_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime)
    attempts: Mapped[int] = mapped_column(BigInteger, default=0)
    last_attempt_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime)

    user: Mapped["User"] = relationship(back_populates="invoices", lazy="selectin")

    __table_args__ = (
        Index("idx_invoices_status_network", "status", "network"),
        Index("idx_invoices_user_status", "user_id", "status"),
    )

    def __repr__(self) -> str:
        return f"<Invoice id={self.id} status={self.status}>"


class ProcessedTx(Base):
    __tablename__ = "processed_txs"

    id: Mapped[int] = mapped_column(primary_key=True)
    tx_hash: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    network: Mapped[str] = mapped_column(String(50))
    invoice_id: Mapped[Optional[int]] = mapped_column(ForeignKey("invoices.id"))
    
    amount: Mapped[Decimal] = mapped_column(Numeric(30, 8))
    from_address: Mapped[str] = mapped_column(String(255))
    tx_time: Mapped[datetime] = mapped_column(UtcDateTime)
    
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, server_default=func.now())

    def __repr__(self) -> str:
        return f"<ProcessedTx id={self.id} tx_hash={self.tx_hash}>"


class OrphanPayment(Base):
    __tablename__ = "orphan_payments"

    id: Mapped[int] = mapped_column(primary_key=True)
    network: Mapped[str] = mapped_column(String(50))
    tx_hash: Mapped[str] = mapped_column(String(255), unique=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(30, 8))
    from_address: Mapped[str] = mapped_column(String(255))
    tx_time: Mapped[datetime] = mapped_column(UtcDateTime)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, server_default=func.now())

    def __repr__(self) -> str:
        return f"<OrphanPayment id={self.id} tx_hash={self.tx_hash}>"


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return f"<Setting key={self.key}>"


class AdminAction(Base):
    __tablename__ = "admin_actions"

    id: Mapped[int] = mapped_column(primary_key=True)
    admin_tg_id: Mapped[int] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(String(255))
    target_user_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    payload: Mapped[Optional[dict]] = mapped_column(JSON)
    
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, server_default=func.now())

    def __repr__(self) -> str:
        return f"<AdminAction id={self.id} action={self.action}>"

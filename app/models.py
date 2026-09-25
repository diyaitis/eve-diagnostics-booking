"""Database schema.

Design notes (see README for the ER diagram):
  * Prices live on ``centre_tests`` (a test costs different amounts at different centres).
  * A booking stores its own ``amount`` - a snapshot of the price when it was made - so later
    price changes never rewrite history or change what a customer owes.
  * ``bookings`` has a composite foreign key to ``centre_tests``: the database itself guarantees
    that a booking can only exist for a test the centre actually offers.
  * ``payments`` has a partial unique index: at most one PENDING/SUCCESS payment per booking.
  * ``webhook_events.event_id`` is unique - that constraint is what makes the webhook idempotent.
"""

import enum
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import expression
from sqlalchemy.types import TypeDecorator, Uuid

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """timestamptz that always round-trips as a timezone-aware UTC datetime (also on SQLite)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Naive datetimes are not allowed; use timezone-aware values")
        return value.astimezone(timezone.utc)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class BookingStatus(str, enum.Enum):
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class PaymentStatus(str, enum.Enum):
    PENDING = "PENDING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class WebhookResult(str, enum.Enum):
    APPLIED = "APPLIED"  # the event changed the payment (and usually the booking)
    NO_CHANGE = "NO_CHANGE"  # the payment was already in the reported state
    IGNORED = "IGNORED"  # the payment was already final with a different state; kept as is


def _enum(enum_cls: type[enum.Enum], name: str) -> Enum:
    # Stored as VARCHAR + CHECK constraint rather than a native PG enum: easier to evolve.
    return Enum(enum_cls, native_enum=False, create_constraint=True, length=16, name=name)


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    full_name: Mapped[str] = mapped_column(String(120))
    hashed_password: Mapped[str] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, server_default=expression.false())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Centre(Base):
    __tablename__ = "centres"
    __table_args__ = (UniqueConstraint("name", "location", name="uq_centre_name_location"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(150))
    location: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    offerings: Mapped[list["CentreTest"]] = relationship(
        back_populates="centre", cascade="all, delete-orphan", order_by="CentreTest.test_id"
    )


class DiagnosticTest(Base):
    __tablename__ = "diagnostic_tests"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(150), unique=True)
    description: Mapped[str | None] = mapped_column(Text)


class CentreTest(Base):
    """A test offered by a centre, with that centre's price."""

    __tablename__ = "centre_tests"
    __table_args__ = (CheckConstraint("price >= 0", name="ck_centre_test_price_non_negative"),)

    centre_id: Mapped[int] = mapped_column(ForeignKey("centres.id", ondelete="CASCADE"), primary_key=True)
    test_id: Mapped[int] = mapped_column(ForeignKey("diagnostic_tests.id"), primary_key=True)
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2))

    centre: Mapped[Centre] = relationship(back_populates="offerings")
    test: Mapped[DiagnosticTest] = relationship(lazy="joined")

    @property
    def name(self) -> str:
        return self.test.name


class Booking(Base):
    __tablename__ = "bookings"
    __table_args__ = (
        ForeignKeyConstraint(
            ["centre_id", "test_id"],
            ["centre_tests.centre_id", "centre_tests.test_id"],
            name="fk_booking_centre_offers_test",
        ),
        CheckConstraint("amount >= 0", name="ck_booking_amount_non_negative"),
        Index("ix_bookings_user_created", "user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    centre_id: Mapped[int] = mapped_column()
    test_id: Mapped[int] = mapped_column()
    appointment_at: Mapped[datetime] = mapped_column(UTCDateTime)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    status: Mapped[BookingStatus] = mapped_column(
        _enum(BookingStatus, "booking_status"), default=BookingStatus.PENDING
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    offering: Mapped[CentreTest] = relationship(viewonly=True)
    payments: Mapped[list["Payment"]] = relationship(
        back_populates="booking", lazy="selectin", order_by="Payment.created_at"
    )

    @property
    def centre_name(self) -> str:
        return self.offering.centre.name

    @property
    def test_name(self) -> str:
        return self.offering.test.name


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (
        # At most one live (PENDING/SUCCESS) payment per booking, enforced by the database so it
        # holds even when two requests race.
        Index(
            "uq_payments_one_live_per_booking",
            "booking_id",
            unique=True,
            postgresql_where=text("status IN ('PENDING', 'SUCCESS')"),
            sqlite_where=text("status IN ('PENDING', 'SUCCESS')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    booking_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("bookings.id"), index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    status: Mapped[PaymentStatus] = mapped_column(
        _enum(PaymentStatus, "payment_status"), default=PaymentStatus.PENDING
    )
    # The provider-side identifier; webhooks refer to a payment by this value.
    provider_reference: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    booking: Mapped[Booking] = relationship(back_populates="payments")


class WebhookEvent(Base):
    """Every webhook event that has been processed. ``event_id`` is the idempotency key."""

    __tablename__ = "webhook_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[str] = mapped_column(String(128), unique=True)
    payment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("payments.id"), index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    result: Mapped[WebhookResult] = mapped_column(_enum(WebhookResult, "webhook_result"))
    received_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

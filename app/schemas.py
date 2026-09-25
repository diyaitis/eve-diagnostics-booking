import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Generic, TypeVar

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    PlainSerializer,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.models import BookingStatus, PaymentStatus, WebhookResult
from app.security import MAX_PASSWORD_BYTES

# Money is exchanged as a fixed 2-decimal string ("500.00") so it never suffers float rounding.
Money = Annotated[Decimal, PlainSerializer(lambda v: f"{v:.2f}", return_type=str, when_used="json")]
Price = Annotated[Decimal, Field(ge=0, max_digits=10, decimal_places=2)]

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=150)]
Location = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]

T = TypeVar("T")


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


# --------------------------------------------------------------------------- auth
class SignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]

    @field_validator("email")
    @classmethod
    def _lowercase_email(cls, value: str) -> str:
        return value.lower()

    @field_validator("password")
    @classmethod
    def _fits_bcrypt(cls, value: str) -> str:
        if len(value.encode()) > MAX_PASSWORD_BYTES:
            raise ValueError(f"Password must be at most {MAX_PASSWORD_BYTES} bytes")
        return value


class UserOut(ORMModel):
    id: uuid.UUID
    email: str
    full_name: str
    is_admin: bool


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


# ------------------------------------------------------------------------ catalogue
class DiagnosticTestCreate(BaseModel):
    name: Name
    description: str | None = Field(default=None, max_length=2000)


class DiagnosticTestOut(ORMModel):
    id: int
    name: str
    description: str | None


class CentreCreate(BaseModel):
    name: Name
    location: Location


class CentreUpdate(BaseModel):
    name: Name | None = None
    location: Location | None = None

    @model_validator(mode="after")
    def _at_least_one_field(self):
        if self.name is None and self.location is None:
            raise ValueError("Provide at least one of: name, location")
        return self


class OfferingUpsert(BaseModel):
    price: Price


class OfferedTestOut(ORMModel):
    test_id: int
    name: str
    price: Money


class CentreOut(ORMModel):
    id: int
    name: str
    location: str
    tests: list[OfferedTestOut] = Field(validation_alias="offerings")


# ------------------------------------------------------------------------ bookings
class BookingCreate(BaseModel):
    centre_id: int
    test_id: int
    appointment_at: AwareDatetime  # must carry a timezone / UTC offset


class PaymentOut(ORMModel):
    id: uuid.UUID
    booking_id: uuid.UUID
    amount: Money
    status: PaymentStatus
    provider_reference: str
    created_at: datetime


class BookingOut(ORMModel):
    id: uuid.UUID
    centre_id: int
    test_id: int
    centre_name: str
    test_name: str
    appointment_at: datetime
    amount: Money
    status: BookingStatus
    created_at: datetime
    payments: list[PaymentOut]


# ------------------------------------------------------------------------ payments
class PaymentCreate(BaseModel):
    booking_id: uuid.UUID
    # Testing aid for the simulated provider: force a specific outcome. Leave out for a random one.
    simulate_outcome: PaymentStatus | None = None


class WebhookIn(BaseModel):
    event_id: str = Field(min_length=1, max_length=128)
    provider_reference: str = Field(min_length=1, max_length=64)
    status: PaymentStatus

    @field_validator("status")
    @classmethod
    def _final_status_only(cls, value: PaymentStatus) -> PaymentStatus:
        if value == PaymentStatus.PENDING:
            raise ValueError("status must be SUCCESS or FAILED")
        return value


class WebhookOut(BaseModel):
    event_id: str
    result: WebhookResult
    duplicate: bool

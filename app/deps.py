from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Query, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models import User
from app.security import decode_access_token
from app.services.mock_provider import MockPaymentProvider

DbSession = Annotated[Session, Depends(get_db)]

# tokenUrl makes the "Authorize" button in the Swagger UI work.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


def get_current_user(token: Annotated[str, Depends(oauth2_scheme)], db: DbSession) -> User:
    user_id = decode_access_token(token)
    user = db.get(User, user_id) if user_id is not None else None
    if user is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_admin(user: CurrentUser) -> User:
    if not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin access required")
    return user


AdminUser = Annotated[User, Depends(require_admin)]


def get_payment_provider() -> MockPaymentProvider:
    return MockPaymentProvider(get_settings().payment_success_rate)


PaymentProvider = Annotated[MockPaymentProvider, Depends(get_payment_provider)]


@dataclass
class PageParams:
    limit: Annotated[int, Query(ge=1, le=100, description="Page size")] = 20
    offset: Annotated[int, Query(ge=0, description="Rows to skip")] = 0


Pagination = Annotated[PageParams, Depends()]


def page(items: list, total: int, params: PageParams) -> dict:
    return {"items": items, "total": total, "limit": params.limit, "offset": params.offset}

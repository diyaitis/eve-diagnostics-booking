from functools import lru_cache

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.errors import ConflictError
from app.models import User
from app.security import hash_password, verify_password


def create_user(db: Session, email: str, password: str, full_name: str, is_admin: bool = False) -> User:
    user = User(
        email=email.lower(),
        full_name=full_name,
        hashed_password=hash_password(password),
        is_admin=is_admin,
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        # The unique constraint on users.email is the source of truth (safe under concurrency).
        db.rollback()
        raise ConflictError("An account with this email already exists") from None
    return user


@lru_cache
def _dummy_hash() -> str:
    return hash_password("not-a-real-password")


def authenticate(db: Session, email: str, password: str) -> User | None:
    user = db.scalar(select(User).where(User.email == email.lower()))
    if user is None:
        # Spend the same time as a real check so response timing doesn't reveal which emails exist.
        verify_password(password, _dummy_hash())
        return None
    return user if verify_password(password, user.hashed_password) else None

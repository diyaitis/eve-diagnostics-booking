from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm

from app.deps import CurrentUser, DbSession
from app.ratelimit import login_limit, signup_limit
from app.schemas import SignupRequest, TokenOut, UserOut
from app.security import create_access_token
from app.services import users

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/signup", response_model=UserOut, status_code=status.HTTP_201_CREATED, dependencies=[Depends(signup_limit)]
)
def signup(data: SignupRequest, db: DbSession):
    return users.create_user(db, data.email, data.password, data.full_name)


@router.post("/login", response_model=TokenOut, dependencies=[Depends(login_limit)])
def login(form: Annotated[OAuth2PasswordRequestForm, Depends()], db: DbSession):
    """Form-encoded login (`username` is the email) so the Swagger "Authorize" button works."""
    user = users.authenticate(db, form.username, form.password)
    if user is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return TokenOut(access_token=create_access_token(user.id))


@router.get("/me", response_model=UserOut)
def me(user: CurrentUser):
    return user

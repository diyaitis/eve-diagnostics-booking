from typing import Annotated

from fastapi import APIRouter, Query, status
from fastapi.responses import JSONResponse

from app import cache
from app.deps import AdminUser, DbSession, Pagination, page
from app.schemas import (
    CentreCreate,
    CentreOut,
    CentreUpdate,
    DiagnosticTestCreate,
    DiagnosticTestOut,
    OfferingUpsert,
    Page,
)
from app.services import catalog

router = APIRouter(tags=["catalogue"])


def _cached(namespace: str, params: dict, model, build) -> JSONResponse:
    """Serves a catalogue read from Redis when possible. ``X-Cache`` says HIT or MISS (absent if no cache).

    The body is built with the same response model the route documents, so cached and uncached
    responses are identical.
    """
    value, hit = cache.cached_json(namespace, params, lambda: model.model_validate(build()).model_dump(mode="json"))
    headers = {} if hit is None else {"X-Cache": "HIT" if hit else "MISS"}
    return JSONResponse(value, headers=headers)


@router.get("/centres", response_model=Page[CentreOut])
def list_centres(
    db: DbSession,
    pagination: Pagination,
    q: Annotated[str | None, Query(description="Match on centre name or location")] = None,
    test_id: Annotated[int | None, Query(description="Only centres that offer this test")] = None,
):
    params = {"q": q, "test_id": test_id, "limit": pagination.limit, "offset": pagination.offset}

    def build():
        items, total = catalog.list_centres(db, q, test_id, pagination.limit, pagination.offset)
        return page(items, total, pagination)

    return _cached("centres", params, Page[CentreOut], build)


@router.get("/centres/{centre_id}", response_model=CentreOut)
def get_centre(centre_id: int, db: DbSession):
    return _cached("centre", {"id": centre_id}, CentreOut, lambda: catalog.get_centre(db, centre_id))


@router.post("/centres", response_model=CentreOut, status_code=status.HTTP_201_CREATED)
def create_centre(data: CentreCreate, db: DbSession, _admin: AdminUser):
    return catalog.create_centre(db, data)


@router.patch("/centres/{centre_id}", response_model=CentreOut)
def update_centre(centre_id: int, data: CentreUpdate, db: DbSession, _admin: AdminUser):
    return catalog.update_centre(db, centre_id, data)


@router.put("/centres/{centre_id}/tests/{test_id}", response_model=CentreOut)
def set_test_price(centre_id: int, test_id: int, data: OfferingUpsert, db: DbSession, _admin: AdminUser):
    """Make a centre offer a test at a price, or change that price. Existing bookings keep their amount."""
    return catalog.set_offering_price(db, centre_id, test_id, data.price)


@router.get("/tests", response_model=Page[DiagnosticTestOut])
def list_tests(
    db: DbSession,
    pagination: Pagination,
    q: Annotated[str | None, Query(description="Match on test name")] = None,
):
    params = {"q": q, "limit": pagination.limit, "offset": pagination.offset}

    def build():
        items, total = catalog.list_tests(db, q, pagination.limit, pagination.offset)
        return page(items, total, pagination)

    return _cached("tests", params, Page[DiagnosticTestOut], build)


@router.post("/tests", response_model=DiagnosticTestOut, status_code=status.HTTP_201_CREATED)
def create_test(data: DiagnosticTestCreate, db: DbSession, _admin: AdminUser):
    return catalog.create_test(db, data)

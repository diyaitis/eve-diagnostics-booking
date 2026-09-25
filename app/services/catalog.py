from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.errors import ConflictError, NotFoundError
from app.models import Centre, CentreTest, DiagnosticTest
from app.schemas import CentreCreate, CentreUpdate, DiagnosticTestCreate
from app.services.pagination import paginate


def list_centres(
    db: Session, q: str | None, test_id: int | None, limit: int, offset: int
) -> tuple[list[Centre], int]:
    stmt = select(Centre).options(selectinload(Centre.offerings)).order_by(Centre.id)
    if q:
        stmt = stmt.where(Centre.name.icontains(q, autoescape=True) | Centre.location.icontains(q, autoescape=True))
    if test_id is not None:
        stmt = stmt.where(Centre.id.in_(select(CentreTest.centre_id).where(CentreTest.test_id == test_id)))
    return paginate(db, stmt, limit, offset)


def get_centre(db: Session, centre_id: int) -> Centre:
    centre = db.get(Centre, centre_id)
    if centre is None:
        raise NotFoundError("Centre not found")
    return centre


def create_centre(db: Session, data: CentreCreate) -> Centre:
    centre = Centre(name=data.name, location=data.location)
    db.add(centre)
    _commit_or_conflict(db, "A centre with this name and location already exists")
    return centre


def update_centre(db: Session, centre_id: int, data: CentreUpdate) -> Centre:
    centre = get_centre(db, centre_id)
    if data.name is not None:
        centre.name = data.name
    if data.location is not None:
        centre.location = data.location
    _commit_or_conflict(db, "A centre with this name and location already exists")
    return centre


def list_tests(db: Session, q: str | None, limit: int, offset: int) -> tuple[list[DiagnosticTest], int]:
    stmt = select(DiagnosticTest).order_by(DiagnosticTest.id)
    if q:
        stmt = stmt.where(DiagnosticTest.name.icontains(q, autoescape=True))
    return paginate(db, stmt, limit, offset)


def create_test(db: Session, data: DiagnosticTestCreate) -> DiagnosticTest:
    test = DiagnosticTest(name=data.name, description=data.description)
    db.add(test)
    _commit_or_conflict(db, "A test with this name already exists")
    return test


def set_offering_price(db: Session, centre_id: int, test_id: int, price: Decimal) -> Centre:
    """Makes a centre offer a test at ``price``, or changes the price if it already does.

    Existing bookings are not affected: each booking keeps the amount it was created with.
    """
    centre = get_centre(db, centre_id)
    if db.get(DiagnosticTest, test_id) is None:
        raise NotFoundError("Test not found")

    offering = db.get(CentreTest, (centre_id, test_id))
    if offering is None:
        db.add(CentreTest(centre_id=centre_id, test_id=test_id, price=price))
    else:
        offering.price = price
    db.commit()
    db.expire(centre, ["offerings"])
    return centre


def _commit_or_conflict(db: Session, message: str) -> None:
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ConflictError(message) from None

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session


def paginate(db: Session, stmt: Select, limit: int, offset: int) -> tuple[list, int]:
    """Runs ``stmt`` one page at a time and also returns the total number of matching rows.

    ``stmt`` must have a stable ORDER BY, otherwise pages can overlap or skip rows.
    """
    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    items = db.scalars(stmt.limit(limit).offset(offset)).unique().all()
    return list(items), total

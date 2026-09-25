"""Loads demo data and an admin user. Safe to run repeatedly.

    python -m app.seed

The admin credentials are for local development only; override them with
SEED_ADMIN_EMAIL / SEED_ADMIN_PASSWORD.
"""

import os
from decimal import Decimal

from sqlalchemy import select

from app import models  # noqa: F401
from app.database import Base, SessionLocal, engine
from app.models import Centre, DiagnosticTest, User
from app.schemas import CentreCreate, DiagnosticTestCreate
from app.services import catalog, users

TESTS = {
    "Complete Blood Count (CBC)": "Measures red cells, white cells and platelets",
    "Lipid Profile": "Cholesterol and triglycerides",
    "HbA1c": "Three-month average blood sugar",
    "Thyroid Profile (T3, T4, TSH)": "Thyroid function",
    "Vitamin D (25-OH)": "Vitamin D level",
}

# (name, location) -> {test name: price}
CENTRES = {
    ("EVE Diagnostics - Koramangala", "Bengaluru"): {
        "Complete Blood Count (CBC)": "350.00",
        "Lipid Profile": "600.00",
        "HbA1c": "550.00",
    },
    ("EVE Diagnostics - Indiranagar", "Bengaluru"): {
        "Complete Blood Count (CBC)": "400.00",
        "Thyroid Profile (T3, T4, TSH)": "750.00",
        "Vitamin D (25-OH)": "1200.00",
    },
    ("EVE Diagnostics - Bandra", "Mumbai"): {
        "Complete Blood Count (CBC)": "450.00",
        "Lipid Profile": "650.00",
        "Vitamin D (25-OH)": "1100.00",
    },
}


def main() -> None:
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        email = os.environ.get("SEED_ADMIN_EMAIL", "admin@example.com").lower()
        password = os.environ.get("SEED_ADMIN_PASSWORD", "admin12345")
        if db.scalar(select(User).where(User.email == email)) is None:
            users.create_user(db, email, password, "Admin", is_admin=True)
            print(f"created admin {email}")

        tests_by_name = {}
        for name, description in TESTS.items():
            test = db.scalar(select(DiagnosticTest).where(DiagnosticTest.name == name))
            if test is None:
                test = catalog.create_test(db, DiagnosticTestCreate(name=name, description=description))
            tests_by_name[name] = test

        for (name, location), prices in CENTRES.items():
            centre = db.scalar(select(Centre).where(Centre.name == name, Centre.location == location))
            if centre is None:
                centre = catalog.create_centre(db, CentreCreate(name=name, location=location))
            for test_name, price in prices.items():
                catalog.set_offering_price(db, centre.id, tests_by_name[test_name].id, Decimal(price))

    print("seed complete")


if __name__ == "__main__":
    main()

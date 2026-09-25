import random
from decimal import Decimal

from app.models import PaymentStatus


class MockPaymentProvider:
    """Stands in for a real payment gateway.

    ``charge`` decides the outcome of a payment. Tests (and demos) can force an outcome;
    otherwise it succeeds with probability ``success_rate``. Returning PENDING means "still being
    processed" - the final result then arrives later through the webhook.
    """

    def __init__(self, success_rate: float = 0.8, rng: random.Random | None = None):
        self.success_rate = success_rate
        self._rng = rng or random.Random()

    def charge(self, reference: str, amount: Decimal, forced: PaymentStatus | None = None) -> PaymentStatus:
        if forced is not None:
            return forced
        return PaymentStatus.SUCCESS if self._rng.random() < self.success_rate else PaymentStatus.FAILED

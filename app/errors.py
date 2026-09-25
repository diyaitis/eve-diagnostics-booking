class DomainError(Exception):
    """A business-rule violation. Turned into a JSON error response by the API layer."""

    status_code = 400

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


class NotFoundError(DomainError):
    status_code = 404


class ConflictError(DomainError):
    status_code = 409


class ForbiddenError(DomainError):
    status_code = 403


class UnprocessableError(DomainError):
    status_code = 422

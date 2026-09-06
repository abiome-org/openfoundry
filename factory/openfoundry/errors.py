from __future__ import annotations

from typing import Any


class OpenFoundryError(Exception):
    code = "openfoundry_error"
    status_code = 400
    default_retryable = False
    default_remediation: tuple[dict[str, str], ...] = ()

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        retryable: bool | None = None,
        remediation: list[dict[str, str]] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}
        self.retryable = self.default_retryable if retryable is None else retryable
        self.remediation = list(self.default_remediation if remediation is None else remediation)

    def as_dict(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "retryable": self.retryable,
                "remediation": self.remediation,
                "details": self.details,
            }
        }


class ConfigurationError(OpenFoundryError):
    code = "configuration_error"
    default_remediation = (
        {
            "action": "project.doctor",
            "command": "openfoundry doctor",
            "description": "Inspect project readiness and local remediation.",
        },
    )


class ValidationError(OpenFoundryError):
    code = "validation_error"


class NotFoundError(OpenFoundryError):
    code = "not_found"
    status_code = 404


class OperationCanceled(OpenFoundryError):
    code = "operation_canceled"


class ConflictError(OpenFoundryError):
    code = "conflict"
    status_code = 409
    default_retryable = True
    default_remediation = (
        {
            "action": "agent.context",
            "command": "openfoundry agent context",
            "description": "Refresh observed state before retrying with the current version.",
        },
    )


class AuthorizationError(OpenFoundryError):
    code = "forbidden"
    status_code = 403


class IntegrityError(OpenFoundryError):
    code = "integrity_error"
    status_code = 422


class CapabilityError(OpenFoundryError):
    code = "capability_mismatch"
    status_code = 422


class ExternalSystemError(OpenFoundryError):
    code = "external_system_error"
    status_code = 502
    default_retryable = True

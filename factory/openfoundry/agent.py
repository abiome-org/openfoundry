from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from openfoundry.actions import capability_catalog
from openfoundry.canonical import canonical_json, sha256_digest
from openfoundry.errors import CapabilityError, NotFoundError, ValidationError

if TYPE_CHECKING:
    from openfoundry.config import ProjectPaths
    from openfoundry.factory import Factory


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _instant(value: datetime | None = None) -> datetime:
    instant = value or datetime.now(UTC)
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValidationError("context time must include a timezone")
    return instant.astimezone(UTC)


def initial_context(
    paths: ProjectPaths,
    *,
    focus: str | None = None,
    limit: int = 20,
    since: str | None = None,
    max_bytes: int = 65_536,
) -> dict[str, Any]:
    from openfoundry.config import bootstrap, load_project
    from openfoundry.executors import default_executor_registry

    if since is not None:
        raise ValidationError("an event cursor cannot be used before factory bootstrap")
    FactoryView._check_limit(limit)
    if max_bytes < 16_384 or max_bytes > 1_048_576:
        raise ValidationError("max_bytes must be between 16384 and 1048576")
    project = load_project(paths)
    plan = bootstrap(paths, plan=True)
    catalog = capability_catalog()
    generated_at = _now()
    empty_page = {"items": [], "returned": 0, "total": 0, "truncated": False}
    context: dict[str, Any] = {
        "apiVersion": "openfoundry.agent/v1alpha1",
        "kind": "AgentContext",
        "trust": {
            "metadata": "untrusted-data",
        },
        "generatedAt": generated_at,
        "project": {
            "name": project["metadata"]["name"],
            "namespace": project["metadata"]["namespace"],
            "revision": sha256_digest(project),
            "profile": "local",
        },
        "readiness": {
            "ready": False,
            "failures": 1,
            "checks": [
                {"name": "project-schema", "status": "pass"},
                {
                    "name": "factory-state",
                    "status": "fail",
                    "detail": "not initialized",
                    "remediation": "inspect and apply the included bootstrap plan",
                },
            ],
        },
        "bootstrapPlan": plan,
        "inventory": {
            "resources": [],
            "executors": default_executor_registry().catalog()["providers"],
        },
        "activity": {
            "runs": dict(empty_page),
            "deployments": dict(empty_page),
            "operations": dict(empty_page),
        },
        "recentEvents": {**empty_page, "cursor": None, "since": None},
        "capabilities": {
            "catalogVersion": catalog["catalogVersion"],
            "catalogDigest": catalog["catalogDigest"],
            "command": "openfoundry agent capabilities",
            "endpoint": "/v1/agent/capabilities",
        },
        "limits": {"maxItemsPerSection": limit, "maxBytes": max_bytes, "focus": focus},
    }
    digest_input = {key: value for key, value in context.items() if key != "generatedAt"}
    result = {**context, "viewDigest": sha256_digest(digest_input)}
    if len(canonical_json(result)) > max_bytes:
        raise CapabilityError("initial agent context exceeds the requested byte budget")
    return result


class FactoryView:
    def __init__(self, factory: Factory) -> None:
        self.factory = factory

    def capabilities(self, action: str | None = None) -> dict[str, Any]:
        return capability_catalog(action)

    def context(
        self,
        *,
        focus: str | None = None,
        limit: int = 20,
        since: str | None = None,
        max_bytes: int = 65_536,
        at: datetime | None = None,
    ) -> dict[str, Any]:
        self._check_limit(limit)
        if max_bytes < 16_384 or max_bytes > 1_048_576:
            raise ValidationError("max_bytes must be between 16384 and 1048576")
        instant = _instant(at)
        generated_at = instant.isoformat().replace("+00:00", "Z")
        doctor = self.factory.doctor()
        readiness_checks = [
            {
                "name": item["name"],
                "status": item["status"],
                **({"detail": item["detail"]} if item["status"] == "fail" else {}),
                **({"remediation": item["remediation"]} if "remediation" in item else {}),
            }
            for item in doctor["checks"]
        ]
        readiness = {
            "ready": doctor["ready"],
            "failures": doctor["failures"],
            "checks": readiness_checks,
        }
        event_window = self.factory.events.window(limit=limit, after=since, focus=focus)
        events = self._page(
            [
                {
                    "id": event.id,
                    "type": event.type,
                    "subject": event.subject,
                    "time": event.time,
                    "actor": event.actor,
                    "runId": event.run_id,
                    "resourceUid": event.resource_uid,
                    "revision": event.revision,
                    "payloadDigest": event.payload_digest,
                }
                for event in event_window.items
            ],
            limit,
            total=event_window.total,
            already_limited=True,
            truncated=event_window.truncated,
        )
        events.update({"cursor": event_window.cursor, "since": since})
        recent_runs = self._recent_resource_status("Run", limit, focus)
        deployments = self._recent_resource_status("DeploymentSpec", limit, focus)
        operations = self.factory.operations.summaries(limit=limit, focus=focus)
        catalog = self.capabilities()
        context: dict[str, Any] = {
            "apiVersion": "openfoundry.agent/v1alpha1",
            "kind": "AgentContext",
            "trust": {
                "metadata": "untrusted-data",
            },
            "generatedAt": generated_at,
            "project": {
                "name": self.factory.project["metadata"]["name"],
                "namespace": self.factory.namespace,
                "revision": sha256_digest(self.factory.project),
                "profile": "local",
                "actor": self.factory.actor,
            },
            "readiness": readiness,
            "inventory": {
                "resources": self.factory.resources.inventory(),
                "executors": self.factory.executor_catalog()["providers"],
            },
            "activity": {
                "runs": recent_runs,
                "deployments": deployments,
                "operations": operations,
            },
            "recentEvents": events,
            "capabilities": {
                "catalogVersion": catalog["catalogVersion"],
                "catalogDigest": catalog["catalogDigest"],
                "command": "openfoundry agent capabilities",
                "endpoint": "/v1/agent/capabilities",
            },
            "limits": {
                "maxItemsPerSection": limit,
                "maxBytes": max_bytes,
                "focus": focus,
            },
        }
        return self._fit_and_digest(context, max_bytes)

    def _recent_resource_status(self, kind: str, limit: int, focus: str | None) -> dict[str, Any]:
        resources = self.factory.resources.latest(kind=kind)
        items: list[dict[str, Any]] = []
        for resource in resources:
            try:
                status, version = self.factory.resources.get_status(resource["metadata"]["uid"])
            except NotFoundError:
                status, version = {"state": "unknown", "reason": "status not initialized"}, 0
            summary = {
                "name": resource["metadata"]["name"],
                "uid": resource["metadata"]["uid"],
                "revision": resource["metadata"]["revision"],
                "createdAt": resource["metadata"]["createdAt"],
                "state": status.get("state", "unknown"),
                "reason": status.get("reason"),
                "statusVersion": version,
            }
            if kind == "Run":
                summary["runId"] = resource["spec"].get("runId")
            if not focus or self._matches(summary, focus):
                items.append(summary)
        return self._page(items, limit)

    @staticmethod
    def _page(
        items: list[dict[str, Any]],
        limit: int,
        *,
        total: int | None = None,
        already_limited: bool = False,
        truncated: bool = False,
    ) -> dict[str, Any]:
        selected = items if already_limited else items[:limit]
        count = len(items) if total is None else total
        return {
            "items": selected,
            "returned": len(selected),
            "total": count,
            "truncated": truncated or count > len(selected),
        }

    @staticmethod
    def _matches(value: Any, focus: str) -> bool:
        return focus.casefold() in canonical_json(value).decode().casefold()

    @staticmethod
    def _check_limit(limit: int) -> None:
        if limit < 1 or limit > 100:
            raise ValidationError("limit must be between 1 and 100")

    def _fit_and_digest(self, context: dict[str, Any], max_bytes: int) -> dict[str, Any]:
        while True:
            digest_input = {key: value for key, value in context.items() if key != "generatedAt"}
            candidate = {**context, "viewDigest": sha256_digest(digest_input)}
            if len(canonical_json(candidate)) <= max_bytes:
                return candidate
            if not self._trim_context(context):
                raise CapabilityError(
                    "agent context base exceeds the requested byte budget",
                    details={"maxBytes": max_bytes},
                    remediation=[
                        {
                            "action": "agent.context",
                            "command": "openfoundry agent context --max-bytes 65536",
                            "description": (
                                "Increase the context budget or select a narrower focus."
                            ),
                        }
                    ],
                )

    @staticmethod
    def _trim_context(context: dict[str, Any]) -> bool:
        pages = [
            context["recentEvents"],
            context["activity"]["operations"],
            context["activity"]["deployments"],
            context["activity"]["runs"],
        ]
        for page in pages:
            if page is context["recentEvents"] and len(page["items"]) == 1:
                continue
            if page["items"]:
                page["items"].pop()
                page["returned"] = len(page["items"])
                page["truncated"] = True
                if page is context["recentEvents"]:
                    items = page["items"]
                    if page["since"] is not None:
                        page["cursor"] = items[-1]["id"] if items else page["since"]
                    else:
                        page["cursor"] = items[0]["id"] if items else None
                return True
        if context["inventory"]["resources"]:
            context["inventory"]["resources"].pop()
            context["limits"]["inventoryTruncated"] = True
            return True
        if context["inventory"]["executors"]:
            context["inventory"]["executors"].pop()
            context["limits"]["executorInventoryTruncated"] = True
            return True
        return False

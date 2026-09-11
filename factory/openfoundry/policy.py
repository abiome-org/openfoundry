from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from openfoundry.canonical import load_document, sha256_digest
from openfoundry.errors import ValidationError

WORKTREE_MODES = ("deny", "allow", "archive")
_POLICY_SUFFIXES = (".yaml", ".yml", ".json")


@dataclass(frozen=True)
class PolicyRule:
    name: str
    effect: Literal["allow", "deny", "warn"]
    match: dict[str, Any] = field(default_factory=dict)
    reason: str = ""


@dataclass(frozen=True)
class PolicyDecision:
    outcome: Literal["allow", "deny", "warn"]
    policy_digest: str
    explanations: tuple[dict[str, str], ...]


class PolicyEngine:
    fields = frozenset({"actor", "action", "resource", "purpose"})

    def __init__(self, rules: list[PolicyRule]) -> None:
        for rule in rules:
            if unknown := rule.match.keys() - self.fields:
                raise ValidationError(f"unsupported policy match fields: {sorted(unknown)}")
        self.rules = tuple(rules)
        self.digest = sha256_digest(
            [
                {"name": r.name, "effect": r.effect, "match": r.match, "reason": r.reason}
                for r in self.rules
            ]
        )

    def evaluate(self, context: dict[str, Any]) -> PolicyDecision:
        matched: list[dict[str, str]] = []
        effects: list[str] = []
        for rule in self.rules:
            if all(
                self._matches(context.get(key), expected) for key, expected in rule.match.items()
            ):
                effects.append(rule.effect)
                matched.append({"rule": rule.name, "effect": rule.effect, "reason": rule.reason})
        outcome: Literal["allow", "deny", "warn"] = "deny"
        if "deny" in effects:
            outcome = "deny"
        elif "warn" in effects:
            outcome = "warn"
        elif "allow" in effects:
            outcome = "allow"
        if not matched:
            matched.append({"rule": "default-deny", "effect": "deny", "reason": "no rule matched"})
        return PolicyDecision(outcome, self.digest, tuple(matched))

    @staticmethod
    def _matches(actual: Any, expected: Any) -> bool:
        if expected == "*":
            return actual is not None
        if isinstance(expected, list):
            return actual in expected or (
                isinstance(actual, (list, set, tuple)) and bool(set(actual) & set(expected))
            )
        return bool(actual == expected)


def _dirty_worktree(value: Any) -> Any:
    if value not in WORKTREE_MODES:
        raise ValidationError(f"policy dirtyWorktree must be one of {', '.join(WORKTREE_MODES)}")
    return value


def _thresholds(value: Any) -> Any:
    if not isinstance(value, dict):
        raise ValidationError("policy promotion thresholds must map metric names to bounds")
    validated: dict[str, dict[str, float]] = {}
    for metric, bounds in value.items():
        if not isinstance(metric, str) or not metric:
            raise ValidationError("policy promotion threshold metric names must be non-empty")
        if not isinstance(bounds, dict) or set(bounds) - {"minimum", "maximum"}:
            raise ValidationError(
                f"policy promotion threshold {metric!r} accepts only minimum/maximum"
            )
        converted = {}
        for bound, number in bounds.items():
            if isinstance(number, bool) or not isinstance(number, (int, float)):
                raise ValidationError(
                    f"policy promotion threshold {metric!r}.{bound} must be a number"
                )
            converted[bound] = float(number)
        if (
            "minimum" in converted
            and "maximum" in converted
            and converted["minimum"] > converted["maximum"]
        ):
            raise ValidationError(f"policy promotion threshold {metric!r} minimum exceeds maximum")
        validated[metric] = converted
    return validated


def _promotion(value: Any) -> Any:
    allowed = {
        "requireEvaluationPass",
        "requireCompatibilityPass",
        "requireVulnerabilityScan",
        "thresholds",
    }
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValidationError(f"policy promotion accepts only {', '.join(sorted(allowed))}")
    for key, item in value.items():
        if key == "thresholds":
            value = {**value, "thresholds": _thresholds(item)}
        elif not isinstance(item, bool):
            raise ValidationError("policy promotion requirements must be booleans")
    return dict(value)


_CONFIG_KEYS: dict[str, Callable[[Any], Any]] = {
    "dirtyWorktree": _dirty_worktree,
    "promotion": _promotion,
}


def _validate_policy_config(config: dict[str, Any]) -> dict[str, Any]:
    validated: dict[str, Any] = {}
    for key, value in config.items():
        if key not in _CONFIG_KEYS:
            raise ValidationError(f"policy config key is not enforced by this factory: {key}")
        validated[key] = _CONFIG_KEYS[key](value)
    return validated


def _policy_rules(document: dict[str, Any], name: str) -> list[PolicyRule]:
    rules = []
    for index, rule in enumerate(document["spec"]["rules"]):
        if (
            not isinstance(rule, dict)
            or not isinstance(rule.get("name"), str)
            or rule.get("effect") not in {"allow", "deny", "warn"}
            or not isinstance(rule.get("match", {}), dict)
        ):
            raise ValidationError(f"policy rule {index} is invalid in {name}")
        rules.append(
            PolicyRule(
                str(rule["name"]),
                rule["effect"],
                dict(rule.get("match", {})),
                str(rule.get("reason", "")),
            )
        )
    return rules


def _policy_documents(location: Path, namespace: str | None) -> list[tuple[str, dict[str, Any]]]:
    from openfoundry.schema_registry import default_registry

    documents = []
    paths = (
        sorted(item for item in location.iterdir() if item.is_file()) if location.is_dir() else []
    )
    for path in paths:
        if path.suffix not in _POLICY_SUFFIXES:
            continue
        value = load_document(path.read_bytes())
        if not isinstance(value, dict):
            raise ValidationError(f"policy document must be one object: {path.name}")
        document = default_registry.validate_as(value, "Policy")
        if document["metadata"].get("namespace", namespace) != namespace:
            raise ValidationError(f"policy namespace does not match the project: {path.name}")
        documents.append((path.name, document))
    return documents


@dataclass(frozen=True)
class ProjectPolicy:
    engine: PolicyEngine
    config: dict[str, Any]
    documents: tuple[dict[str, Any], ...]
    digest: str
    directory: str

    @property
    def enforced(self) -> bool:
        return bool(self.documents)

    @property
    def dirty_worktree(self) -> str:
        return str(self.config.get("dirtyWorktree", "allow"))

    def authorize(self, context: dict[str, Any]) -> PolicyDecision:
        if not self.documents:
            return PolicyDecision(
                "allow",
                self.digest,
                ({"rule": "no-policy-documents", "effect": "allow", "reason": ""},),
            )
        return self.engine.evaluate(context)

    @classmethod
    def load(cls, root: str | Path, project: dict[str, Any]) -> ProjectPolicy:
        extensions = project.get("spec", {}).get("extensions", {})
        directory = str(extensions.get("policyDirectory", "policies"))
        if not directory or Path(directory).is_absolute() or ".." in Path(directory).parts:
            raise ValidationError("policyDirectory must be a relative path inside the project")
        namespace = project.get("metadata", {}).get("namespace")
        rules: list[PolicyRule] = []
        config: dict[str, Any] = {}
        documents: list[dict[str, Any]] = []
        for name, document in _policy_documents(Path(root) / directory, namespace):
            rules.extend(_policy_rules(document, name))
            for key, item in _validate_policy_config(
                dict(document["spec"].get("config", {}))
            ).items():
                if key in config and config[key] != item:
                    raise ValidationError(f"policy documents disagree on config key: {key}")
                config[key] = item
            documents.append({"path": name, "document": document})
        digest = sha256_digest({"directory": directory, "documents": documents})
        return cls(PolicyEngine(rules), config, tuple(documents), digest, directory)


def promotion_gate(
    evidence: dict[str, Any], requirements: dict[str, Any] | None = None
) -> PolicyDecision:
    requirements = requirements or {}
    checks = {
        "lineage": bool(evidence.get("lineage_complete")),
        "rights": bool(evidence.get("rights_valid")),
    }
    optional = {
        "requireEvaluationPass": ("evaluation", "evaluation_passed", True),
        "requireCompatibilityPass": ("compatibility", "compatibility_passed", False),
        "requireVulnerabilityScan": ("vulnerabilities", "vulnerabilities_valid", False),
    }
    for option, (name, evidence_key, default) in optional.items():
        if requirements.get(option, default) or (
            name == "vulnerabilities" and evidence.get("vulnerabilities_present")
        ):
            checks[name] = bool(evidence.get(evidence_key))
    thresholds = requirements.get("thresholds") or {}
    scores = evidence.get("metric_scores") or {}
    for metric, bounds in thresholds.items():
        value = scores.get(metric)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            checks[f"threshold:{metric}"] = False
            continue
        number = float(value)
        checks[f"threshold:{metric}"] = (bounds.get("minimum", number) <= number) and (
            number <= bounds.get("maximum", number)
        )
    explanations = tuple(
        {"rule": name, "effect": "allow" if passed else "deny", "reason": "release evidence"}
        for name, passed in checks.items()
    )
    return PolicyDecision(
        "allow" if all(checks.values()) else "deny",
        sha256_digest({"gate": "promotion-v2", "requirements": requirements}),
        explanations,
    )

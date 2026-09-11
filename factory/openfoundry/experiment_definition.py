from __future__ import annotations

import hashlib
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from pydantic import ValidationError as ModelError

from openfoundry.canonical import load_document, portable_relative_path, sha256_digest
from openfoundry.config import ProjectPaths, bootstrap
from openfoundry.errors import ValidationError
from openfoundry.modules import extract_module_package, package_module

Name = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")]


class DefinitionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, populate_by_name=True)


class Script(DefinitionModel):
    source: str = "src"
    command: list[str] = Field(min_length=1)
    dependencies: str | None = None
    inputs: list[Name] | None = None
    artifacts: dict[Name, str] = Field(default_factory=dict)
    metrics: str | None = None
    examples: str | None = None
    # Optional relative path the script may write a resumable/branchable
    # checkpoint to (file or directory, inside the stage output dir).
    # When present at stage end it is imported as the stage checkpoint.
    checkpoint: str | None = None

    @model_validator(mode="after")
    def outputs(self) -> Script:
        if set(self.artifacts) & {"measurement", "examples", "checkpoint"}:
            raise ValueError("measurement, examples and checkpoint are reserved artifact names")
        for path in [*self.artifacts.values(), self.metrics, self.examples, self.checkpoint]:
            if path is not None:
                portable_relative_path(path, "script output")
        return self

    def validate_arguments(self, available: set[str], candidates: dict[str, Candidate]) -> None:
        if self.inputs is not None and not set(self.inputs) <= available:
            raise ValueError("script.inputs contains an unknown input")
        selected = available if self.inputs is None else set(self.inputs)
        for candidate in candidates.values():
            try:
                for argument in self.command:
                    argument.format_map(
                        {
                            "inputs": dict.fromkeys(selected, "input"),
                            "output": "output",
                            "parameters": candidate.parameters,
                        }
                    )
            except (KeyError, ValueError, IndexError, TypeError) as exc:
                raise ValueError(f"invalid command substitution: {exc}") from exc


class Dataset(DefinitionModel):
    source: str
    rights: dict[str, Any]


class Metric(DefinitionModel):
    direction: Literal["maximize", "minimize"] = "maximize"
    minimum: float | None = None
    maximum: float | None = None
    maxRegression: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def bounds(self) -> Metric:
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("metric minimum exceeds maximum")
        return self


class Candidate(DefinitionModel):
    rationale: str = ""
    parameters: dict[str, Any] = Field(default_factory=dict)
    # Branch/resume source for this candidate. Accepted forms:
    # run/<id>, checkpoint/<name>, release/<name>, alias/<name>,
    # artifact:sha256:<digest>, sha256:<digest>. A train script that declares
    # the reserved base input receives "" for candidates without from.
    from_ref: str | None = Field(default=None, alias="from")


class SearchBudget(DefinitionModel):
    maxRuns: int | None = Field(default=None, gt=0)
    maxCostUSD: float | None = Field(default=None, gt=0)


class Search(DefinitionModel):
    # Candidate whose parameters seed every generated trial.
    template: Name = "baseline"
    # Cartesian grid over parameters. Empty grid with count set means
    # repeats of the template (useful for variance estimates).
    grid: dict[str, list[Any]] = Field(default_factory=dict)
    count: int | None = Field(default=None, gt=0)
    # Declared execution budget. Local runs trials sequentially in a
    # deterministic order regardless of concurrency; the value is recorded
    # for schedulers that honor it.
    concurrency: int = Field(default=1, ge=1)
    budget: SearchBudget = Field(default_factory=SearchBudget)

    @model_validator(mode="after")
    def bounds(self) -> Search:
        for key, values in self.grid.items():
            if not values:
                raise ValueError(f"search grid {key!r} must list at least one value")
        if not self.grid and self.count is None:
            raise ValueError("search requires a grid or a count")
        if (
            self.count is not None
            and self.budget.maxRuns is not None
            and self.count > self.budget.maxRuns
        ):
            raise ValueError("search count exceeds budget maxRuns")
        return self


def expand_search(search: Search, template: dict[str, Any]) -> list[dict[str, Any]]:
    """Deterministic trial parameters: cartesian grid, cycled to count, capped by maxRuns."""
    from itertools import product

    keys = sorted(search.grid)
    combos = [
        dict(zip(keys, values, strict=True)) for values in product(*(search.grid[k] for k in keys))
    ] or [{}]
    if search.count is not None:
        combos = [combos[index % len(combos)] for index in range(search.count)]
    if search.budget.maxRuns is not None:
        combos = combos[: search.budget.maxRuns]
    return [{**template, **combo} for combo in combos]


class Accelerator(DefinitionModel):
    type: str | None = None
    count: float = Field(default=0, ge=0)


class Limits(DefinitionModel):
    timeoutSeconds: float = Field(default=3600, gt=0)
    cpuSeconds: float | None = Field(default=None, gt=0)
    addressSpaceBytes: int | None = Field(default=None, gt=0)
    processes: int | None = Field(default=None, gt=0)
    fileSizeBytes: int | None = Field(default=None, gt=0)
    memoryBytes: int | None = Field(default=None, gt=0)
    costLimitUSD: float | None = Field(default=None, gt=0)
    preemptible: bool = False
    accelerators: Accelerator | None = None


class ExperimentDefinition(DefinitionModel):
    name: Name
    objective: str = Field(min_length=1)
    modelCard: str = "MODEL_CARD.md"
    data: dict[Name, Dataset] = Field(default_factory=dict)
    train: Script
    evaluate: Script
    metrics: dict[Name, Metric] = Field(min_length=1)
    primaryMetric: str
    baseline: Name = "baseline"
    candidates: dict[Name, Candidate]
    search: Search | None = None
    limits: Limits = Field(default_factory=Limits)
    executor: str = "local"
    provider: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def references(self) -> ExperimentDefinition:
        if self.baseline not in self.candidates:
            raise ValueError("baseline must name a candidate")
        if self.primaryMetric not in self.metrics:
            raise ValueError("primaryMetric must name a metric")
        if self.evaluate.metrics is None:
            raise ValueError("evaluate.metrics must name the script's JSON metrics file")
        if set(self.metrics) & {"passed", "compatibilityPassed"}:
            raise ValueError("passed and compatibilityPassed are reserved metric names")
        if set(self.data) & set(self.train.artifacts):
            raise ValueError("dataset and training artifact names must be distinct")
        if "base" in set(self.data) | set(self.train.artifacts):
            raise ValueError("base is reserved for candidate branching")
        for name, script in (("train", self.train), ("evaluate", self.evaluate)):
            available = set(self.data) | (
                set(self.train.artifacts) if name == "evaluate" else {"base"}
            )
            script.validate_arguments(available, self.candidates)
        for name, candidate in self.candidates.items():
            if candidate.from_ref is not None and not _is_branch_ref(candidate.from_ref):
                raise ValueError(f"candidate {name!r} has an invalid from reference")
        _check_search(self.search, self.candidates)
        return self


_BRANCH_PREFIXES = ("run/", "checkpoint/", "release/", "alias/", "artifact:sha256:", "sha256:")


def _is_branch_ref(value: str) -> bool:
    if not value or not isinstance(value, str):
        return False
    if value.startswith(_BRANCH_PREFIXES):
        return bool(value not in ("run/", "checkpoint/", "release/", "alias/"))
    return False


def _check_search(search: Search | None, candidates: dict[str, Candidate]) -> None:
    if search is not None and search.template not in candidates:
        raise ValueError("search template must name a candidate")


def project_path(root: Path, base: Path, value: str) -> Path:
    portable_relative_path(value, "experiment path")
    path = (base / value).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValidationError("experiment path escapes the project")
    return path


def read_definition(path: Path) -> ExperimentDefinition:
    return _validated_definition(load_document(path.read_bytes()))


def _validated_definition(value: Any) -> ExperimentDefinition:
    try:
        return ExperimentDefinition.model_validate(value)
    except ModelError as exc:
        raise ValidationError(
            "invalid experiment definition",
            details={"errors": exc.errors(include_input=False, include_context=False)},
            remediation=[
                {
                    "command": "openfoundry experiment schema",
                    "description": (
                        "Inspect supported fields. Memory limits use limits.addressSpaceBytes "
                        "(virtual address space in bytes)."
                    ),
                }
            ],
        ) from exc


def resource(kind: str, name: str, spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "apiVersion": "openfoundry.dev/v1alpha1",
        "kind": kind,
        "metadata": {"name": name},
        "spec": spec,
    }


def write_yaml(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(value, sort_keys=False))


def capture_script(root: Path, base: Path, script: Script, target: Path) -> dict[str, str]:
    source = project_path(root, base, script.source)
    if not source.is_dir():
        raise ValidationError(f"script source is not a directory: {script.source}")
    tracked = (
        subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", "."],
            cwd=source,
            check=True,
            capture_output=True,
        )
        .stdout.decode()
        .split("\0")
    )
    with tempfile.NamedTemporaryFile(suffix=".tar") as package:
        package_module(source, package.name, included_paths=set(tracked))
        extract_module_package(package.name, target)
    if any(
        (target / name).exists()
        for name in (
            "openfoundry-script.yaml",
            "openfoundry-script-runner.py",
            "openfoundry-requirements.lock",
        )
    ):
        raise ValidationError("script source contains reserved OpenFoundry adapter filenames")
    files = {
        path.relative_to(target).as_posix(): "sha256:"
        + hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(target.rglob("*"))
        if path.is_file()
    }
    lock = (
        project_path(target, target, script.dependencies).read_bytes()
        if script.dependencies
        else b""
    )
    (target / "openfoundry-requirements.lock").write_bytes(lock)
    (target / "openfoundry-script-runner.py").write_bytes(
        Path(__file__).with_name("script_runner.py").read_bytes()
    )
    write_yaml(
        target / "openfoundry-script.yaml",
        resource(
            "Module",
            target.name,
            {
                "entryPoint": {"command": ["python3", "openfoundry-script-runner.py"]},
                "environment": {
                    "dependencyLock": "openfoundry-requirements.lock",
                    "dependencyDigest": "sha256:" + hashlib.sha256(lock).hexdigest(),
                },
                **({"checkpoint": True} if script.checkpoint else {}),
            },
        ),
    )
    return files


def stage(
    name: str, script: Script, module: str, inputs: dict[str, str], parameters: dict[str, Any]
) -> dict[str, Any]:
    outputs = [*script.artifacts]
    if script.examples:
        outputs.append("examples")
    return {
        "name": name,
        "module": module,
        "dataUse": "evaluation" if name == "evaluate" else "training",
        "needs": ["train"] if name == "evaluate" else [],
        "inputs": inputs if script.inputs is None else {key: inputs[key] for key in script.inputs},
        "outputs": outputs,
        "config": {
            **script.model_dump(exclude={"source", "dependencies", "inputs"}),
            "parameters": parameters,
        },
    }


def evaluation_spec(definition: ExperimentDefinition) -> dict[str, Any]:
    metrics = [
        {
            "name": name,
            "output": f"evaluate.{name}",
            **metric.model_dump(include={"minimum", "maximum"}, exclude_none=True),
        }
        for name, metric in definition.metrics.items()
    ]
    spec = {"metrics": metrics, "extensions": {"command": definition.evaluate.command}}
    return resource("EvaluationSpec", f"{definition.name}-{sha256_digest(spec)[7:19]}", spec)


def initialize(
    path: Path, *, name: str, objective: str, source: str, actor: str | None = None
) -> dict[str, Any]:
    path = path.resolve()
    if path.exists():
        raise ValidationError("experiment definition already exists")
    definition = _validated_definition(
        {
            "name": name,
            "objective": objective,
            "primaryMetric": "accuracy",
            "train": Script(
                source=source,
                command=["python3", "train.py", "--output", "{output}/model"],
                artifacts={"model": "model"},
            ),
            "evaluate": Script(
                source=source,
                command=[
                    "python3",
                    "evaluate.py",
                    "--model",
                    "{inputs[model]}",
                    "--output",
                    "{output}/metrics.json",
                ],
                metrics="metrics.json",
            ),
            "metrics": {"accuracy": Metric(minimum=0.8)},
            "candidates": {
                "baseline": Candidate(rationale="Establish the current model's performance.")
            },
        }
    )
    root = path.parent
    root.mkdir(parents=True, exist_ok=True)
    if not (root / "openfoundry.yaml").exists():
        actor = actor or "local-user"
        slug = re.sub(r"[^A-Za-z0-9_-]", "-", name)
        project = resource("Project", slug, {"owners": [actor]})
        project["metadata"]["namespace"] = f"local/{slug}"
        write_yaml(root / "openfoundry.yaml", project)
        write_yaml(
            root / "policies/local.yaml",
            resource(
                "Policy",
                "local",
                {
                    "rules": [
                        {"name": "project-owner", "effect": "allow", "match": {"actor": actor}}
                    ],
                    "config": {"dirtyWorktree": "archive"},
                },
            ),
        )
    ignored = root / ".gitignore"
    existing = ignored.read_text() if ignored.exists() else ""
    additions = [
        value for value in (".openfoundry/", ".venv/") if value not in existing.splitlines()
    ]
    if additions:
        ignored.write_text(existing.rstrip() + "\n" + "\n".join(additions) + "\n")
    if not (root / ".git").exists():
        subprocess.run(["git", "init", "-q", str(root)], check=True)
    write_yaml(path, definition.model_dump(mode="json", exclude_none=True))
    card = root / "MODEL_CARD.md"
    if not card.exists():
        card.write_text(f"# {name}\n\n{objective}\n\nExperiment: `{path.name}`\n")
    bootstrap(ProjectPaths(root))
    return {
        "definition": str(path),
        "project": str(root),
        "next": f"openfoundry experiment run {path.name} --candidate baseline",
    }

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openfoundry.canonical import load_document
from openfoundry.database import Database
from openfoundry.errors import ConfigurationError, NotFoundError, ValidationError
from openfoundry.schema_registry import default_registry
from openfoundry.security import ApiTokenStore, SecretStore, SigningIdentity
from openfoundry.stores.filesystem import FilesystemStore


@dataclass(frozen=True)
class ProjectPaths:
    root: Path

    @property
    def config(self) -> Path:
        return self.root / "openfoundry.yaml"

    @property
    def state(self) -> Path:
        return self.root / ".openfoundry"

    @property
    def database(self) -> Path:
        return self.state / "metadata.db"

    @property
    def signing_key(self) -> Path:
        return self.state / "identity" / "signing.key"

    @property
    def secret_key(self) -> Path:
        return self.state / "identity" / "secrets.key"

    @property
    def store(self) -> Path:
        return self.state / "store"

    @property
    def runs(self) -> Path:
        return self.state / "runs"

    @property
    def packages(self) -> Path:
        return self.state / "packages"

    @property
    def environments(self) -> Path:
        return self.state / "environments"


def discover_project(start: str | Path | None = None) -> ProjectPaths:
    current = Path(start or Path.cwd()).resolve()
    if current.is_file():
        current = current.parent
    for candidate in (current,) if start is not None else (current, *current.parents):
        if (candidate / "openfoundry.yaml").is_file():
            return ProjectPaths(candidate)
    raise ConfigurationError(
        f"no openfoundry.yaml in project directory {current}"
        if start is not None
        else "no openfoundry.yaml found in this directory or its parents"
    )


def local_actor(project: dict[str, Any]) -> str:
    owners = project["spec"].get("owners", [])
    return str(owners[0]) if owners else "local-user"


def load_project(paths: ProjectPaths) -> dict[str, Any]:
    value = load_document(paths.config.read_bytes())
    if not isinstance(value, dict):
        raise ValidationError("openfoundry.yaml must contain one resource object")
    return default_registry.validate(value)


def bootstrap(
    paths: ProjectPaths,
    *,
    profile: str = "local",
    plan: bool = False,
) -> dict[str, Any]:
    if profile != "local":
        raise ConfigurationError(
            "the built-in bootstrap profile is local; site profiles use bindings"
        )
    project = load_project(paths)
    directories = [
        paths.state,
        paths.state / "identity",
        paths.store,
        paths.runs,
        paths.packages,
        paths.environments,
        paths.state / "operations",
    ]
    actions = [
        {"action": "create-directory", "path": str(path.relative_to(paths.root))}
        for path in directories
        if not path.exists()
    ]
    if not paths.database.exists():
        actions.append({"action": "initialize-database", "path": ".openfoundry/metadata.db"})
    if not paths.signing_key.exists():
        actions.append({"action": "generate-signing-identity", "path": ".openfoundry/identity"})
    if plan:
        return {"profile": profile, "project": project["metadata"]["name"], "actions": actions}

    old_umask = os.umask(0o077)
    try:
        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)
        database = Database(paths.database)
        identity = SigningIdentity(paths.signing_key)
        secrets_store = SecretStore(database, paths.secret_key)
        FilesystemStore(paths.store)
        try:
            local_token = secrets_store.get("local-api-token", "api-authentication").decode()
        except NotFoundError:
            local_token = secrets.token_urlsafe(32)
            secrets_store.put("local-api-token", local_token, "api-authentication")
        ApiTokenStore(database).register(
            local_token,
            actor=local_actor(project),
            scopes={"*"},
        )
        database.close()
    finally:
        os.umask(old_umask)
    return {
        "profile": profile,
        "project": project["metadata"]["name"],
        "state": str(paths.state),
        "keyId": identity.key_id,
        "actions": actions,
        "ready": True,
    }

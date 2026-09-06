from __future__ import annotations

import argparse
from pathlib import Path

from openfoundry.config import ProjectPaths
from openfoundry.factory import Factory


def execute(project: str | Path, operation_id: str) -> None:
    paths = ProjectPaths(Path(project))
    with Factory(paths) as reader:
        actor = str(reader.operations.get(operation_id)["request"]["actor"])
    with Factory(paths, actor=actor) as factory:
        factory.execute_run_operation(operation_id)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--operation", required=True)
    args = parser.parse_args()
    execute(args.project, args.operation)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

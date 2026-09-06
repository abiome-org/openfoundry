from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def select_python(requested: str | None) -> Path:
    candidates = [requested] if requested else [sys.executable, "python3.12", "python3.11"]
    for candidate in candidates:
        executable = shutil.which(str(candidate))
        if executable is None:
            continue
        path = Path(executable).resolve()
        result = subprocess.run(
            [str(path), "-c", "import json, sys; print(json.dumps(list(sys.version_info[:2])))"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0 and json.loads(result.stdout) in ([3, 11], [3, 12]):
            return path
    raise SystemExit("Install Python 3.11 or 3.12, then rerun make setup.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create OpenFoundry's locked development environment."
    )
    parser.add_argument("--python", help="Python 3.11 or 3.12 executable")
    args = parser.parse_args()
    environment = ROOT / ".venv"
    python = environment / "bin/python"
    if not environment.exists():
        subprocess.run(
            [str(select_python(args.python)), "-m", "venv", str(environment)], check=True
        )
    elif not python.exists():
        raise SystemExit(
            ".venv exists without a Python interpreter; move it aside and rerun setup."
        )
    result = subprocess.run(
        [str(python), "-c", "import sys; sys.exit(sys.version_info[:2] not in ((3, 11), (3, 12)))"],
        check=False,
    )
    if result.returncode:
        raise SystemExit(
            ".venv needs a working Python 3.11 or 3.12; move it aside and rerun setup."
        )
    pip = [str(python), "-m", "pip", "--disable-pip-version-check"]
    locked = [
        "--only-binary=:all:",
        "--require-hashes",
        "-r",
        "requirements.lock",
        "-r",
        "requirements.build.lock",
    ]
    wheels = environment / "wheels"
    subprocess.run([*pip, "download", "--dest", str(wheels), *locked], cwd=ROOT, check=True)
    subprocess.run(
        [*pip, "install", "--no-index", "--find-links", str(wheels), *locked],
        cwd=ROOT,
        check=True,
    )
    subprocess.run(
        [*pip, "install", "--no-build-isolation", "--no-deps", "-e", "."], cwd=ROOT, check=True
    )
    print("Ready. Run make check or make test; activation is optional.")


if __name__ == "__main__":
    main()

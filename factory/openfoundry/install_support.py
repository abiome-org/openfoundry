from __future__ import annotations

import argparse
import os
import secrets
import shutil
import stat
import sys
from contextlib import suppress
from pathlib import Path

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
STARTER = (
    "data/fixtures/affine.jsonl",
    "data/fixtures/rights.yaml",
    "evaluations/example-affine.yaml",
    "model-packages/example-affine.yaml",
    "modules/examples/affine-regression",
    "modules/examples/affine-serving",
    "workloads/example-from-scratch.yaml",
)


def copy_starter(source_root: Path, target: Path) -> list[str]:
    copied = []
    for relative in STARTER:
        source, destination = source_root / relative, target / relative
        if os.path.lexists(destination):
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, destination, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy2(source, destination)
        copied.append(relative)
    return copied


def _open_parent(path: Path) -> int:
    descriptor_root = path.parent.parent
    if descriptor_root in {Path("/proc/self/fd"), Path("/dev/fd")} and path.parent.name.isdecimal():
        descriptor = int(path.parent.name)
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise ValueError("inherited installer target is not a directory")
        return os.dup(descriptor)
    return os.open(path.parent, os.O_RDONLY | _DIRECTORY | _NOFOLLOW)


def _read_regular(directory: int, name: str) -> tuple[str, int, tuple[int, int]] | None:
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_NONBLOCK | _NOFOLLOW,
            dir_fd=directory,
        )
    except FileNotFoundError:
        return None
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        os.close(descriptor)
        raise ValueError("managed destination must be a regular non-symbolic-link file")
    with os.fdopen(descriptor, encoding="utf-8") as source:
        content = source.read()
    return content, stat.S_IMODE(metadata.st_mode), (metadata.st_dev, metadata.st_ino)


def _validate_markers(content: str, begin: str, end: str) -> None:
    lines = content.splitlines()
    begin_lines = [index for index, line in enumerate(lines) if line == begin]
    end_lines = [index for index, line in enumerate(lines) if line == end]
    if content.count(begin) != len(begin_lines) or content.count(end) != len(end_lines):
        raise ValueError("managed markers must appear on standalone lines")
    if not begin_lines and not end_lines:
        return
    if len(begin_lines) != 1 or len(end_lines) != 1 or begin_lines[0] >= end_lines[0]:
        raise ValueError("expected exactly one ordered begin/end marker pair")


def validate_managed_file(destination: Path, begin: str, end: str) -> None:
    if not os.path.lexists(destination):
        return
    directory = _open_parent(destination)
    try:
        current = _read_regular(directory, destination.name)
    finally:
        os.close(directory)
    if current is None:
        return
    _validate_markers(current[0], begin, end)


def _temporary_name(destination_name: str) -> str:
    return f".{destination_name}.openfoundry-{secrets.token_hex(8)}"


def upsert_managed_section(source: Path, destination: Path, begin: str, end: str) -> bool:
    section = source.read_text(encoding="utf-8").rstrip("\n")
    section_lines = section.splitlines()
    if not section_lines or section_lines[0] != begin or section_lines[-1] != end:
        raise ValueError("managed template markers do not match the requested section")

    directory = _open_parent(destination)
    temporary_name: str | None = None
    try:
        current = _read_regular(directory, destination.name)
        original, mode, identity = current or ("", 0o644, None)
        _validate_markers(original, begin, end)

        if begin in original:
            start = original.index(begin)
            finish = original.index(end, start) + len(end)
            updated = original[:start] + section + original[finish:]
        else:
            separator = "" if not original else ("\n" if original.endswith("\n") else "\n\n")
            updated = original + separator + section + "\n"
        if updated == original:
            return False

        while temporary_name is None:
            candidate = _temporary_name(destination.name)
            try:
                descriptor = os.open(
                    candidate,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
                    mode,
                    dir_fd=directory,
                )
            except FileExistsError:
                continue
            temporary_name = candidate

        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            os.fchmod(output.fileno(), mode)
            output.write(updated)
            output.flush()
            os.fsync(output.fileno())

        observed = _read_regular(directory, destination.name)
        observed_identity = None if observed is None else observed[2]
        if observed_identity != identity:
            raise RuntimeError("managed destination changed during installation")
        os.replace(
            temporary_name,
            destination.name,
            src_dir_fd=directory,
            dst_dir_fd=directory,
        )
        temporary_name = None
        os.fsync(directory)
        return True
    finally:
        if temporary_name is not None:
            with suppress(FileNotFoundError):
                os.unlink(temporary_name, dir_fd=directory)
        os.close(directory)


def render_template(source: Path, destination: Path, name: str, namespace: str) -> bool:
    content = source.read_text(encoding="utf-8")
    content = content.replace("__OPENFOUNDRY_PROJECT_NAME__", name)
    content = content.replace("__OPENFOUNDRY_PROJECT_NAMESPACE__", namespace)
    directory = _open_parent(destination)
    temporary_name: str | None = None
    try:
        current = _read_regular(directory, destination.name)
        if current is not None:
            return False
        while temporary_name is None:
            candidate = _temporary_name(destination.name)
            try:
                descriptor = os.open(
                    candidate,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
                    0o644,
                    dir_fd=directory,
                )
            except FileExistsError:
                continue
            temporary_name = candidate
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.link(
            temporary_name,
            destination.name,
            src_dir_fd=directory,
            dst_dir_fd=directory,
            follow_symlinks=False,
        )
        os.unlink(temporary_name, dir_fd=directory)
        temporary_name = None
        os.fsync(directory)
        return True
    finally:
        if temporary_name is not None:
            with suppress(FileNotFoundError):
                os.unlink(temporary_name, dir_fd=directory)
        os.close(directory)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    validate = commands.add_parser("validate")
    validate.add_argument("destination", type=Path)
    validate.add_argument("begin")
    validate.add_argument("end")

    upsert = commands.add_parser("upsert")
    upsert.add_argument("source", type=Path)
    upsert.add_argument("destination", type=Path)
    upsert.add_argument("begin")
    upsert.add_argument("end")

    render = commands.add_parser("render")
    render.add_argument("source", type=Path)
    render.add_argument("destination", type=Path)
    render.add_argument("name")
    render.add_argument("namespace")

    starter = commands.add_parser("starter")
    starter.add_argument("source", type=Path)
    starter.add_argument("target", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "validate":
            validate_managed_file(arguments.destination, arguments.begin, arguments.end)
        elif arguments.command == "upsert":
            upsert_managed_section(
                arguments.source,
                arguments.destination,
                arguments.begin,
                arguments.end,
            )
        elif arguments.command == "render":
            render_template(
                arguments.source,
                arguments.destination,
                arguments.name,
                arguments.namespace,
            )
        else:
            copy_starter(arguments.source, arguments.target)
    except (OSError, RuntimeError, UnicodeError, ValueError) as error:
        print(f"install support: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

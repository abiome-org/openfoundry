from __future__ import annotations

import base64
import hashlib
import zipfile
from pathlib import Path


def build_wheel(
    directory: Path,
    *,
    name: str = "openfoundrytiny",
    version: str = "1.0",
    source: str = 'VERSION = "1.0"\n',
) -> tuple[Path, str]:
    dist_info = f"{name}-{version}.dist-info"
    files: dict[str, bytes] = {
        f"{name}/__init__.py": source.encode(),
        f"{dist_info}/METADATA": (
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n".encode()
        ),
        f"{dist_info}/WHEEL": (
            b"Wheel-Version: 1.0\nGenerator: openfoundry-tests\nRoot-Is-Purelib: "
            b"true\nTag: py3-none-any\n"
        ),
        f"{dist_info}/top_level.txt": f"{name}\n".encode(),
    }
    record = []
    for path, content in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=")
        record.append(f"{path},sha256={digest.decode()},{len(content)}")
    record.append(f"{dist_info}/RECORD,,")
    files[f"{dist_info}/RECORD"] = ("\n".join(record) + "\n").encode()
    directory.mkdir(parents=True, exist_ok=True)
    wheel = directory / f"{name}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, content in files.items():
            archive.writestr(zipfile.ZipInfo(path, date_time=(2020, 1, 1, 0, 0, 0)), content)
    return wheel, hashlib.sha256(wheel.read_bytes()).hexdigest()


def lock_for(name: str, version: str, wheel_digest: str) -> bytes:
    return f"{name}=={version} --hash=sha256:{wheel_digest}\n".encode()

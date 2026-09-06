"""Create a runnable project with a verified public dataset and a stable split."""

import argparse
import hashlib
import io
import json
import shutil
import urllib.request
import zipfile
from pathlib import Path

from openfoundry.experiment_definition import initialize

URL = "https://archive.ics.uci.edu/static/public/228/sms%2Bspam%2Bcollection.zip"
SHA256 = "1587ea43e58e82b14ff1f5425c88e17f8496bfcdb67a583dbff9eefaf9963ce3"


def split(content):
    unique, conflicts = {}, set()
    lines = content.decode("utf-8").splitlines()
    for line in lines:
        label, text = line.split("\t", 1)
        normalized = " ".join(text.casefold().split())
        identity = hashlib.sha256(normalized.encode()).hexdigest()
        if label not in {"ham", "spam"}:
            raise ValueError("unexpected dataset label")
        if identity in unique and unique[identity]["label"] != label:
            conflicts.add(identity)
        unique[identity] = {"id": identity, "text": text, "label": label}
    rows = [row for identity, row in sorted(unique.items()) if identity not in conflicts]
    training = [row for row in rows if int(row["id"][:8], 16) % 5 != 0]
    development = [row for row in rows if int(row["id"][:8], 16) % 5 == 0]
    return (
        training,
        development,
        {"rawRows": len(lines), "uniqueRows": len(rows), "conflictingMessages": len(conflicts)},
    )


def prepare(destination, archive=None):
    destination = Path(destination).resolve()
    if destination.exists():
        raise ValueError("choose a new example project directory")
    if archive:
        payload = Path(archive).read_bytes()
    else:
        with urllib.request.urlopen(URL, timeout=30) as response:
            payload = response.read()
    if hashlib.sha256(payload).hexdigest() != SHA256:
        raise ValueError("UCI archive differs from the recorded dataset checksum")
    with zipfile.ZipFile(io.BytesIO(payload)) as compressed:
        training, development, counts = split(compressed.read("SMSSpamCollection"))
    initialize(
        destination / "experiment.yaml",
        name="sms-spam",
        objective="Detect SMS spam while preserving legitimate messages.",
        source="src",
        actor="local-user",
    )
    source = Path(__file__).resolve().parent
    shutil.copytree(
        source / "src", destination / "src", ignore=shutil.ignore_patterns("__pycache__")
    )
    for name in ("experiment.yaml", "MODEL_CARD.md"):
        shutil.copyfile(source / name, destination / name)
    (destination / "data").mkdir()
    for name, rows in (("train", training), ("dev", development)):
        (destination / "data" / f"{name}.json").write_text(json.dumps(rows))
    provenance = {
        "source": URL,
        "sha256": SHA256,
        "license": "CC-BY-4.0",
        **counts,
        "trainingRows": len(training),
        "developmentRows": len(development),
    }
    (destination / "data/provenance.json").write_text(json.dumps(provenance, indent=2))
    with (destination / ".gitignore").open("a") as stream:
        stream.write("data/\nmodel/\nreview.html\nresults.json\n")
    return provenance


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    parser.add_argument("--archive", type=Path)
    args = parser.parse_args()
    print(json.dumps(prepare(args.destination, args.archive), indent=2))

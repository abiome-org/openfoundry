from __future__ import annotations

import importlib
import json
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from openfoundry.candidate_review import review
from openfoundry.errors import ConfigurationError

if TYPE_CHECKING:
    from openfoundry.experiments import ExperimentService


def track(service: ExperimentService, run_id: str, uri: str) -> dict[str, Any]:
    from openfoundry.run_support import _operation_lease

    service.factory._authorize("experiment.track")
    report = review(service, run_id, details=True)
    candidate = report["candidate"]
    try:
        tracking = importlib.import_module("mlflow.tracking")
    except ImportError as exc:
        raise ConfigurationError("install openfoundry[tracking] to export to MLflow") from exc
    client = tracking.MlflowClient(tracking_uri=uri)
    lock = service.factory.paths.state / "operations" / f"{candidate['runId']}.tracking.lock"
    with _operation_lease(lock):
        experiment = client.get_experiment_by_name(candidate["experiment"])
        experiment_id = (
            experiment.experiment_id
            if experiment
            else client.create_experiment(
                candidate["experiment"],
                artifact_location=(service.factory.paths.state / "tracking").as_uri()
                if urlsplit(uri).scheme.startswith("sqlite")
                else None,
            )
        )
        found = client.search_runs(
            [experiment_id],
            filter_string=f"tags.`openfoundry.runId` = '{candidate['runId']}'",
            max_results=1,
        )
        run = (
            found[0]
            if found
            else client.create_run(
                experiment_id,
                tags={
                    "openfoundry.runId": candidate["runId"],
                    "mlflow.runName": candidate["name"],
                    "openfoundry.definitionDigest": candidate["reproduce"]["definitionDigest"],
                },
            )
        )
        tracking_id = run.info.run_id
        for name, value in candidate["parameters"].items():
            client.log_param(tracking_id, name, json.dumps(value, sort_keys=True))
        for name, value in candidate["scores"].items():
            if isinstance(value, (float, int)):
                client.log_metric(tracking_id, name, float(value))
        for name in ("wallSeconds", "cpuSeconds"):
            if candidate["measurement"][name] is not None:
                client.log_metric(tracking_id, name, candidate["measurement"][name])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "openfoundry-review.json"
            path.write_text(json.dumps(report, indent=2))
            client.log_artifact(tracking_id, str(path))
        client.set_terminated(tracking_id)
    return {"runId": candidate["runId"], "trackingRunId": tracking_id, "trackingUri": uri}

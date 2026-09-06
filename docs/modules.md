# Modules

A module is a directory with a `module.yaml` and an executable that speaks
`openfoundry.module/v1`. Trainers, evaluators, serving adapters, data transforms, and
environments are all modules; OpenFoundry does not distinguish roles.

## Manifest

```yaml
apiVersion: openfoundry.dev/v1alpha1
kind: Module
metadata:
  name: affine-regression
spec:
  entryPoint:
    command: [python3, main.py]
    codeRoot: .
  environment:
    dependencyLock: requirements.lock
    dependencyDigest: sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
  contracts:
    input: {type: object}
    output: {type: object}
    config: {type: object}
    state: {type: object}
  checkpoint: true
  fixtures:
    - request: {operation: run, inputs: {input: 3.0}, state: {slope: 2.0, intercept: 1.0}}
      result: {status: ok, outputs: {prediction: 7.0}}
```

- `entryPoint.command` is the argument vector, run without a shell from
  `codeRoot` (default `.`). The executable must not be an absolute path; a
  relative path must stay inside the code root and be executable.
- `environment.dependencyLock` names a file inside the code root and
  `dependencyDigest` is its SHA-256. An empty lock runs the interpreter as it
  is. A non-empty lock is a hash-pinned `pip` requirements file; the local
  executor realizes it into a cached virtual environment under
  `.openfoundry/environments/` and layers the interpreter's own site directories after
  it so `openfoundry.sdk` stays importable.
- `contracts` are self-contained JSON Schemas (no `$ref`) for the request
  inputs, config, and state and for the result outputs and state. A missing
  contract accepts any object.
- `checkpoint: true` declares that the module may emit a `checkpoint` artifact.
- `fixtures` are request and result pairs that `openfoundry module test` executes. A
  module without fixtures is tested with one `validate` request that must
  return `status: ok`.

## Protocol

The executor writes one request file and expects one result file. The paths
arrive in the environment as `OPENFOUNDRY_REQUEST_FILE` and `OPENFOUNDRY_RESULT_FILE`;
`OPENFOUNDRY_RUN_ID` identifies the execution.

Request:

```json
{
  "protocol": "openfoundry.module/v1",
  "operation": "run",
  "inputs": {"dataset": {"path": "/abs/stage/inputs/dataset", "manifestDigest": "sha256:..."}},
  "config": {"action": "train", "steps": 500},
  "state": {},
  "context": {"runId": "...", "stage": "train", "runDirectory": "..."}
}
```

`operation` is one of `validate`, `prepare`, `run`, `quiesce`, `checkpoint`,
`restore`, or `stop`. `inputs` carries the resolved stage inputs: a dataset
snapshot is an object with `path` (the restored payload) and `manifestDigest`;
a release or checkpoint reference adds `state` (the protocol state that was
published with it) and, for releases, `modelPackageRef`. An artifact output such
as `train.model` is restored to the same path descriptor; an inline output such
as `train.modelState` carries its value unchanged. `config` is
the stage's semantic configuration verbatim.

Result:

```json
{
  "protocol": "openfoundry.module/v1",
  "status": "ok",
  "outputs": {"loss": 0.000001, "modelState": {"slope": 2.0, "intercept": 1.0}},
  "state": {"slope": 2.0, "intercept": 1.0, "format": "json-affine/v1"},
  "metrics": {"steps": 500},
  "artifacts": [{"name": "model", "kind": "model", "path": "model.json"}],
  "error": null
}
```

`status` is `ok` or `error`; an error carries `code`, `message`, and
`details`. Every declared stage output must appear in `outputs` or be the name
of an artifact. Artifact paths are relative to the stage run directory and
must stay inside it; each artifact is imported into the content-addressed
store and its digest becomes the output value. An artifact of kind
`checkpoint` is published atomically together with the result `state` as a
`Checkpoint` resource; the module must declare `checkpoint: true`, return a
non-empty state, and emit at most one checkpoint per stage.

The Python SDK wraps the exchange:

```python
from openfoundry.sdk import ProtocolRequest, ProtocolResult, main

def run(request: ProtocolRequest) -> ProtocolResult:
    return ProtocolResult(status="ok", outputs={"echo": request.inputs})

if __name__ == "__main__":
    raise SystemExit(main({"validate": lambda _r: ProtocolResult(status="ok"), "run": run}))
```

Any language works: read the request file, write the result file atomically,
exit non-zero on error. Modules run with network denied and only `PATH`,
`HOME`, `LANG`, and `TZ` from the parent environment.

## Commands

```sh
openfoundry module init modules/my-trainer --name my-trainer
openfoundry module validate modules/my-trainer/module.yaml
openfoundry module test modules/my-trainer/module.yaml --binding bindings/local.yaml
```

`init` scaffolds a manifest, an empty lock, and a `main.py` that echoes its
inputs. `validate` loads the manifest, confines the code root, verifies the
lock digest, checks the contracts, and captures the source as a reproducible
tar whose digest identifies the module in every run. `test` prepares the
environment through the binding's executor and runs the fixtures.

## Source capture

A run never executes the checkout directly. At admission OpenFoundry packages each
stage's module directory (excluding `.git`, `.venv`, `.openfoundry`, caches, and any
`secrets` directory) into a content-addressed artifact, extracts that package
into the run directory, and records the package digest, the module digest, and
the environment digest in the `Run` resource. Changing the checkout after
admission does not change what runs, and a recovered run re-verifies the
captured source before continuing.

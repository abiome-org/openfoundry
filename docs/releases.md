# Releases and deployments

A release names an immutable model version: artifacts, the captured recipe and
runtime, exact data revisions, and measured evidence. Its signed manifest points
to the admitted run and result. Evaluation may be failed or absent; saving a
version preserves what happened.

```sh
openfoundry release create run/<run-id> --name v1 --intended-use "Classify incoming text"
openfoundry release show v1
openfoundry release promote v1 --alias candidate
openfoundry release list
```

`release create --promote` combines saving and selection. Promotion and deployment
verify the signature and check current data rights and project requirements.
By default, evaluation must pass. Projects can also require compatibility, metric
thresholds from recorded scores, or a vulnerability scan, or allow selection
without a passing evaluation:

```yaml
config:
  promotion:
    requireEvaluationPass: true
    requireCompatibilityPass: false
    requireVulnerabilityScan: false
    thresholds:
      accuracy: {minimum: 0.8}
```

Lineage and current rights are always checked. Actor authorization is enforced
by the project's rules. An alias move is atomic; `--expected-version <version>`
rejects a stale update. Saving a new release revision never changes an existing
alias implicitly. `release show` includes `aliasVersions` for guarded updates.
References accept `release/<name>`, `alias/<name>`, or an exact release URI.
Deployments resolve the reference once and preserve that revision for rollback.
Launch and rollback recheck current requirements under dataset-rights locks.

## Optional vulnerability evidence

`openfoundry release evidence run/<run-id>` prints a report skeleton with the aggregate
model and captured source digests. Populate it with actual scanner output and
pass it to `release create --vulnerability-report <path>`. OpenFoundry imports the report;
it does not perform a vulnerability scan. The report records scanner identity,
database revision, a timestamp with timezone, subjects, findings, and waivers.
Open high or critical findings block promotion unless their IDs are waived.
A supplied failing report blocks promotion even when scanning is optional.

## Deployments

```yaml
apiVersion: openfoundry.dev/v1alpha1
kind: DeploymentSpec
metadata:
  name: affine-service
spec:
  releaseRef: release/affine-v1
  extensions:
    form: service
    port: 8090
```

```sh
openfoundry --actor deployment-operator deploy deployments/affine-service.yaml
openfoundry deployment list
openfoundry deployment status affine-service
openfoundry deployment cancel affine-service
openfoundry deployment rollback affine-service --expected-version <status-version>
```

`deploy` verifies the release against current promotion requirements, applies
the deployment manifest as an immutable revision, and
launches it through the executor named in `extensions.executor` (default
`local`, options in `extensions.executorConfig`). Forms:

| Form | Behavior |
| --- | --- |
| `edge` | Packages the release; nothing runs |
| `service` without `command` | Serves the release through its model package's inference adapter |
| `service`, `batch`, `actor`, `control` with `command` | Runs the given argv under the executor |

A served release restores the adapter source admitted with the run, checks
its environment digest against the admitted one, restores the artifact named by
`stateOutput`, and passes its usable file or directory as `request.state["path"]`.
Inline state objects also work; see [model packages](evaluation.md#model-packages).
The server listens on `extensions.host` and `extensions.port`
(default `127.0.0.1:8090`). `GET /healthz` reports the release revision and
request counters; `POST /v1/infer` with `{"inputs": {...}}` validates the
inputs against the package input signature, runs one module exchange, and
validates the outputs against the output signature. Error responses carry
codes and request ids, never request values.

Status changes are compare-and-set on `statusVersion`: read the current
status, pass its version to `rollback`, and refresh after a stale-version
error instead of retrying blindly. Rollback relaunches the previous immutable
deployment revision.
Reapplying the same manifest leaves a running deployment in place and starts a
new instance when the earlier one stopped or failed.

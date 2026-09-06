# Workloads

A `WorkloadSpec` says what runs: a graph of stages, each executing one module
operation. A `Binding` says where it runs. Rebinding never changes the graph.

## Workload manifest

```yaml
apiVersion: openfoundry.dev/v1alpha1
kind: WorkloadSpec
metadata:
  name: example-from-scratch
spec:
  modelPackageRef: modelpackage/example-affine
  evaluationRefs: [evaluationspec/example-affine]
  graph:
    stages:
      - name: train
        module: modules/examples/affine-regression/module.yaml
        operation: run
        inputs: {dataset: dataset/example-affine}
        config: {action: train, learningRate: 0.02, steps: 500}
        outputs: [modelState, loss, model, checkpoint]
      - name: evaluate
        needs: [train]
        module: modules/examples/affine-regression/module.yaml
        inputs: {modelState: train.modelState}
        config: {action: evaluate, tolerance: 0.01}
        outputs: [passed, maximumError]
```

Stage inputs name one of:

| Reference | Resolves to |
| --- | --- |
| `dataset/<name>` | The pinned snapshot revision, restored under the stage's `inputs/` |
| `<stage>.<output>` | An output of a stage listed in `needs` |
| `release/<name>` | The release's model and state artifacts plus its model package reference |
| `checkpoint/<name>` | The checkpoint's module state and protocol state |
| `sha256:<digest>` | The restored payload of that artifact manifest |

Every reference is pinned to an exact revision when the run is created,
verified in the local store before allocation, and recorded as a `used`
lineage edge. `modelPackageRef` and `evaluationRefs` are described under
[evaluation](evaluation.md).

## Binding manifest

```yaml
apiVersion: openfoundry.dev/v1alpha1
kind: Binding
metadata:
  name: local
spec:
  executor: local
  resources:
    timeoutSeconds: 3600
    addressSpaceBytes: 8589934592
  config: {}
```

`executor` names a provider from `openfoundry executor list`; an unknown provider is
an error, never a fallback to local. `resources` are POSIX limits the local
executor applies to every module process: `cpuSeconds`, `addressSpaceBytes`,
`processes`, `fileSizeBytes`, and `timeoutSeconds`. `config` holds the
provider's own options, validated against the contract the provider
publishes; the local provider accepts `dependencyWheelhouse` and
`dependencyIndex` for lock realization.

## Running

```sh
openfoundry executor preflight bindings/local.yaml --workload workloads/example-from-scratch.yaml
openfoundry --actor research-agent run workloads/example-from-scratch.yaml --binding bindings/local.yaml
openfoundry runs list
openfoundry runs status <run-id>
openfoundry lineage show run:<run-id>/stage:train
```

Preflight instantiates the provider and reports missing capabilities and host
issues without creating anything. `run` performs admission and executes the
graph in this process; `run --detach` records an operation and starts a
worker, and `openfoundry operation list`, `get`, and `reconcile` follow it.

Admission does all of the following before allocating compute, and records the
result in the immutable `Run` resource:

1. Loads the workload and binding, checks the policy for `workload.run`, and
   applies the dirty-worktree rule.
2. Pins datasets, references, the model package, and evaluation specs to exact
   revisions and checks rights for each stage's training or evaluation use.
3. Captures every stage module and the inference adapter as content-addressed
   packages and prepares their environments through the executor.
4. Writes the run state under `.openfoundry/runs/<run-id>/state.json` and transitions
   it through `Validated`, `Admitted`, and `Running`.

Each stage then runs one module exchange; its outputs and imported artifacts
become the run's outputs, and lineage links the stage to its inputs, module
source, and artifacts. A succeeded run publishes an immutable `RunResult`, and
`runs status` reports the terminal state, outputs, and result reference.

## Recovery

An interrupted controller can be resumed with
`openfoundry operation reconcile <operation-id>` under the original actor. OpenFoundry
reattaches to a submitted execution, verifies that the captured sources, plan
digests, and admission digests match what was recorded, and continues. If the
outcome cannot be established from durable evidence, the run is marked
`Failed` with reason indeterminate and nothing is replayed; module work is
not assumed to be idempotent. A run whose `RunResult` was published before the
interruption is reconciled from that result alone.

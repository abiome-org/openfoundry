# OpenFoundry

OpenFoundry runs model experiments from ordinary training and evaluation
scripts. Define the task, candidates, metrics, and limits in one YAML file. OpenFoundry
captures source and data, runs the work, compares results, and exports the model
with its evidence. Your agent chooses what to try next.

## Try a real model

On Linux with Python 3.11 or 3.12 and unprivileged user namespaces:

```sh
make setup
make example
```

This downloads the public UCI SMS corpus, trains three scikit-learn classifiers,
interrupts and recovers a controller, selects a candidate, reproduces its scores,
saves and selects a release, and tests exported inference in a fresh environment. Open
`.venv/text-classification/review.html` for the comparison and `results.json`
for timings, compute, and reproduction evidence. Use `EXAMPLE_DIR=/new/path`
to repeat it. The [example definition](examples/text-classification/experiment.yaml)
and [model card](examples/text-classification/MODEL_CARD.md) describe the experiment.

## Bring your scripts

Install OpenFoundry into your environment, then run from your model project:

```sh
openfoundry experiment init --name my-model --objective "Improve useful task performance" --source src
# Edit experiment.yaml to name your scripts, data, outputs, metrics, and candidates.
openfoundry experiment run experiment.yaml --candidate baseline
openfoundry experiment search experiment.yaml
openfoundry experiment leaderboard experiment.yaml
openfoundry experiment list
openfoundry experiment status <run-id>
openfoundry experiment review <run-id> --baseline <baseline-id> --html review.html
openfoundry experiment reproduce <run-id>
openfoundry experiment export <run-id> --to model
```

Commands are argument lists without a shell. Use `{inputs[name]}` for data or
model paths, `{parameters[name]}` for candidate settings, and `{output}` for the
output directory; double literal braces. Each script's optional `inputs` selects
which datasets or training artifacts it receives. Scripts need no OpenFoundry imports.
Evaluation writes a JSON object of finite numeric metrics and explicit boolean
`passed` and `compatibilityPassed` checks. Optional examples are JSON records with
stable `id`, `input`, `expected`, `prediction`, and numeric `score` fields.

Runs capture uncommitted source by default and respect Git ignore rules.
Review prints a compact comparison; `--details` includes examples, diffs, and
runtime evidence. `dependencies` names a hash-pinned
binary requirements lock inside each script's source directory. Separate training
and evaluation source directories when their code changes independently: changes
to the evaluator's captured source, inputs, or protocol flag comparisons for review.
Use results to improve training and selection; report reused evaluation data as
development evidence. `openfoundry experiment schema` exposes the full definition schema.

Runs survive agent sessions. Use `openfoundry operation reconcile <run-id>` to resume and
`openfoundry operation cancel <run-id> --reason "Try another candidate"` to stop. Each stage
has a wall timeout; local CPU, memory, process, and file-size limits use POSIX
process limits. Bindings also carry accelerator requests, memory, cost caps, and
preemptible flags; local runs CPU-only and reports an issue when accelerators are
requested. Measured wall/CPU/GPU time and estimated cost are reported separately
from configured limits. Candidates branch with `from: run/<id>` (or a checkpoint,
release, or alias); a declared script `checkpoint` output is published for reuse.

For MLflow, install `openfoundry[tracking]`, then run
`openfoundry experiment track <run-id> --uri sqlite:///tracking.db` or use your tracking
server URI. Repeated exports reuse the same MLflow run. CLI and authenticated
HTTP expose the same workflow; `openfoundry agent capabilities experiment.run` describes it.

## Releases and deployment

Save a named version, then select it when it meets your project's requirements:

```sh
openfoundry release create <run-id> --name v1 --intended-use "Classify incoming text"
openfoundry release promote v1 --alias candidate
```

Releases preserve exact data, captured code, artifacts, and evaluation evidence.
Failed evaluation does not prevent saving a version. Selection requires passing
evaluation by default; projects configure metric thresholds and additional checks
in promotion policy. See
[releases](docs/releases.md) for policy and serving, or the
[walkthrough](docs/walkthrough.md) for a complete workload lifecycle.
`./install.sh /path/to/model-project` installs a runnable starter and operator
guide. Existing files are preserved.

OpenFoundry 2 supports the local lifecycle on CPython 3.11/3.12 on Linux x86-64 and
the `openfoundry.executor/v1` plugin contract. Only local execution is built in. Production
scale, cluster recovery, and full air-gap operation require deployment-specific
evidence. See [operations](docs/operations.md) for installation and support policy.

## Reference

| Page | What it covers |
| --- | --- |
| [Projects](docs/projects.md) | Configuration, namespaces, actors, policies |
| [Modules](docs/modules.md) | Protocol, manifests, dependency locks, fixtures |
| [Data](docs/data.md) | Snapshots, rights, verification, stores, sync |
| [Workloads](docs/workloads.md) | Stage graphs, bindings, runs, recovery |
| [Evaluation](docs/evaluation.md) | Metrics, compatibility, comparisons |
| [Releases](docs/releases.md) | Promotion, aliases, deployments, serving |
| [Executors](docs/executors.md) | Provider capabilities and plugin API |
| [Agent state](docs/agent-control.md) | Factory context and command discovery |
| [Architecture](docs/architecture.md) | Code ownership and invariants |

Git holds code and configuration. Artifact stores hold data and models. `.openfoundry/`
holds untracked runtime state. For development, use `make check`,
`make test TEST_ARGS='tests/test_experiments.py -q'`, and `make test-all`.
See [CONTRIBUTING.md](CONTRIBUTING.md) and [AGENTS.md](AGENTS.md).

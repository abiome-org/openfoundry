<!-- BEGIN OpenFoundry OPERATOR GUIDE -->
# Operating this OpenFoundry

Build a useful model for the user's task. Read `MODEL_CARD.md` for purpose,
interface, and measured targets. Use evaluation feedback to improve training
and selection; describe reused evaluation data as development evidence.
Reserve fresh cases when an independent estimate is needed.

User intent and host instructions take precedence over this guide. Carry
already authorized work through verification; resolve routine choices yourself.
Respect actual policy and tool denials. Report an unresolved blocker clearly.

## Work with the factory

Use `.venv/bin/openfoundry` in an installed project. Global options precede subcommands.
The local actor defaults to the configured project owner. `--project` selects
that directory exactly; `--actor` explicitly selects an existing identity.

```sh
openfoundry agent context
openfoundry agent capabilities experiment.run
openfoundry experiment init --name my-model --objective "The user's task" --source src
```

Edit `experiment.yaml` to name data, scripts, outputs, metrics, candidate
parameters, and limits. Add a `search` block (grid/count, concurrency, budget)
to sweep instead of hand-editing candidates, and `from: run/<id>` (or a
checkpoint, release, or alias) to branch a candidate from prior work. A train
`checkpoint` path is published for reuse. Scripts need no OpenFoundry imports.
Source capture respects Git ignores and archives uncommitted edits by default.
Dependency locks live inside each script's source directory. Custom stage graphs
can use modules, workloads, evaluation specs, and bindings directly. On macOS
(no user namespaces), set `network: allow` on dev scripts and
`permitUnisolated: true` in the provider config; `deny` stages fail closed.

```sh
openfoundry experiment run experiment.yaml --candidate baseline
openfoundry experiment search experiment.yaml
openfoundry experiment leaderboard experiment.yaml
openfoundry experiment list
openfoundry experiment review <run-id> --baseline <baseline-id>
openfoundry experiment reproduce <run-id>
openfoundry experiment export <run-id> --to model
openfoundry release create <run-id> --name v1 --intended-use "The user's task"
openfoundry release promote v1 --alias candidate
```

Inspect scores, regressions, held-out exposure, changed examples, source/data
revisions, and measured compute and cost. Use `--details` for full review
evidence. `review` ranks by the primary metric; pairwise diff is the detail view.
When an evaluator changes, inspect the comparison and remeasure as needed. Update
the model card with conclusions and the evidence behind them.

After interruption, `openfoundry operation reconcile <run-id>` resumes admitted work;
`openfoundry operation cancel <run-id> --reason "<reason>"` stops it. For alias moves
and deployment rollback, use the observed version with `--expected-version`
when protecting against concurrent changes. Refresh after a conflict.

## Preserve useful history

Git holds source and configuration, artifact stores hold data and models, and
`.openfoundry/` holds generated runtime state. Use `openfoundry admin backup` and verified
restore for that state. Keep secrets and sensitive payloads out of Git, logs,
and shared context; secret input supports a hidden prompt or `--value-stdin`.

Record each dataset's rights and its training or evaluation role. A release
preserves a model and its evidence, including failed or missing evaluation.
Promotion and deployment check current data rights, signatures, lineage, metric
thresholds, and project requirements. Evaluation must pass by default;
compatibility, metric thresholds, and vulnerability scanning are additional
project options. Record real scanner output when used. No invented reviewer or
report is needed to save a version.

Unknown or unready executors fail before allocation. Report only capabilities,
recovery behavior, and scale supported by observed tests.
<!-- END OpenFoundry OPERATOR GUIDE -->

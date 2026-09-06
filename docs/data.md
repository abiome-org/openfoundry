# Data

A `DatasetSnapshot` is an immutable, rights-bearing identity for data a
workload consumes. Runs pin the exact snapshot revision; later imports under
the same name are new revisions.

## Adding data

```sh
openfoundry data add data/fixtures/affine.jsonl --name example-affine --mode copy \
  --rights data/fixtures/rights.yaml
openfoundry data verify example-affine
openfoundry data list
```

| Mode | Behavior |
| --- | --- |
| `copy` | Imports the bytes into the local content-addressed store; the only mode a workload can execute |
| `register` | Leaves the bytes in place and records size, modification time, and digest |
| `mount` | Records a path an executor provides, with the same drift checks |
| `stream` | Records a credential-free URI and requires a cursor or version policy |

`verify` re-checks the stored or registered bytes against the recorded
digests. The rights document declares at least `license` and
`trainingAllowed`; add consent basis, redistribution, attribution, sensitivity,
retention, residency, and export terms as they apply. Training inputs require
`trainingAllowed: true`. Evaluation-only inputs can retain `trainingAllowed: false`;
`evaluationAllowed: false` explicitly denies evaluation. Script experiments declare
these uses from their train/evaluate stages. Lower-level workload stages default
to `dataUse: training` and can declare `dataUse: evaluation`.
Rights are checked against the pinned and newest revisions at admission, execution,
recovery, release, and promotion. Revocation denies both uses.

```sh
openfoundry --actor data-steward data revoke example-affine --reason "consent withdrawn"
```

Revocation creates a new revision that denies training. Queued runs fail at
admission and promotions are denied; existing evidence is never rewritten.

## Roles and separation

Use training data, test failures, reward signals, and evaluation results to
improve the model. Record which snapshots supplied training examples, feedback,
or candidate-selection evidence so later comparisons have the right context.

If a reserved test reveals a useful failure, learn from it and record that it
influenced development. The result remains useful evidence; an independent
measurement then needs fresh or still-reserved cases. The same scoring code can
serve training and evaluation when it measures the intended behavior correctly.

Choose data splits and access boundaries for the experiment. Where a measurement
depends on unseen answers, keep those answers out of model-visible inputs.
Protect credentials and score records from modification by evaluated code.
Separate snapshots make data use traceable; they do not by themselves enforce
blind evaluation.

## Stores and sync

The local store lives under `.openfoundry/store`. Additional stores hold replicas:

```sh
openfoundry store add secondary --driver filesystem --endpoint .openfoundry/secondary
openfoundry admin secret set primary --purpose artifact-store-credentials \
  --value '{"aws_access_key_id":"...","aws_secret_access_key":"..."}'
openfoundry store add primary --driver s3 --endpoint s3://bucket/prefix --secret-ref primary
openfoundry sync push dataset/example-affine --to secondary --plan
openfoundry sync push dataset/example-affine --to secondary
```

S3 credentials are an encrypted secret with purpose
`artifact-store-credentials`; accepted keys are `aws_access_key_id`,
`aws_secret_access_key`, `aws_session_token`, `region_name`, `use_ssl`, and
`verify`. Sync transfers only missing chunks, verifies every chunk at the
destination, publishes the manifest last, and never deletes. `--plan` reports
the transfer without mutating anything; the default policy requires a plan
before a transfer.

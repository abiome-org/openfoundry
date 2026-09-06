# Operations runbook

## Breaking rename: OpenFoundry (unreleased)

This rebrand changes public and internal names. It does not automatically migrate
existing projects, runtime state, backups, or signed artifacts. The previous
OpenModelFactory checkout already used version 2.0.0; this is a separate change.

| Interface | Previous name | New name |
|---|---|---|
| CLI and Python package | `omf` | `openfoundry` |
| Distribution | `open-model-factory` | `openfoundry` |
| Project configuration | `omf.yaml` | `openfoundry.yaml` |
| Runtime directory | `.omf/` | `.openfoundry/` |
| Environment prefix | `OMF_` | `OPENFOUNDRY_` |
| Schema and protocol prefixes | `omf.dev/`, `omf.` | `openfoundry.dev/`, `openfoundry.` |
| Base error class | `OMFError` | `OpenFoundryError` |

Update integrations, manifests, environment variables, and container builds
before using a fresh OpenFoundry project. Rebuild custom executor plugins for the
new package and entry-point names. Existing installer-managed environments also
use the old marker names; use a separate project directory for a fresh install.

Back up existing projects and retain their exact pre-rebrand checkout and
compatible environment for inspecting or restoring old state. Do not bulk-edit
stored manifests, signed releases, or backups: changing names can invalidate
hashes and signatures. Renaming a runtime directory alone is not a migration.
Cross-brand restore and import are not verified or supported by this change.

## Install and initialize

Use the directory installer for a new or existing project. Inspect its complete
plan before allowing package downloads or local state creation:

```sh
./install.sh --plan /path/to/model-project
./install.sh /path/to/model-project
. /path/to/model-project/.venv/bin/activate
openfoundry --project /path/to/model-project --output json agent context
```

The installer preserves existing manifests and appends rather than replaces an
existing `MODEL_CARD.md`, `AGENTS.md`, or `.gitignore`. It creates missing
versioned project configuration, initializes Git only when needed, prints and
applies the initialization plan, then requires `openfoundry doctor` and bounded agent
context to succeed. Reinstallation builds a fresh environment from the selected
base interpreter and atomically replaces only a `.venv` carrying the OpenFoundry
installer marker. An unrelated pre-existing `.venv` is rejected rather than
executed or overwritten. Pip may
use the configured package index only for hash-locked binary runtime and build
dependencies; OpenFoundry itself builds without an isolated backend download. The plan
discloses that network effect. OpenFoundry creates no hosted account, uploads no project
metadata, and leaves call-home telemetry disabled.

For a manual development installation, use Python 3.11 or 3.12 in an isolated
environment:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --only-binary=:all: --require-hashes -r requirements.lock
python -m pip install --only-binary=:all: --require-hashes -r requirements.build.lock
python -m pip install --no-build-isolation --no-deps -e .
openfoundry bootstrap
openfoundry doctor
```

The literal `openfoundry bootstrap` command is idempotent. It initializes
repository-local state under `.openfoundry` with a restrictive umask. Back up
`.openfoundry/identity` separately from artifact data and restrict it to the factory
operator. The local bearer token is encrypted in the metadata database and is
intentionally never printed by the CLI.

## Service operation

`docker compose up --build -d` starts the authenticated API after the project
has been initialized. Terminate TLS at the site's ingress or reverse proxy; the
built-in server does not provide TLS or mutual workload authentication. The
initial operator credential is an all-scope local token; obtain it through an
authorized operator workflow, not logs or Git. Create expiring least-privilege
credentials tied to a named actor with
`openfoundry admin token create --actor <identity> --scope read`. Revoke them with
`openfoundry admin token revoke <token-id>`. Token values are stored only as hashes and are
returned once at creation.

For direct service operation:

```sh
openfoundry api serve --host 127.0.0.1 --port 8080
```

Use a process supervisor such as systemd, Kubernetes, or the site's scheduler.
Run one writer service per SQLite metadata database. WAL permits concurrent
readers, but a shared network filesystem must honor SQLite locking semantics.

## Executor readiness

Inventory provider code and inspect its source before authorizing it, then
preflight the exact binding and workload:

```sh
openfoundry --output json executor list
openfoundry --output json executor preflight bindings/site.yaml --workload workloads/train.yaml
```

Installed `openfoundry.executors` entry points are trusted code loaded into the API
process. Unknown providers, missing protocol transport, unavailable scheduler
tools, and unenforceable isolation stop with an error before OpenFoundry allocates a
run. A successful scheduler preflight does not prove operation at scale; test
portable workloads, restart, cancellation, checkpoints, and supported scale. See
[the executor provider guide](executors.md) for backend-specific requirements.

## Backup and restore

Before upgrading, create and verify a backup.

Create one verified archive containing metadata, signing and encryption keys,
encrypted secrets, and every local content-addressed artifact:

```sh
openfoundry admin backup /secure-backups/factory-$(date +%Y%m%d).openfoundry-backup
```

The archive contains sensitive key material and is created with mode `0600`.
Protect and replicate it as a secret. Record the reported signing key ID in a
separate trusted location.

Restore into a checkout with the same `openfoundry.yaml` and no `.openfoundry` directory:

```sh
openfoundry admin restore /secure-backups/factory-20260903.openfoundry-backup \
  --expected-key-id sha256:<recorded-key-id>
openfoundry doctor
```

1. stop writers;
2. retain the failed `.openfoundry` directory for forensics;
3. run `openfoundry admin restore` with the separately recorded key ID;
4. reconnect any external artifact stores;
5. run `openfoundry doctor`, verify dataset snapshots, inspect runs and releases, and
   inspect signed event tails;
6. resume deployments only after policy review.

Restore verifies the signed inventory, database and migration history, resource
and event digests, local artifacts, and encrypted secrets before atomically
creating `.openfoundry`. It refuses to replace existing state.

## S3-compatible artifact stores

Store credentials as an encrypted JSON secret with purpose
`artifact-store-credentials`. Accepted keys are `aws_access_key_id`,
`aws_secret_access_key`, `aws_session_token`, `region_name`, `use_ssl`, and
`verify`.

```sh
openfoundry admin secret set primary \
  --purpose artifact-store-credentials \
  --value '{"aws_access_key_id":"…","aws_secret_access_key":"…"}'
openfoundry store add primary --driver s3 --endpoint s3://bucket/prefix --secret-ref primary
openfoundry sync push dataset/training-corpus --to primary --plan
openfoundry sync push dataset/training-corpus --to primary
```

Use `--plan` before each production transfer. Sync never deletes destination
content and publishes the manifest only after every chunk verifies.

## Vulnerability evidence and release promotion

Projects can require vulnerability scanning with `promotion.requireVulnerabilityScan`.
Without that requirement, scanning is optional. A supplied report must cover the
model and admitted sources and contain no unwaived open high/critical findings.
OpenFoundry stores imported evidence; it does not run a scanner. See
[releases](releases.md#optional-vulnerability-evidence) for the report format.

## Deployment lifecycle and rollback

Deployment commands run through the explicit executor provider recorded in the
immutable deployment revision (local by default). Inspect and cancel them with
`openfoundry deployment status <name>` and
`openfoundry deployment cancel <name>`. Each status response includes `statusVersion`;
use that value as the compare-and-swap guard when restoring the previous
immutable deployment revision:

```sh
openfoundry deployment rollback <name> --expected-version <status-version>
```

A stale version is rejected rather than overwriting a concurrent deployment
change. Deployment and rollback verify the pinned release signature and check
current data rights and project requirements before launch.

## Air-gapped installation

On a connected build host, build wheels for OpenFoundry and every dependency, generate
an inventory and checksums, scan them under site policy, and sign the bundle.
Transfer through the approved media process. In the isolated environment,
verify signatures and hashes and install with:

```sh
python -m pip install --no-index --find-links ./wheels openfoundry
```

Run with network namespace denial or a site sandbox and verify no external
traffic. Offline installation alone does not prove that the full lifecycle is
air-gapped; test the complete supported workflow with egress denied.

## Distribution release

CLI/HTTP contracts and persisted formats are versioned. OpenFoundry 2 removes generic
agent planning APIs and uses `openfoundry.release/v2` manifests. Existing run, dataset,
and artifact history remains intact. Back up before upgrading; remove legacy
`unsignedModules`, `sync`, and `promotion.requireCompleteLineage` policy keys,
and recreate releases from their recorded runs before promoting or deploying
with version 2, then reapply deployment manifests to pin those releases. Old
releases remain available for historical inspection. Executor providers still use
`openfoundry.executor/v1`; their package dependency must permit OpenFoundry 2.

`make release-candidate` builds the wheel and source archive twice, rejects
non-reproducible bytes, and emits checksums, an SPDX SBOM, and SLSA provenance.
Candidate provenance records the Git revision and a source-patch digest when the
checkout is dirty; final releases require a clean checkout.

The release test installs both artifacts without an index, runs `pip check`,
upgrades legacy state, and rehearses identity-preserving backup and restore.

A final release must run from the clean `v<version>` tag. Invoke
`tools/release.py` without `--candidate`, pass a current scanner report with
`--vulnerability-report`, and set `OPENFOUNDRY_RELEASE_SIGN_COMMAND` to the site's
approved signer wrapper. The wrapper receives the `SHA256SUMS` path as its final
argument and must create the adjacent `SHA256SUMS.sig`. The tool rejects
unwaived open high or critical findings and does not publish artifacts; upload
the completed directory only through the repository's approved release process.

## Incident and recovery rules

- Quarantine, do not mutate, suspect artifacts.
- Revoke compromised credentials and rotate site trust deliberately; historical
  signatures remain bound to the old key.
- Preserve event, lineage, scheduler, and ingress logs under retention policy.
- A failed or incomplete checkpoint is never a restore target.
- Reconciliation attaches to admitted executor receipts or finalizes immutable
  results. Unknown launch outcomes require inspection; OpenFoundry never blindly replays
  them. Use `openfoundry operation reconcile <operation-id>` under the original actor.
  `openfoundry operation cancel <operation-id>` durably records stop intent and confirms
  executor termination before reporting cancellation. A finalizing run completes
  its evidence publication; a late cancellation leaves that result intact.
- Alias and deployment changes require a recorded passing policy decision.
- State capacity limits only from reproducible benchmark runs on the actual
  hardware and topology being described.

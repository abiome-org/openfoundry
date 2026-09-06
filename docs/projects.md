# Projects

A project is a Git repository with an `openfoundry.yaml` at its root. Everything OpenFoundry
knows about the project lives either in versioned files next to it or in the
untracked `.openfoundry/` directory.
Without `--project`, OpenFoundry finds the nearest parent `openfoundry.yaml`. An explicit
`--project` selects that directory exactly.

## The project manifest

```yaml
apiVersion: openfoundry.dev/v1alpha1
kind: Project
metadata:
  name: my-model
  namespace: local/my-model
spec:
  owners: [local-user]
```

`metadata.namespace` is the identity every resource in the project carries.
Other manifests may omit their namespace; OpenFoundry stamps the project namespace when
it loads them, and it rejects a manifest that names a different one. `owners`
is optional; the first owner becomes the actor of the local API token that
`openfoundry bootstrap` creates. `spec.extensions.policyDirectory` changes where policy
documents are read from (default `policies`).

## Local state

`openfoundry bootstrap` creates `.openfoundry/` with a restrictive umask:

| Path | Content |
| --- | --- |
| `metadata.db` | SQLite: resources, statuses, aliases, events, lineage, operations, tokens, secrets |
| `identity/` | The signing key and the secrets encryption key |
| `store/` | The local content-addressed artifact store |
| `runs/` | Per-run directories: captured sources, requests, results, logs, state |
| `packages/` | Temporary module packages |
| `environments/` | Realized dependency-lock environments |
| `operations/` | Locks and logs of detached operations |

Never edit or commit `.openfoundry/`. `openfoundry doctor` checks the host, the project, the
database and its migration history, identity, stores, and policy loading, and
reports each finding with a remediation. `openfoundry admin backup` and
`openfoundry admin restore` move the whole directory as one signed archive; see
[operations](operations.md).

## Actors

Every mutation is attributed to an actor. The CLI and Python API default to the
first configured project owner, or `local-user` when no owner is set. Use
`openfoundry --actor <identity> ...` to select an existing policy identity explicitly;
HTTP uses the token's actor. Replace the scaffold's `local-user`
before sharing a project and issue separate scoped tokens to other operators.

## Policies

Every `Policy` document in the policy directory is loaded, validated, and
applied as one decision source. A project without policy documents allows
every actor; a project with them denies any action no rule allows and records
the denial as a signed `PolicyDecisionRecorded` event.

```yaml
apiVersion: openfoundry.dev/v1alpha1
kind: Policy
metadata:
  name: default
spec:
  rules:
    - name: allow-local-project-operations
      effect: allow
      match: {actor: local-user, resource: local/my-model}
  config:
    dirtyWorktree: archive
    promotion: {requireEvaluationPass: true}
```

Rules match an action (`workload.run`, `sync.execute`, `release.create`,
`release.promote`, `deployment.apply`, `data.revoke`, ...), an actor, and a
resource; `deny` overrides `allow`. `dirtyWorktree` governs admission: `deny`
admits a workload only from a committed tree with no uncommitted or untracked
files, `archive` admits a dirty tree and stores the patch as a
`worktree-patch` artifact referenced by the run, and `allow` records the state
without archiving. New projects use `archive`, so iteration does not require a
commit before each run. Promotion requirements control evaluation, compatibility,
and vulnerability checks; see [releases](releases.md). Unknown configuration
is rejected when policy loads.

## The model card

`MODEL_CARD.md` is the human record of purpose, interface, benchmark targets,
data boundaries, and risks. It is prose, not a resource: keep it short, link to
immutable results as they appear, and record decisions in its table with the
experiment revision that justified them.

## Inspecting a project

```sh
openfoundry doctor
openfoundry agent context
openfoundry resource list --kind WorkloadSpec
openfoundry runs list
openfoundry release list
openfoundry deployment list
```

`--output table` (the default) prints aligned columns for lists and YAML for
single objects; `--output json` and `--output yaml` print the full structures.

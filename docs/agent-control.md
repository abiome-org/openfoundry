# Factory state for agents

`openfoundry agent context` reports readiness, resource inventory, runs, deployments,
operations, and recent event metadata. It works before bootstrap and includes
an initialization plan when local state is missing. Your agent owns the task
and decides what to do with these facts.

```sh
openfoundry --output json agent context --limit 10 --max-bytes 16384
openfoundry --output json agent context --since <event-id> --focus <run-id>
openfoundry agent capabilities experiment.run
openfoundry agent capabilities release.promote
```

Context omits operation requests, results, errors, and event payloads. Focus
searches metadata only. Each page reports `returned`, `total`, and `truncated`.
`--limit` accepts 1–100 items per section; `--max-bytes` accepts 16 KiB–1 MiB.
Incremental cursors never advance past an omitted event. If even one event
cannot fit, the command reports a size error instead of losing that event.

`viewDigest` excludes the observation timestamp, so unchanged state has a stable
identity. HTTP exposes the same view at `GET /v1/agent/context`, with ETags and
`If-None-Match` support.

The version 2 action catalog describes actual commands, HTTP routes, required
token scopes, and whether an action mutates state. CLI registration, HTTP
routing, scope checks, and OpenAPI consume these same definitions. Use a
command's `--help` or OpenAPI for its input schema. Ordinary errors include
`code`, `message`, `retryable`, and, when relevant, `details` and `remediation`.

OpenFoundry does not store agent goals or generic knowledge, prescribe next actions,
or assign static approval labels. Task management belongs to the agent's host;
model evidence belongs to runs, evaluations, comparisons, and releases.

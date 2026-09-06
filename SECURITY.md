# Security policy

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability. Use GitHub's private
security-advisory flow for this repository and include affected versions,
reproduction steps, impact, and any known mitigations. Maintainers should
acknowledge a complete report within three business days and coordinate a fix
and disclosure timeline with the reporter.

## Security boundary

OpenFoundry treats module, dataset, and release input as untrusted. Local
process resource limits are defense-in-depth, not a VM-grade sandbox. Expose the
HTTP API only behind site-managed TLS and identity controls. Never commit `.openfoundry`,
private keys, API tokens, cloud credentials, or data payloads.

The newest `1.x` minor receives correctness and security fixes. The preceding
minor receives security fixes for six months after its successor; major version
1 receives security fixes for twelve months after 2.0 is released. Exact end
dates and exceptions are recorded in `CHANGELOG.md`. Security behavior is
supported only where the test suite and named deployment controls exercise it
directly.

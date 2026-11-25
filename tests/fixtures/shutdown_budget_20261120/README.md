These are the reviewed payloads and dependencies of the one-time 2026-11-20 shutdown-budget
installer, preserved from the tree merged in 8533570d0cf67ecd13a99495f2fe86cd465cd106.
Its hash checks intentionally continue to reject later lifecycle changes.
Current deployment scripts live in deploy/gcp and have separate tests.

backup.sh and retention.sh are also pinned to that same tree. The legacy
installer must reject later backup implementations; its allowlist is not a
rolling checksum of the current deployment scripts.

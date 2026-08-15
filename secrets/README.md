# Local secret files

`bash scripts/init-local.sh` creates the ignored files in this directory for the
loopback-only HTTP development stack. Files are group-read-only (`0440`) inside a
non-world-accessible directory; Compose grants that supplemental host group only
to the API/worker and Hub containers with different non-root UIDs.

Do not reuse these files in production. Production requires HTTPS and a managed
secret mechanism that mounts each secret only into its intended service. Never
commit generated files from this directory.

`admin_lifecycle_token` is intentionally mounted only into JupyterHub (to
register the service) and the operation worker (to execute audited cross-user
server lifecycle requests). It must never be mounted into the public API,
frontend, migration or bootstrap-profile containers.

`make domain-test-tls` creates a separate ignored `domain-test/` local CA, leaf
certificate and leaf key for the loopback-only HTTPS rehearsal. The Gateway sees
only `tls.crt` and `tls.key`; `ca.key` must remain on the test host and must never
be copied into a container or reused as an operating certificate authority. The
generator does not edit `/etc/hosts` or any OS/browser trust store.

# What the installer manages

Exculpata provides one guided setup process. On fresh installations it builds
and starts the application, PostgreSQL + pgvector, the HTTPS gateway (plus the
optional Kerberos proxy), ClamAV and its signature updater, and the `egress-proxy`
CONNECT relay as containers. Operators do not need a separate host ClamAV
installation for this stack.

Prepare the host using [the installation playbook](INSTALL.md): service account,
storage, Docker Engine (`>= 26.0.0`) and Compose v2 (`>= 2.20.2`). Backups also
require `restic`. The installer checks prerequisites; it does not silently
reconfigure the Docker daemon, replace drivers, open firewall ports or change
other applications. Review TLS and exposure before remote use.

Fresh installations on `main` do not download or bundle model containers. Instead,
operators run or procure their own HTTPS model APIs (generation, embeddings,
reranking, and speech) and connect them per
[model configuration](MODEL_CONFIGURATION.md). Existing `v0.1.0-beta.1` nodes
that still use bundled GPU workers retain their staged model cache on `--resume`
and `update` (and can migrate in place via `--retire-bundled-models`). For those
legacy bundled nodes, see [model acquisition](MODEL_ACQUISITION.md) and the
[ASR import interface](ASR_MODEL_IMPORT.md).

## Managed service security updates

The gateway, ClamAV and PostgreSQL have small derived images with pinned upstream
bases. Their builds apply available distribution package fixes while preserving
upstream entrypoints and service configuration. Both ClamAV roles use the same
image. Initial installation and node update build these images alongside the
application; no host package is changed. The selected release namespace keeps
previous images available for rollback.

Package repositories change independently of source. Record the actual final
image IDs/digests, package inventory and advisory assessment for each qualified
build. A pinned base or unchanged source commit does not make later package
resolution byte-identical. Review image advisories before each release; do not
interpret antivirus or a successful synthetic workflow as proof that hostile
files are safe. Follow [storage and backup](STORAGE_AND_BACKUP.md) before updating
an existing node, and preserve its prior capsule and images until acceptance.

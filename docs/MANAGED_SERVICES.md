# What the installer manages

Exculpata provides one guided setup process. It builds and starts the
application, PostgreSQL, the HTTPS gateway, ClamAV and its signature updater,
and the selected review/transcription services as containers. Operators do not
need a separate host ClamAV installation for this stack.

Prepare the host using [the installation playbook](INSTALL.md): service account,
storage, Docker/Compose and, for GPU features, NVIDIA drivers and Container
Toolkit. Backups also require restic. The installer checks prerequisites; it
does not silently reconfigure the Docker daemon, replace drivers, open firewall
ports or change other applications. Review TLS and exposure before remote use.

Model weights are acquired separately. The installer displays the pinned
reference acquisition plan and terms, asks for acknowledgement, downloads the
selected artifacts, verifies their bytes and prepares offline inference. The
reference Nemotron diarizer does not require an access token. Operators can use
compatible models they procure themselves through the documented adapters and
[ASR import interface](ASR_MODEL_IMPORT.md). A different model architecture may
need an adapter; arbitrary model compatibility is not promised.

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

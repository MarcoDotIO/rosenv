# Security

rosenv is a development environment manager. Containers share the host kernel and rely on a trusted Docker daemon. Access to a Docker socket can confer host-level control. A writable workspace mount exposes its contents, including any credentials, to commands run inside the container.

Normal commands use the invoking user's UID/GID. Package operations use root inside the container. Resource ownership labels and Docker daemon identity checks prevent rosenv from treating unrelated resources as its own; they do not make hostile code safe to execute.

A project manifest, its tasks, Docker images, package repositories, and source workspace must be trusted before running them. Review changes to dependencies and tasks as you would review a build script. A lockfile records package versions and image identities; it is not a signature or a vulnerability assessment.

## Reporting

Report issues against the latest release or current default branch. There are no separately maintained older release branches.

Use the repository's **Security → Report a vulnerability** option if private reporting is available. Otherwise, open a minimal issue requesting a private reporting channel, without credentials, exploit details, or sensitive files. Ordinary correctness issues belong in the public issue tracker.

Never include `.env` files, Docker credentials, SSH keys, or live tokens in a report. Describe the impacted behavior and provide a sanitized reproduction. No response-time guarantee is offered.

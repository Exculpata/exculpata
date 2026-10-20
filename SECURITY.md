# Security policy

The [reference egress model](docs/NETWORK_EGRESS.md) describes app isolation,
the destination-limited OIDC proxy, its acceptance limits and the named
`model-stager`, `clamav-updater` and gateway exceptions. It is not a host firewall
or a claim that all containers are offline.

This prerelease has no public support window. Report suspected vulnerabilities
through GitHub private vulnerability reporting: open this repository's
**Security** tab and choose **Report a vulnerability**.
Include version, synthetic reproduction steps, and impact. Do not send
real evidence, transcript text, credentials, tokens, keytabs, private hostnames,
or internal network details.

Do not open a public issue for a suspected vulnerability or disclosure. If the
private reporting form is unavailable, contact the maintainer privately before
providing details. Public source availability is not production certification.

Do not deploy an unreviewed branch with confidential material. Keep the HTTPS
gateway private, use trusted certificates, stage models before confidential
work, and run AI workers offline.

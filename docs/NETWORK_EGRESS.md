# Reference network egress

The reference Compose app joins only internal networks (`services`, `models`
and `app-egress`). It has no published ports. Ordinary direct outbound TCP is
blocked by that topology, including clients that ignore proxy settings. The
gateway continues reaching the app and optional Kerberos proxy through
`services`; the proxy alone bridges `app-egress` to `front`.

## DNS boundary and supported runtime

An internal network flag is not sufficient evidence of DNS isolation. Docker's
embedded resolver (`127.0.0.11`) answers service names and can forward unknown
names upstream. [CVE-2024-29018](https://github.com/moby/moby/security/advisories/GHSA-mq39-4gv4-mvpx)
describes older Engines forwarding from internal networks through a host-loopback
resolver. Fixed Moby versions suppress that host-namespace forwarding. No leak
was observed on the previously tested Engine; its missing DNS proof was not a
demonstration of exfiltration.

The app, PostgreSQL, ClamAV parser, retrieval, generator and transcription API
explicitly set `dns: [127.0.0.1]` and `dns_search: ["."]`. On supported Docker
Engine user-defined networks, embedded service discovery remains at `127.0.0.11`;
the explicit upstream is the **container's own loopback**, with no resolver
listening there in the deployed services. This follows the advisory's explicit
upstream workaround and prevents fallback to host resolvers. It is not
`dns: [127.0.0.11]`, which would point the embedded resolver at itself. The search
setting prevents inherited host search suffixes. OIDC issuer resolution happens
in the constrained relay; its DNS settings are unchanged. Existing gateway,
model-stager, updater and Kerberos proxy DNS settings are unchanged.

The installer uses Engine version only as a nonblocking advisory. Known upstream
fixes include 23.0.11, 25.0.5 and 26.0.0; backports and unrecognized versions must
meet the same behavioral check. A new version cannot bypass that check. Preflight
checks configuration and reports the advisory; it cannot certify live isolation.

Startup, update acceptance and live doctor inspect each running restricted
service's exact DNS/search settings and internal-only memberships. From the actual
app, the diagnostic first requires positive internal service lookups over UDP and
TCP. It then briefly binds a bounded responder on **the app's own** loopback port
53 and generates six unpredictable names: A, AAAA and TXT over each transport.
Every query through Docker's embedded resolver must receive a positive response
and be observed by that local responder with the matching name, type and transport.
NXDOMAIN, refusal, timeout, an unavailable listener, or a positive response without
a local observation fails acceptance. The responder closes before the diagnostic
returns. Doctor therefore performs transient container-local network probes; it
does not change host DNS or daemon settings. Dry-run reports DNS unverified.

The live smoke separately proves the test recorder is reachable from the app with
six direct UDP/TCP controls. It recreates only its disposable app with that recorder
as the DNS upstream, requires a forwarded query to be observed, and requires the
behavioral check to reject it. Restoring production DNS must pass all six local
challenges without increasing that recorder's counter. This recorder is on an
internal test network, outside the app namespace; the counter is not a claim to
capture arbitrary daemon or Internet DNS. The causal local challenge provides the
production proof. Neither temporary responder is a deployed resolver.
Host-root/runtime compromise, additional network attachments, private overrides
and deliberately running a resolver in a restricted container are outside this
guarantee. Docker internal bridges can still reach appropriately bound services
on their host gateway. An operator must not expose a DNS forwarder or general
proxy there to these networks; this change is not a firewall for host services.
No host firewall or system DNS configuration is installed by the product.

## OIDC relay

The Authlib adapter uses the explicitly configured
`CASE_INTELLIGENCE_OIDC_PROXY` and disables ambient proxy discovery. Compose fixes
that setting and all upper/lowercase HTTP, HTTPS and catch-all proxy variables to
`egress-proxy:3128`; `NO_PROXY` is empty. Thus cleartext requests reach a denying
relay, and suffixes such as `x.postgres` cannot acquire a proxy exemption.
Internal companion clients use a separate transport that validates their existing
exact host allowlists, disables ambient proxies and rejects redirects. Hardcoded
loopback health probes also disable ambient proxies. The relay has no host ports,
volumes, application environment file or secrets.

The proxy permits CONNECT only to the configured issuer's exact host and port.
It rejects all other destinations, plain HTTP forwarding, malformed authorities,
userinfo, paths, body headers and conflicting Host headers. Noncanonical numeric
IPv4 authorities (octal, hexadecimal, shortened and integer forms) are rejected
before resolution, even when a resolver would interpret one as a public IP. DNS is resolved for
each connection; every answer must pass validation before any connection is
made, and the proxy dials that validated numeric address without resolving it
again. Empty issuer configuration starts in deny-all mode for local and Kerberos
sign-in. Requests, destinations and tunneled bytes are not logged. Concurrency,
header size/time, tunnel duration and transferred bytes are bounded by eight
connection slots, operation timeouts and byte limits. DNS and all connection
attempts share one five-second budget. DNS runs in a killable subprocess;
timeout kills/reaps it before releasing the slot. Header parsing checks a
five-second deadline between reads; relaying checks a 60-second deadline between
iterations. In-flight reads/writes retain five-second socket timeouts, so those
loop deadlines permit bounded overshoot rather than hard elapsed-time limits.
Tunnels carry at most 8 MiB.

This is a destination control, not TLS inspection: CONNECT can carry other
protocols or paths to the approved authority. The application still validates
the exact issuer, same-origin authorization/token/JWKS endpoints, signed tokens,
PKCE, nonce/state and groups. Discovery does **not** expand the allowlist.
Cross-origin endpoints remain unsupported, as before. Redirect following remains
disabled for real Authlib discovery, token and JWKS requests. Discovery metadata
is restricted to approved protocol fields before Authlib can merge it into client
options, so it cannot replace proxy, TLS, redirect, timeout, header or secret policy.
TLS verification, SNI and optional custom CA handling stay in the app;
the proxy never receives a CA file or client secret.

By default the relay accepts globally routable issuer addresses only. An operator
using a private issuer must explicitly save `CASE_INTELLIGENCE_OIDC_EGRESS_PINS`
in the selected app env file: a single-quoted JSON object containing `issuer`
(the full configured issuer URL, exactly) and `addresses` (1–16 distinct IP
address strings). Pins authorize only that issuer authority. Every resolved
address must be in the pin set; a changed issuer URL or mixed/unpinned DNS answer
fails closed. DNS is still required; pins do not replace name resolution or TLS
verification. Public unicast, RFC1918 and IPv6 ULA addresses can be pinned;
loopback, link-local/metadata, shared-address space, multicast, unspecified,
reserved and IPv6 transition destinations cannot. CIDRs and wildcards are not
accepted. Review exact issuer addresses privately before upgrading a private
issuer; no private access is inferred or automatically migrated. DNS rotation
requires an explicit reviewed pin update and recreation of the proxy and app.
Known ULA metadata endpoints documented by
[AWS](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/configuring-instance-metadata-options.html)
and [Google Cloud](https://cloud.google.com/compute/docs/metadata/overview#metadata_server_endpoints)
are explicitly denied both when pins are loaded and when DNS answers are checked.
Ordinary ULA pins remain supported. An IP allowlist cannot identify every
operator-specific metadata or administrative service; review the chosen issuer.

## Configuration and upgrades

Installer Compose invocations derive `OIDC_EGRESS_ISSUER` from the one selected
application env file's `CASE_INTELLIGENCE_OIDC_ISSUER`, only in `oidc` mode. Shell
overrides are discarded. This preserves fresh, legacy and explicitly selected
filenames without copying app configuration or secrets into the proxy. Only the
issuer and its optional pin JSON are passed. Changing
the saved issuer requires recreating the proxy and app together through the
installer. An existing proxy cannot observe a changed env file automatically.

The installer accepts single-line `KEY=value` entries with upper/lowercase keys,
spaces around `=`, unquoted, single-quoted or JSON-compatible double-quoted
values, and inline comments. An unquoted comment requires preceding whitespace;
quoted comments follow the closing quote. Auth mode uses `strip().lower()`, as
the app does. Duplicate keys, multiline values, unset keys without `=`, `export`
prefixes, colon delimiters and unsupported quoted escapes fail with a line-only
diagnostic. Auth mode, issuer and pin values containing `$` are deliberately
rejected: write resolved literals instead of shell/Compose interpolation before
upgrading. Keep secrets in their existing files; diagnostics never print values.
Other env entries are not copied to the proxy. This is a supported subset of
[Compose env-file syntax](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/#env-file-syntax),
not a second interpolation engine.

For operator-managed direct Compose invocation, supply `OIDC_EGRESS_ISSUER`
with the same value as the selected application's issuer, and `OIDC_EGRESS_PINS`
with the saved pin JSON if present, when creating the
services; otherwise the relay denies all. Merely setting an issuer in root
`.env` does not configure application authentication. Review the rendered graph
privately. Existing nodes must build the new proxy image and recreate the app
and other restricted services to receive the DNS configuration
to gain isolation; a source checkout or passing static check does not apply it.
Install and update include `egress-proxy` in their explicit image-build targets.
It shares the application's pinned Python base; its incremental image layers
remain unmeasured in the existing advisory evaluation-capacity estimate until
controlled build acceptance. Model-selection build targets are unchanged.

## Named exceptions and limits

* `gateway` remains on `front` for ingress, with the existing single loopback
  HTTPS publication, and on `services`. Its normal bridge attachment has egress;
  this change does not implement a gateway outbound firewall.
* `model-stager` remains an operator-invoked `tools` service on `front`. It reads
  the source checkout and writes the model cache; it has no case-storage or
  application-secret mount. Model hub/CDN downloads are not confined by this
  OIDC proxy. Model runtime networks/offline flags remain unchanged.
* `clamav-updater` remains on `updates` and writes only the shared signature
  volume. It needs signature update egress; `clamav`, which parses evidence,
  remains internal. The updater has no uploaded-evidence mount.
* Host package/image acquisition, Docker control-plane behavior, host-root
  access and private deployment overlays are outside the proxy's boundary.

An operator may separately review a host DOCKER-USER/nftables policy for their
Docker version, IPv4/IPv6 routing, DNS and organizational identity dependencies.
No host rule is installed or changed here. The repository check does not audit
external overlays or prove a host firewall; retain private overlay review and
controlled live acceptance before treating a node as isolated.

## Repository contract and evidence

`make network-contract` runs `scripts/network-contract.py`; `make check`,
`make check-fast` and the full Quality gates application job include it. The
reviewed `config/egress-allowlist.toml` records Compose network definitions,
per-service memberships, published ports, mounts, proxy-routing environment and
network-escape settings,
plus exact
Python networking expressions and literal URL/DNS-name occurrences. New Compose files,
implicit/default networks, host/service network modes, capabilities and changed
ports require review. Duplicate YAML keys and unparseable sources fail closed.
Service volumes (short and long syntax), `volumes_from`, `use_api_socket` and
named-volume definitions are inventoried because Docker/container-runtime sockets
and host binds can defeat container isolation, even when mounted read-only.
The inventory detects changed mount expressions, external volumes and driver
options; it cannot prove what an operator variable resolves to, follow a host
symlink, or inspect the contents of an approved mount. Installer path checks and
private rendered-graph/host review remain necessary.
Source findings include file/line reasons. No whole source file is exempted.

The AST inventory follows ordinary imports, aliases, client construction and
sync/async with-bindings. It includes inert XML/provenance URLs and DNS-shaped
filenames as reviewed occurrences; these are not permission to contact hosts.
It is not whole-program analysis: reflection, generated code,
third-party networking, other languages and subprocess utilities still require
review. Editing both an allowance and its code is a review event, not proof that
the new destination is safe.

`tests/test_reference_network_boundary.py` extends the existing topology contract
with synthetic mutations and HTTPX environment checks. `tests/test_egress_proxy.py`
checks denial, address validation/rebinding and a bidirectional socket-pair smoke.
Socket pairs do not prove container routing or a fresh install. Live Compose
positive/negative isolation smoke, browser journeys and security/privacy review
remain acceptance requirements; unexecuted checks must be reported as such.

The opt-in `scripts/test-egress-smoke.py --docker-context CONTEXT --output DIR`
builds the actual production application and relay targets. It derives their
network memberships, DNS, proxy environment, user and hardening from `compose.yaml`.
Fixture changes remove app data mounts, env files and dependencies, replace startup
with a probe process, and add three bounded synthetic endpoints. Five containers
use the production app/relay networks; none publishes ports or mounts host data.
A directly reachable internal canary makes cleartext/suffix denial assertions
independent of routing failure. The actual Authlib adapter performs signed-token
success, missing-CA denial, same-authority discovery/token/JWKS redirect denial,
and malicious-metadata transport checks. Each redirect stage must be observed,
and the foreign TLS endpoint must answer from the relay network before its app
denials count. Numeric direct TCP/HTTPX denials, causal
DNS challenges and the known-forwarding negative control are independent checks.
The harness cleans up only its unique project and images. The caller must supply
an approved disposable runtime and remove it after retaining evidence.

This exercises production-derived app/relay services, not a full installed node,
external identity provider or browser sign-in. Fresh installation and doctor remain
separate acceptance. Empty-issuer testing proves healthy relay denial, not complete
local-account or Kerberos sign-in. External IdP, Kerberos and installed browser
authentication checks must be reported separately when deferred.
The separate `Network isolation smoke` workflow runs for PRs carrying the
`network-smoke` label (including label events), merge groups, a weekly Monday
06:43 UTC schedule, or explicit manual dispatch. Scheduled runs use the default
branch, so the new schedule takes effect only after this workflow is merged.
It uses a disposable hosted Linux runner and publishes only its content-free
summary. The six existing required quality checks are unchanged. A skipped
smoke job is not a live test pass.

This implements the repository enforcement work tracked by
[#319](https://github.com/Exculpata/exculpata/issues/319). The separate architecture
contract in #305 should link here after coordination; its draft branch is not
modified by this change.

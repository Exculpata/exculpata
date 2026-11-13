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

The installer requires **Docker Engine 26.0.0 or newer AND live DNS verification**.
Preflight blocks older, prerelease, malformed, unknown or unavailable Engine versions
before installation/update writes. Recognized versions have three numeric components
and may include `+` build metadata. Older-engine backports are not a supported
exception, even if they include an upstream fix or pass the live check. A supported
version cannot bypass the live check either. Preflight is offline with respect to
application traffic and cannot certify live isolation; startup/update acceptance
and doctor must independently observe the runtime behavior below.

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

Failed updates can roll back to a capsule that predates this network contract.
The Docker 26.0.0 floor still applies. Only that rollback path may accept the prior
release's application, storage, authentication and selected-capability health without the new DNS proof. It
explicitly warns that DNS egress isolation is **not verified**; it does not print
a DNS success. New capsules declare network contract version 1 in their manifest.
Earlier unmarked egress capsules are recognized by their relay, inventory, DNS
helper or Compose markers and still require strict DNS proof during rollback.
Unknown or invalid declared contracts fail closed. Normal install, update and
doctor acceptance never take this legacy rollback exception.

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

## OIDC and generator relay

The Authlib adapter uses the explicitly configured
`CASE_INTELLIGENCE_OIDC_PROXY` and disables ambient proxy discovery. Compose fixes
that setting and all upper/lowercase HTTP, HTTPS and catch-all proxy variables to
`egress-proxy:3128`; `NO_PROXY` is empty. Thus cleartext requests reach a denying
relay, and suffixes such as `x.postgres` cannot acquire a proxy exemption.
Internal companion clients use a separate transport that validates their existing
exact host allowlists, disables ambient proxies and rejects redirects. Hardcoded
loopback health probes also disable ambient proxies. The relay has no host ports,
volumes, application environment file or secrets.

The proxy permits CONNECT only to the configured issuer's exact host and port,
and one separately configured external generator authority. The generator slot
uses `GENERATOR_EGRESS_URL` and `GENERATOR_EGRESS_PINS`, derived from the selected
app env file by the installer. Empty generator configuration denies that slot.
Both authorities use the same parser, DNS validation, pin and address-denial rules.
A shared authority with conflicting pin sets fails startup rather than broadening
either slot. Generator pins use the existing `issuer` JSON field bound to the full
generator URL. Only reviewed RFC1918/ULA pins allow private generator addresses;
loopback and metadata destinations cannot be pinned. See
[model configuration](MODEL_CONFIGURATION.md#existing-answer-endpoints).

The app's `CASE_INTELLIGENCE_GENERATOR_PROXY` selects the CONNECT relay explicitly;
its transport cannot be bypassed with `NO_PROXY`. The bundled generator continues
to use the exact-allowlisted internal companion route. External generator HTTP
URLs and `$` interpolation are rejected at configuration boundaries. Host loopback
answer servers are unreachable from the containerized app; no host-gateway route
is installed. The host-only doctor transport does not alter application routing.

Outer configuration whitespace and trailing slashes are trimmed consistently
with application settings, including issuer-bound pin matching. Line controls
remain invalid. Installer preflight rejects noncanonical numeric issuer hosts
before writing configuration, matching the application and relay. Structural URL
checks also reject trailing-dot/double-dot hosts, backslashes, embedded controls
or spaces, unsupported ASCII host characters, and invalid/zero ports before writes.
Unicode/ASCII-IDNA names, literal underscores, encoded path characters and valid
HTTPS ports retain their existing treatment. Literal URLs also reject mapped IPv6, 6to4, Teredo, metadata and other
non-pinnable addresses. Ordinary RFC1918 and ULA literals retain explicit-pin
support. DNS, certificate and provider metadata acceptance remain runtime checks.
Existing ASCII issuer names containing underscores retain literal, exact-host
matching for compatibility with existing private issuers accepted by HTTPX. This
is an ASCII-only compatibility rule, not permission to normalize underscores
into other characters; Unicode names must pass IDNA2008. Replacing an underscore
or adding a suffix does not gain access.
Installer, application settings and relay use the same offline issuer/pin policy.
A pinned, hash-verified source archive supplies IDNA2008 without host package
installation or a preflight network request; its license and reproduction manifest
ship with the source. Extra sibling modules fail closed; relative imports are loaded
only from the verified in-memory archive, including deferred IDNA dependencies.
A finder in the trusted parent package intercepts only this private package's
normal imports and reloads. It compares this interpreter's cache payload hashes against freshly compiled
trusted loader source, rejects unknown/symlinked cache artifacts, and
executes source bytes without ever executing a cache payload. Timestamp, size
and source-hash cache headers cannot authorize different code. Relative spellings
(including leading `./`) and `compileall` destination/prefix filenames are preserved
as traceback metadata. Bounded string candidates are read without deserializing
untrusted objects; each candidate must reproduce the complete trusted-source payload
hash. Filename strings are limited to 16 KiB and 128 distinct candidates per cache;
the cache read is bounded by the canonical payload size plus that filename allowance.
Canonical loader caches tagged for another CPython version are inert: they are
not read or executed by this interpreter, and that version validates them against
its own compiled source if it later uses the checkout. Other Python imports are unchanged.

This IDNA integrity boundary assumes a **root-owned, read-only installed package**
and **does not protect parent-package bytecode**. In particular,
`case_intelligence/__pycache__/__init__*.pyc` can run before the private IDNA finder
is installed. A writable parent package, interpreter, launcher or trusted source
can therefore defeat the boundary. No additional import hook can establish trust
in code that already ran before it. The regression suite deliberately demonstrates
execution of a harmless synthetic parent-cache payload; it does not claim to block it.

Live `doctor`, install completion and update acceptance check the running app
container, then stream a metadata-only probe
as its normal non-root user. The probe uses `python -I -S -B` without importing
`case_intelligence`, compares its effective UID/GID/groups with the app process,
and checks the effective root mount's read-only flag. It checks the installed
venv package, its ancestors and package entries for root ownership, read-only
mounts and effective app-user write access, including nested mounts and ACL-aware
access checks. Group/world-writable mode bits are independently rejected on the
package, every ancestor and every entry, even if the read-only mount or current
app identity masks those permissions. Root-owner write bits alone are permitted
only when all effective protection checks pass. Symlinks, missing/inaccessible metadata, unexpected identities and
unverified results fail closed with a bounded diagnostic. Compose's `user` and
`read_only` declarations alone are not proof; a declared read-only container must
also pass the live probe. Install and update run it after startup and before health
acceptance can report completion. A failed update check enters the existing rollback
path; restored-release health/Docker/DNS acceptance remains unchanged.
Rollback does not re-check the restored package. A successful rollback therefore
proves neither its package ownership nor its mode, mount or effective write-access
protection; run live doctor against the restored node to verify those properties.
An empty successful running-app inventory reports that the app is not running
and package protection was not checked. Failed or malformed inventory still
reports unverified package trust; it is not evidence that the app is stopped.
Prepare-only
and dry-run operations do not run the live probe or establish package protection.
Dry-run doctor explicitly leaves this check unverified.

Doctor does not write probe files, change ownership/permissions, repair a node,
or attest an already compromised process, interpreter, image or privileged host.
The trusted operator must rebuild/recreate an unprotected installation from trusted
inputs before retrying doctor. These checks supplement the independent Docker
version and live DNS gates; they do not replace either gate or installed sign-in
acceptance.
Invalid Unicode names fail before configuration writes.
Unicode issuer hostnames use the same IDNA2008 ASCII encoding as HTTPX; this
does not authorize similar-looking names or change the issuer string used for
token/discovery validation or explicit pin binding. The relay's IDNA dependency
is version- and hash-locked to the application's version. This prevents the standard
library's older IDNA mapping from silently changing the permitted authority.

The relay builds from the repository root to share that offline policy. Its
`deploy/egress-proxy/Dockerfile.dockerignore` is the active Dockerfile-specific
ignore file: it excludes everything except the relay code, shared policy and
explicit licensed IDNA files. Runtime/configuration data, credentials, caches and
unrelated application sources stay outside the relay build context. The name is
intentional. Legacy Docker builders use the root `.dockerignore`, which separately
excludes all unreviewed IDNA siblings; the Dockerfile copies only exact reviewed
IDNA files. The native smoke exports a never-started scratch image to check actual
legacy and BuildKit context contents, with a failing baseline negative control.
That control vendors the exact historical root ignore file and runs from a
synthetic source archive without Git history or an old-commit fetch.
A `.dockerignore` inside the relay directory alone would not filter
the repository-root context. See [Docker's context rules](https://docs.docker.com/build/concepts/context/#dockerignore-files).

The relay rejects all other destinations, plain HTTP forwarding, malformed authorities,
userinfo, paths, body headers and conflicting Host headers. Noncanonical numeric
IPv4 authorities (octal, hexadecimal, shortened and integer forms) are rejected
before resolution, even when a resolver would interpret one as a public IP. DNS is resolved for
each connection; every answer must pass validation before any connection is
made, and the proxy dials that validated numeric address without resolving it
again. Empty issuer configuration denies the issuer slot for local and Kerberos
sign-in while leaving a valid configured generator slot working. Startup validates
every configured destination before listening. An invalid generator URL or pins
setting stops the whole proxy, including OIDC egress, with exit code 1 and an error
naming the generator settings without printing their values. Requests, destinations
and tunneled bytes are not logged. Concurrency,
header size/time, tunnel duration and transferred bytes are bounded by role
slots, operation timeouts and byte limits. The shared constants in
`src/case_intelligence/oidc_network_policy.py` apply in both images:

| Role | Concurrent tunnels | Duration | Total bytes in both directions |
| --- | --- | --- | --- |
| OIDC | 2 reserved | 60 seconds | 8 MiB |
| GENERATOR | 2 | 310 seconds | 32 MiB |
| EMBEDDING | 4 | 310 seconds | 32 MiB |
| RERANK | 4 | 310 seconds | 32 MiB |

Role slots are acquired after CONNECT validation and before DNS, and released
on completion or failure. At most 16 connections, including header parsing,
are admitted. The separate pre-header budget (`RELAY_HEADER_CONNECTIONS`)
covers a burst spanning all twelve role slots and shares the global cap of 16;
`RELAY_ADMISSION_HEADROOM` remains four. A connection reserves that budget before
starting a worker and releases it when it acquires a role slot, or on failure.
Incomplete headers never acquire role slots. Busy model tunnels cannot use
the two OIDC slots. Exhausted admission or role caps
return `HTTP/1.1 503 Service Unavailable` with `Content-Length: 0`, then close;
rejection responses have a 0.1-second write timeout. Headers must finish within
one ten-second `RELAY_HEADER_SECONDS` budget, including blocked reads; arriving
bytes do not reset it. Expiry returns `HTTP/1.1 408 Request Timeout` with
`Content-Length: 0`, closes the connection and releases admission. A full
pre-header budget occupied by slow clients also rejects valid CONNECTs promptly
with 503. It does not queue requests behind busy model tunnels.
Embedding and rerank each admit three concurrent requests with one additional
slot, without increasing total admission or using the reserved OIDC slots.

CONNECT identifies an authority, not the purpose of the encrypted request.
An authority configured for both OIDC and a model therefore fails startup.
Model roles sharing an authority and identical pins share the first configured
role's pool in GENERATOR, EMBEDDING, RERANK order; conflicting pins still fail.

DNS and all connection
attempts share one five-second budget. Each address receives an equal share of
the remaining budget for the remaining validated addresses, so a stalled first
endpoint leaves time for fallback addresses. DNS runs in a killable subprocess;
timeout kills/reaps it before releasing the slot. Header parsing checks a
five-second deadline between reads; relaying checks its role's deadline between
iterations. In-flight reads/writes retain five-second socket timeouts, so those
loop deadlines permit bounded overshoot rather than hard elapsed-time limits.
The model limit includes ten seconds beyond the app's maximum 300-second
generator timeout and covers the installer's 180-second default. Run
`python -m case_intelligence.generator_doctor` in the existing app container
to check its effective `CASE_INTELLIGENCE_GENERATOR_TIMEOUT` before a schema
probe. A value above the shared generator limit, or an invalid/non-finite value,
returns an explicit configuration message and exits 1.
The doctor rejects timeouts above 310 s or at or below 0, stricter than the app's 1 to 300 s clamp, and fails closed.
The installer doctor also reports this timeout detail and fails for the bundled generator.
The in-app diagnostic is distinct from the installer's external schema probe. Recreate both images
after changing shared limits so the doctor and relay use the same version.

This is a destination control, not TLS inspection: CONNECT can carry other
protocols or paths to the approved authority. The application still validates
the exact issuer, same-origin authorization/token/JWKS endpoints, signed tokens,
PKCE, nonce/state and groups. Discovery does **not** expand the allowlist.
Cross-origin endpoints remain unsupported, as before. Redirect following remains
disabled for real Authlib discovery, token and JWKS requests. Discovery metadata
is restricted to approved protocol fields before Authlib can merge it into client
options, so it cannot replace proxy, TLS, redirect, timeout, header or secret policy.
Initial discovery and sanitization are serialized: a delayed concurrent response
cannot restore raw transport options after validation. Failed or cancelled loads
release the lock. Endpoint origins use HTTPX's lowercased ASCII wire host (including
case-insensitive IPv6 hexadecimal), so Unicode and
its IDNA spelling agree while distinct names that Unicode casefold conflates do
not gain access. The live fixture also exercises concurrent malicious metadata.
TLS verification, SNI and optional custom CA handling stay in the app;
the proxy never receives a CA file or client secret.

By default the relay accepts globally routable issuer addresses only. An operator
using a private issuer must supply reviewed exact pins explicitly. Fresh installation
accepts `--oidc-egress-pins` as one-line JSON and validates it before writes, then
saves `CASE_INTELLIGENCE_OIDC_EGRESS_PINS` in the selected app env file. Existing
nodes retain that setting; resume/update never replace it from command-line defaults.
The value is a single-quoted JSON object in the env file containing `issuer`
(the full issuer URL after the same outer-whitespace/trailing-slash trimming as
application settings) and `addresses` (1–16 distinct IP
address strings). Pins authorize only that issuer authority. Every resolved
address must be in the pin set; a changed issuer URL or mixed/unpinned DNS answer
fails closed. Retained pin JSON is validated by the same parser during blocking
resume/update provider preflight, before release staging or configuration changes.
Malformed, duplicate-key, empty, mismatched and unsafe-address pins fail closed.
Private literal issuers without matching pins fail fresh preflight; a literal
issuer with nonmatching explicit pins also fails. Hostname preflight remains
offline and does not resolve or automatically pin any address. Operators must
review private hostname destinations and supply their pins before installation;
DNS and actual provider sign-in still need deployment validation.
Fresh and retained provider preflight also apply the reference adapter's existing
resolved-literal rule, so interpolation syntax is rejected before writes.
DNS is still required; pins do not replace name resolution or TLS
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
issuer, external generator URL and their optional pin JSON are passed. Changing
either saved URL requires recreating the proxy and app together through the
installer. An existing proxy cannot observe a changed env file automatically.

The installer accepts single-line `KEY=value` entries with upper/lowercase keys,
spaces around `=`, unquoted, single-quoted or JSON-compatible double-quoted
values, and inline comments. An unquoted comment requires preceding whitespace;
quoted comments follow the closing quote. Auth mode uses `strip().lower()`, as
the app does. Duplicate keys, multiline values, unset keys without `=`, `export`
prefixes, colon delimiters and unsupported quoted escapes fail with a line-only
diagnostic. Auth mode, issuer, pins and generator values containing `$` are deliberately
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

### Generator isolation evidence

Exculpata is local-first by default. The generator runs as the model-file owner,
with a read-only root, all capabilities dropped, no-new-privileges and Docker's
default seccomp profile. No custom profile or host sysctl change is applied.
The network smoke runs a fixture image with the generator's network, DNS, user,
root and temporary-filesystem restrictions. It checks default routes, numeric
TCP destinations, UDP DNS and external name resolution, with a reachable named
peer as a positive control. Its receipt is `summary.json`'s `generator_lockdown`.
A passing probe shows the generator container had no route out in this topology
on this host when it ran. The app can still reach approved proxy destinations,
and operators can configure an external answer server.
`model-stager` is on `front` and writes the model root that the generator mounts
read-only; this path predates this change and is by design.
The network contract checks `compose.yaml` only, so a Compose override that puts
the generator on `front` would bypass the probe's evidence; operators must not do that.

The opt-in `tests/test_generator_lockdown_container.py` uses the already staged
pinned image with `EXCULPATA_GENERATOR_CONTAINER_TEST=1` and no image pull or GPU.
Optionally set `EXCULPATA_GENERATOR_CONTAINER_TEST_SUBNET` to a strict IPv4 CIDR
to create the internal test network with an explicit subnet, or set
`EXCULPATA_GENERATOR_CONTAINER_TEST_NETWORK` to reuse an existing empty internal
network. Reused networks are inspected first and never removed by the test.
Setting both fails; an invalid or IPv6 subnet skips the test. With neither set,
the default commands, arguments, order and cleanup are unchanged.
It also checks capabilities, seccomp, denied user-namespace creation, writable
caches, native loading restrictions and importing vLLM. This is distinct from
GPU acceptance. Merge requires the L3 L40S receipt: healthy generator startup,
one answer through the app, healthy restart with empty caches, inspected user,
read-only root, dropped capabilities and security options, and peak cache usage
for both executable tmpfs directories. No fallback setting is approved here.

## Repository contract and evidence

`make network-contract` runs `scripts/network-contract.py`; `make check`,
`make check-fast` and the full Quality gates application job include it. The
reviewed `config/egress-allowlist.toml` records Compose network definitions,
per-service memberships, published ports, mounts, environment-file sources,
proxy-routing and issuer/pin environment, and namespace/escape settings, plus exact first-party Python networking expressions and literal URL/DNS-name occurrences
under `src/`, `services/*/src/`, `deploy/` and `scripts/` (including nested
operational scripts). New Compose files,
implicit/default networks, host/service network modes, capabilities and changed
ports require review. Duplicate YAML keys and unparseable sources fail closed.
Issuer or pin removal, replacement or rewiring requires review, as do `pid`,
`ipc`, `userns_mode`, `uts`, `cgroup` and `isolation` changes. Mutation tests
exercise these selectors as data only; they never join host namespaces.
Service volumes (short and long syntax), `volumes_from`, `use_api_socket` and
named-volume definitions are inventoried because Docker/container-runtime sockets
and host binds can defeat container isolation, even when mounted read-only.
The inventory detects changed mount expressions, external volumes and driver
options; it cannot prove what an operator variable resolves to, follow a host
symlink, or inspect the contents of an approved mount. Installer path checks and
private rendered-graph/host review remain necessary.
Source findings include file/line reasons. No first-party Python source file in
those roots is exempted. Pinned third-party
internals remain dependency review material; the offline IDNA source archive has
its own hash/license manifest and is inspected by the publication archive gate.

The AST inventory follows ordinary imports, aliases, client construction and
sync/async with-bindings. It includes inert XML/provenance URLs as reviewed
occurrences; these are not permission to contact hosts. URL and IP detection
does not depend on file context. A bare hostname-shaped literal is reported
unless its last label is one of `json`, `jsonl`, `toml`, `yaml`, `yml`, `txt`,
`csv`, `tsv`, `md`, `py`, `pdf`, `log` and it has syntactic file context.
File context means a direct argument to a resolved file-safe call, a direct
operand of any `/` expression, or a direct value assigned to a name, attribute
or keyword ending in `path`, `file`, `filename`, `fname` or `dir` (case-insensitive).
The exact `FILE_SAFE_CALLS` identities are `pathlib.Path`, `pathlib.PurePath`,
`builtins.open` and `os.path.join`. Imports and aliases must bind those identities
in the lexical scope. Local rebinding, parameters, ambiguous imports and wildcard
imports cannot grant file safety. Bare `open` resolves to the builtin only when
unshadowed. Neither unimported `Path` nor arbitrary `.open` or `.join` is safe.
Assignment targets use `Name.id` or `Attribute.attr`; subscript and tuple targets
give no identifier. Container contents and annotations do not gain direct file
context. The existing networking/client resolver is separate and unchanged.

Host context wins before file context: assignment or keyword identifiers
containing `host`, `server`, `domain`, `proxy`, `proxies` or `url` retain the
finding. The ancestor walk then checks every enclosing call against the exact
four identities, continuing through safe calls so an outer unknown call wins.
It crosses calls, attributes, subscripts, binary/unary/boolean/comparison and
conditional expressions, starred arguments, keywords, f-string nodes, and
tuple/list/dict/set containers. It stops at an enclosing statement or `withitem`,
the first unknown call, or 32 ancestor hops. An unlisted node or exhausting the
cap before a boundary retains the finding. `with open("a.json") as f:` is a
supported file form. `frobnicate(Path("a.json"))` retains the finding without
requiring a host-call name list.

Unknown wrappers such as `json.load(open("a.json"))`,
`pd.read_csv(Path("a.csv"))`, `json.loads(Path("a.json").read_text())` and
`shutil.copy(Path("a.json"), dest)` deliberately retain findings and allowances.
The safe-call set must not grow to accommodate these accepted over-reports.

This is not call-reachability or taint analysis. Known gaps include bare
`host:port`, `host/path`, `//host`, concatenation, f-strings, neutral-name variable
indirection, browser instances obtained through `webbrowser.get()`, and
subprocess curl arguments. Dotted literals inside unknown calls remain reported;
these gaps concern forms the literal inventory cannot reconstruct or follow.
A host-shaped file literal assigned to a neutral name, such as
`from pathlib import Path; x = Path("evil.md"); requests.get(x)`, is not tracked
across statements. Imported `Path("evil.py")`, builtin `open("evil.md")` and
division with a non-Path operand count as files by design. Bare `evil.py` and
`evil.md` remain reported, as do file-context literals ending in `zip`, `mov`,
`sh`, `pl` or `cc`, which are outside the closed extension list.
This change concerns only the static AST inventory. Runtime proxy, smoke,
canary and isolation controls are unchanged and remain separate requirements.
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
with a probe process, and add three bounded synthetic endpoints. Fixture health
checks require the app identity module to load and each endpoint to answer its
loopback HTTP probe before Compose startup succeeds; the relay retains its
production health check. Five containers
use the production app/relay networks; none publishes ports or mounts host data.
A directly reachable internal canary makes cleartext/suffix denial assertions
independent of routing failure. The actual Authlib adapter performs signed-token
success, missing-CA denial, same-authority discovery/token/JWKS redirect denial,
and malicious-metadata transport checks. Each redirect stage must be observed,
and the foreign TLS endpoint must answer from the relay network before its app
denials count. The relay's observed IPv4 default route must select the existing
`front` gateway. Numeric direct TCP/HTTPX denials, causal
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

## Operator retrieval destinations

The relay also accepts `EMBEDDING_EGRESS_URL` / `EMBEDDING_EGRESS_PINS` and
`RERANK_EGRESS_URL` / `RERANK_EGRESS_PINS`. Installer Compose invocations derive
these from the selected app file's corresponding `CASE_INTELLIGENCE_` settings;
operator-managed Compose must supply them explicitly. Empty slots deny access.
Recreate app and relay together after changes. Only destination URLs and pins
reach the relay, never API keys or app secret mounts.

Embedding and rerank requests reuse the generator CONNECT transport and relay
policy: exact host/port, full DNS-answer validation, numeric dialing, reviewed
private pins, the same unconditional deny set, end-to-end TLS and no redirects.
Ambient `NO_PROXY` cannot bypass this path. Loopback is denied; LAN endpoints
need HTTPS, trusted certificates and reviewed RFC1918/ULA pins. Relay setup,
time and byte limits still apply. See [retrieval configuration](MODEL_CONFIGURATION.md#optional-embedding-and-rerank-endpoints).

## Filename inventory deletion measurement

Baseline: main `6b5ff8cab640b90d7ddb754305cf5db312a4796e`. The bare hostname
match at scanner lines 188-189 on that revision is the original root cause.
The r5 implementation starts from PR #488's `06bfebc` plus that main merge.

Generated using `Counter(main host records) - Counter(post-fix source_inventory)`:
1,151 original host records, 46 deleted occurrences across 40 keys, 1,105 retained
host records, zero new records. Surviving records and all non-host allowances are
unchanged from main. Unknown outer wrappers remain on the allowlist; the table
lists only measured deletions, not every filename-shaped occurrence.
Twenty deleted keys also have retained occurrences, so deletion proof uses
multiplicity rather than incorrectly asserting that those entire keys disappear.
The table's record number is the one-based main `source` array index, not a line
number or a runtime authorization. The filename test verifies the complete table
against suppressed occurrences from the real repository inventory.

| Main record | Path | Digest | Detail | Extension | Context |
| --- | --- | --- | --- | --- | --- |
| 15 | `services/transcription/src/transcription_v2/api.py` | `d405dbb5661255de4b2ff0cb7d86fe2f725b45bad77dd19c460a7811e6dc584e` | `ui-summary.json` | json | division |
| 34 | `services/transcription/src/transcription_v2/pipeline.py` | `d8d0422389f03d783e32e627250fe29834bd09c6361640d1ff00661dd6820034` | `config.yaml` | yaml | division |
| 50 | `services/transcription/src/transcription_v2/resources.py` | `d8d0422389f03d783e32e627250fe29834bd09c6361640d1ff00661dd6820034` | `config.yaml` | yaml | division |
| 51 | `services/transcription/src/transcription_v2/review_exports.py` | `1c7fd51bfd8f7e79698965c3c0dd657698b249bcfd338f9003c60b01f166a7b6` | `machine-result.json` | json | division |
| 62 | `services/transcription/src/transcription_v2/worker.py` | `d8d0422389f03d783e32e627250fe29834bd09c6361640d1ff00661dd6820034` | `config.yaml` | yaml | division |
| 103 | `src/case_intelligence/identity.py` | `995293729fa4ef6b0ce94e462db8cbd3af78036b44f327e89adec5e89e936dc3` | `local-accounts.json` | json | division |
| 119 | `src/case_intelligence/matter_archive.py` | `dd36113a1ed2d9349341cfcfd4874562bdfc244f9b2dc89d09d89f48b8cabcd7` | `unavailable-references.jsonl` | jsonl | division |
| 120 | `src/case_intelligence/matter_archive.py` | `320073ecd31a5ee829055edb2f7393dcbec77f91cb05fd392714c16e28415b9d` | `sources.jsonl` | jsonl | division |
| 143 | `src/case_intelligence/pilot_uploads.py` | `ffa5b716b5a57837f7929dfcca4b4dfdeb97210a7fd5a12d2f1978846d6f1743` | `manifest.json` | json | division |
| 149 | `src/case_intelligence/question_draft_evaluation.py` | `76f65e507c8553d93c2dfec3417a6851f5de95206d0ff0de073ccf6de7b01c03` | `question-drafts-v1.json` | json | division |
| 150 | `src/case_intelligence/question_draft_evaluation.py` | `b546905e3eaa417cad3f64329ae02e69b2844d8551db177d092ef2d9a3a81276` | `models.json` | json | division |
| 303 | `scripts/assertion-storage-restore-drill.py` | `00cef86f5c6bd4b73bfe948f62db56d70ff5a509ebc7358f30fe0fe37106e05e` | `synthetic-seed.json` | json | division |
| 311 | `scripts/benchmark-time-to-briefing.py` | `2dfe744f64b97238df644a0ef96b7f245bb6ffc7519b726824c0753e6e3797c4` | `run.json` | json | division |
| 439 | `scripts/browser-accept-email-coverage.py` | `eaa3b668f38bd7c630e701879ab7b338c228812a63f61a47d726b56b6e73a290` | `damaged.pdf` | pdf | division |
| 538 | `scripts/browser-accept-intake-receipts.py` | `b48152b3ad1a85681c670025958b8b9ed69ec8d702cc66e70cae0a9d1425b152` | `resume.txt` | txt | division |
| 539 | `scripts/browser-accept-intake-receipts.py` | `8efad9c7de03296e3da4cd49fd8981a1e8ba4e162a20034f47df877be5a1f993` | `capacity-limited.txt` | txt | division |
| 550 | `scripts/browser-accept-loose-file-preflight.py` | `5ea8a2478572a4c9116a5880f19c07ce966314d5d781c901cfe4c74cc86b43c5` | `incident-notes.txt` | txt | division |
| 553 | `scripts/browser-accept-loose-file-preflight.py` | `eaa3b668f38bd7c630e701879ab7b338c228812a63f61a47d726b56b6e73a290` | `damaged.pdf` | pdf | division |
| 556 | `scripts/browser-accept-loose-file-preflight.py` | `a8ef9e8466d92c23d0a78c632ebc12931b9aa1af4cc6f1cc2e6016a9d366c29f` | `oversize.txt` | txt | division |
| 607 | `scripts/browser-accept-matter-rename.py` | `8ffce577845a5740a2d042617b49546a1c5140dae2f9e31e0c3b348fb3a0b61d` | `generated-source.txt` | txt | division |
| 631 | `scripts/browser-accept-notebook-conflicts.py` | `8ffce577845a5740a2d042617b49546a1c5140dae2f9e31e0c3b348fb3a0b61d` | `generated-source.txt` | txt | division |
| 654 | `scripts/browser-accept-report-conflicts.py` | `8ffce577845a5740a2d042617b49546a1c5140dae2f9e31e0c3b348fb3a0b61d` | `generated-source.txt` | txt | division |
| 667 | `scripts/browser-accept-reports-bundle.py` | `f6bd61c429eeb4f1fea36720e745eceba905e5e93a8b9a81919400e18bb0992f` | `generated-note.txt` | txt | division |
| 685 | `scripts/browser-accept-review-decision-conflicts.py` | `8ffce577845a5740a2d042617b49546a1c5140dae2f9e31e0c3b348fb3a0b61d` | `generated-source.txt` | txt | division |
| 734 | `scripts/browser-accept-upload-occurrences.py` | `eafb4aff19bc57c3106afe5e1d4aef0b6422e0de242745e6e94f7d5733ab1cc1` | `report.txt` | txt | division |
| 860 | `scripts/exculpata_backup.py` | `33b93bf6bfddc583596ad10e3d565a703ec0e978f19dcc9678e7020d6cbcaad9` | `installation.json` | json | division |
| 864 | `scripts/exculpata_backup.py` | `9dc9baa5e9719de650a9d436c49d6d93ca64ddc5f677e67499c60881a30a47df` | `backup-status.json` | json | division |
| 865 | `scripts/exculpata_backup.py` | `3df3c9e5a5a6f5e1c5fa735bdebe47732f6cea8a7a2d34b43728511cec01d973` | `backup.json` | json | division |
| 898 | `scripts/exculpata_install.py` | `33b93bf6bfddc583596ad10e3d565a703ec0e978f19dcc9678e7020d6cbcaad9` | `installation.json` | json | division |
| 899 | `scripts/exculpata_install.py` | `33b93bf6bfddc583596ad10e3d565a703ec0e978f19dcc9678e7020d6cbcaad9` | `installation.json` | json | division |
| 900 | `scripts/exculpata_install.py` | `33b93bf6bfddc583596ad10e3d565a703ec0e978f19dcc9678e7020d6cbcaad9` | `installation.json` | json | division |
| 901 | `scripts/exculpata_install.py` | `33b93bf6bfddc583596ad10e3d565a703ec0e978f19dcc9678e7020d6cbcaad9` | `installation.json` | json | division |
| 902 | `scripts/exculpata_install.py` | `33b93bf6bfddc583596ad10e3d565a703ec0e978f19dcc9678e7020d6cbcaad9` | `installation.json` | json | division |
| 903 | `scripts/exculpata_install.py` | `33b93bf6bfddc583596ad10e3d565a703ec0e978f19dcc9678e7020d6cbcaad9` | `installation.json` | json | division |
| 944 | `scripts/exculpata_install.py` | `fb362910d898a3711cde7aec3c8c281e7e464caec39a99d90fe50cd83009ed47` | `provisioned.json` | json | division |
| 945 | `scripts/exculpata_install.py` | `6573734df9fc41c1c129c990d7c179058c7e416da2c071afdeffb3f92baf2450` | `install-progress.json` | json | division |
| 949 | `scripts/exculpata_install.py` | `3df3c9e5a5a6f5e1c5fa735bdebe47732f6cea8a7a2d34b43728511cec01d973` | `backup.json` | json | division |
| 973 | `scripts/exculpata_install.py` | `9dc9baa5e9719de650a9d436c49d6d93ca64ddc5f677e67499c60881a30a47df` | `backup-status.json` | json | division |
| 983 | `scripts/full-text-synthesis-restore-drill.py` | `ade1fbce1069898f2b7c1e1c092f980a10507f2918bee33dfaae0ab0bcbebb86` | `seed.json` | json | division |
| 1182 | `scripts/run-browser-acceptance.py` | `834aa60b7b3085c08406d4bbe0f2d6c5811ca9ad1f74666deef53da29f614842` | `journey.log` | log | division |
| 1191 | `scripts/stage-models.py` | `36ef972f897f6ac412726601658c79d81880a84fda4b82f603f89dace9431d3e` | `approved-model-manifest.json` | json | division |
| 1192 | `scripts/stage-models.py` | `36ef972f897f6ac412726601658c79d81880a84fda4b82f603f89dace9431d3e` | `approved-model-manifest.json` | json | division |
| 1199 | `scripts/test-egress-smoke.py` | `76543611dccc2cd0832fc6eb344b9cbfb3d6dc00aa7babae7409f07d62ddf24a` | `compose.json` | json | division |
| 1249 | `scripts/verify-upload-occurrence-rollback.py` | `afb0cb5531d1272db535b04969ca6bac54e172bac65c2f2de2e92b94792d8edc` | `expected.json` | json | division |
| 1261 | `scripts/run-browser-acceptance.py` | `482b31b2820c4cac4c563e4d13f4d3a4431b5e70623f07c28c4856a3dce3b908` | `navigation-timeouts.json` | json | division |
| 1352 | `scripts/defense-eval-ci.py` | `c7bc716846e7756cc4fcd87f70ef5d8b78a49ee24d100510dc872f105635e459` | `provenance.json` | json | division |

Additional local parity occurrences introduced after the baseline are numbered
separately below. These Path divisions read development requirements, read the
expected-skip data, and write the synthetic event file, respectively. They do not
name network destinations; the same exact multiset test verifies their exclusion.

| Parity occurrence | Path | Digest | Detail | Extension | Context |
| --- | --- | --- | --- | --- | --- |
| 1 | `scripts/ci_parity_environment.py` | `50c86b7ed8ac2cf95bd48334961bf0530cdc77b5a56f852c5c61b89d735fd711` | `pyproject.toml` | toml | division |
| 2 | `scripts/ci_parity_plan.py` | `c53b3d28d865bd84a336bece60274b6a01fc4a899cf0514ba42a7112d8b1671c` | `ci-parity-expected-skips.toml` | toml | division |
| 3 | `scripts/ci_parity_runner.py` | `6ffa4ae6c03880f53aab1689dbb5bf3aaf3d2cbbfb3a8004366c88e8b668bcee` | `event.json` | json | division |

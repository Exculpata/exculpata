# Installation handoff and team setup

The installer ends with the configured HTTPS sign-in URL, authentication mode,
initial local administrator username when recorded, observed preparation and
runtime states, and the next incomplete step. It never treats a prepared node
as a successful browser sign-in. Passwords and tokens are not included in phase
receipts or the handoff.

## CPU practice matter

The installer keeps the saved application env file on upgrade. An explicit
`EXCULPATA_APP_ENV_FILE` in `compose.env` wins (the previous-prefix alias is
also accepted). Otherwise it selects `exculpata.env` if present in the saved
config root, then `recordbench.env`. The missing new-name file does not require
renaming or copying an existing configuration. Validation, diagnostics, updates
and rollback use that same selected file; custom selections must be filenames
inside the config root. Keeping both filenames does not migrate the node’s
saved naming generation; conflicting storage markers or installation metadata
still block the operation. Fresh installs still write
`exculpata.env`. Direct `docker compose` cannot detect filenames: set
`EXCULPATA_APP_ENV_FILE` to the intended filename when both files exist or when
using a new-name file; its unset default remains `recordbench.env`.

Start with the [Ubuntu CPU node quick start](INSTALL.md#cpu-node-quick-start-ubuntu-2404)
and a successful `./install doctor --root /srv/exculpata`. This first journey
uses just the installer-created administrator and one synthetic text file.
Team setup, additional accounts and adoption checks come later.
Legacy nodes retain `/srv/recordbench`, the `recordbench` service account, and
`recordbench.admin` (or their saved custom paths and administrator).

1. **Reach loopback HTTPS.** On the node itself, use
   `https://localhost:8443`. For a remote node, run this on your browser computer
   using your ordinary SSH operator account (replace the example destination):

   ```console
   ssh -N -L 127.0.0.1:8443:127.0.0.1:8443 operator@node.example.test
   ```

   Keep that tunnel open while using the browser. Port 8443 must be free on the
   browser computer. Keep the node's listener on loopback; do not open a firewall
   port or change the bind address for this exercise.

2. **Trust this practice node's certificate before signing in.** The installer
   generated `/srv/exculpata/tls/tls.crt` for `localhost`. As the service
   account, inspect its fingerprint:

   ```console
   openssl x509 -in /srv/exculpata/tls/tls.crt -noout -sha256 -fingerprint
   ```

   In another terminal as the ordinary operator on the node, export only that
   public certificate to the operator's home, outside the checkout:

   ```console
   sudo -u exculpata cat /srv/exculpata/tls/tls.crt > "$HOME/loopback.crt"
   ```

   If browsing remotely, copy it on the browser computer through the same
   authenticated SSH connection:

   ```console
   scp operator@node.example.test:loopback.crt "$HOME/loopback.crt"
   openssl x509 -in "$HOME/loopback.crt" -noout -sha256 -fingerprint
   ```

   Compare its fingerprint with the node's. Import it as a trusted
   authority in a disposable browser profile or dedicated test user's browser
   certificate store. Restart that browser and verify that
   `https://localhost:8443/auth/login` has no certificate warning. Keep the
   private `tls.key` on the node. Do not use an insecure-certificate browser
   switch or click through a warning. Remove this evaluation trust after the
   exercise; use organization-trusted TLS for staff access.

   For Chromium on a **dedicated Linux test user with a fresh certificate
   store**, the accepted route uses `libnss3-tools` on the browser computer.
   After saving the verified public certificate as `~/loopback.crt`:

   ```console
   sudo apt-get install --yes libnss3-tools
   mkdir -p "$HOME/.pki/nssdb"
   certutil -N -d "sql:$HOME/.pki/nssdb" --empty-password
   certutil -A -d "sql:$HOME/.pki/nssdb" -n synthetic-exculpata-loopback \
     -t 'C,,' -i "$HOME/loopback.crt"
   ```

   Do not initialize over an existing user's certificate database. Other
   browsers may have their own certificate store. When finished, remove this
   trust with `certutil -D -d "sql:$HOME/.pki/nssdb" -n synthetic-exculpata-loopback`.

3. **Sign in as the first administrator.** Use the username shown in the
   installer handoff (default `exculpata.admin`) and the password you entered
   during installation. This is an application account, separate from the
   `exculpata` Linux service account. No preview identity or seeded account
   is needed. An administrator with no matter arrives at **Team setup**.

4. **Create a practice matter.** Choose **Create matter**, name it
   `Synthetic CPU practice`, and review its end date before submitting. The
   default is 30 days of review plus seven days of export grace, after which
   enabled maintenance can delete it without another confirmation; see
   [temporary-matter retention](EXPIRED_MATTER_RECOVERY.md). You own the new matter.
   The other team-setup checklist items can remain incomplete for this one-user
   exercise.

5. **Add one synthetic source.** On the browser computer, save a UTF-8 text file
   named `synthetic-practice.txt` outside the checkout containing:

   ```text
   Synthetic practice only. No real people or case facts.
   The fictional Aurora team inspected the blue folder on 12 September 2026.
   ```

   Open **Sources**, choose **Upload sources**, enter the collection name
   `Synthetic practice sources`, then choose the file. Inspect the selection
   preview and select **Upload 1 ready file**. Wait for **Sources ready**, choose
   **Review uploaded sources**, then open `synthetic-practice.txt` and check
   that the text is visible. Reload and confirm the matter and source remain.
   With `--models none`, generated answers and transcription are unavailable;
   this source-review exercise does not need them. A failed upload or scanner
   check is an incomplete journey: use [startup troubleshooting](INSTALL.md#startup-troubleshooting)
   and retain the failure accurately, without disabling scanning.

6. **Record a dated receipt.** Include the public Ubuntu image/version,
   architecture, CPU/RAM/disk allocation, exact source revision, commands and
   wall times for host setup, install, doctor and browser practice. Record
   preflight/doctor exit status, successful first-admin sign-in, matter creation
   and source opening/reload. Note any retries or uncompleted steps. Use only
   content-free observations and synthetic names; omit host identifiers,
   credentials, certificates, cookies, runtime files and raw logs. A green
   doctor alone does not establish browser acceptance.

The wider team journey below is optional for this practice path. Code-only
contribution still uses [make bootstrap/check](../CONTRIBUTING.md#set-up-a-development-checkout).

## Continue an interrupted installation

Run `./install install --root /srv/exculpata --resume` using the same reviewed
release and service-account identity. Paths with spaces are quoted in the
printed command. Resume uses the saved configuration, account-directory profile,
initial administrator identity, model selection and storage boundary. Before any
Compose command, it rechecks the entered node path, all protected storage ancestors and saved mount coordinates, then restores the saved
authentication, TLS, model and GPU choices for preflight. Conflicting saved
coordinates or unsafe replacements stop without launching or stopping services.
It checks the Compose graph, validates existing storage and accounts through the
operator tools, and reuses existing accounts and secrets. It does not reset sources or
reinitialize an existing account file. A relocated canonical account directory
is read from the saved Compose mount and must pass the same protected-ancestry
checks; it does not need to move back under the node root.

For an interruption before initial administrator creation, provide the password
again through the existing protected terminal or stdin option. The password
is deliberately not saved for resume. Older nodes without recorded initial
administrator metadata still require explicit `--admin-username` and
`--admin-display-name` if account creation was never completed. An existing
valid account file does not require those bootstrap inputs.

Model staging writes a version 2 receipt binding the catalog digest, exact
selected groups, review profile, snapshot pathnames, file sizes and artifact
hashes. Each file symlink also retains its literal target and resolved blob path;
removing, repointing or replacing a snapshot link invalidates verification even
when the original blob still exists. Directory symlinks and artifacts outside the
approved cache are refused. Before resumed staging, `stage-models.py verify` checks
that receipt, all recorded bytes, and the complete current file inventory within
each selected snapshot and the staged tokenizer directory. Added configuration,
tokenizer files or links invalidate verification; unrelated cached revisions are
outside the selected inventory. Verification remains offline, without writing
files or accepting a token. A matching selection skips
staging and gated-token entry. Missing, changed or older version 1/unreceipted
selections run the normal pinned staging path. The model catalog, revisions and licenses
are unchanged by this feature. Hash verification establishes retained artifact
integrity; it does not replace runtime loading or representative evaluation.

Before resumed runtime checks, the current launcher's stdlib verifier checks the
cache offline against the installed release's exact catalog. An unattended
resume that needs gated diarization staging must select `--accept-model-terms`
and `--hf-token-stdin` before any Compose command or phase write. A verified
cached selection requires neither flag nor token entry. Container-side
verification still repeats before the retained cache is reused during provisioning.
Updates perform the saved-coordinate and ancestry checks before their backup or
Compose work; this feature does not change model revisions during an update.

## Read the states correctly

The owner-only `state/install-progress.json` receipt has `format_version: 1`,
the installed release identifier, fixed phase names/states and observation time.
It contains no passwords, tokens, source text or user identity. States are
`checking`, `complete`, `incomplete`, or `not-selected`.

| State | What was observed |
| --- | --- |
| Prepared | Runtime build and storage/account/model preparation finished. |
| Running | The application answered its health probe. |
| Login reachable | The bounded loopback sign-in endpoint answered; Kerberos also issued the expected protected gateway challenge. |
| Basic review | Health reported source review, storage and required malware scanning ready. |
| Selected capabilities | The health contract reported every selected optional capability ready. |
| Browser sign-in | Always left for a person to verify in the actual browser. |

Resume revalidates completed preparation; its old receipt is not a readiness
override. `--prepare-only` performs preparation without starting services. A
failed selected model remains incomplete while healthy basic review can still
be reported separately. `./install doctor --root /srv/exculpata` repeats the
live checks and prints the handoff. A failure retains node state and reports the
next unverified phase for recovery.

The local loopback probe does not validate the certificate trust store on the
operator's browser or a teammate's device. A generated smoke certificate remains
for isolated loopback testing. Arrange the matching hostname and trusted TLS
through the operator before team access; do not bypass browser protection.

Receipts are derived and are not a new account or source store. Existing nodes
without them are revalidated on resume. Unknown receipt formats fail closed and
require the matching installer. Normal configuration/control backups retain the
installation coordinates; a restore may reconstruct phase observations through
resume/doctor. The model-cache receipt is rebuildable with that cache. No database
migration or optimistic checklist completion field is introduced.

## First administrator in the browser

An administrator without an assigned matter opens **Team setup** after default
sign-in. Explicit destinations are retained. The same page remains in the
account menu and workspace health page for later visits.

1. Check actual basic and optional capability status.
2. Give a synthetic teammate a sign-in through **People** or the configured
   identity provider, then have them sign in once.
3. Create a synthetic practice matter or choose an existing matter whose case
   team you have joined.
4. Add synthetic sources through **Sources**, wait for preparation, and resolve
   reported failures. Counts come from the existing readiness projection.
5. Add the teammate from **Case team**. Admission, application administrator
   roles and matter membership remain separate.
6. Have the teammate open that matter in their own session. The page reports
   whether a current eligible teammate has a successful matter-open audit event
   after their latest membership grant. A same-time observation is insufficient. Revoking/regranting access requires a
   fresh opened page. Separately confirm that an unassigned synthetic account
   is denied and that the sources are usable.

Local, OIDC and Kerberos modes show their own identity guidance. Setup is an
authenticated administrator-only read path; it cannot create an initial
administrator or claim an installation. Progress survives restart because it
is derived from existing readiness, current memberships and audit evidence.
It is not a certification of source accuracy, model quality or negative access.

## Acceptance before adoption

Run the portable synthetic browser runner described in
[Browser account management](LOCAL_ACCOUNT_BROWSER.md). It now also checks the
setup entry point, narrow layout and recorded teammate-open evidence. Run the
installer interruption, account and onboarding regressions on the integration
checkout. Before adopting that revision, independently complete clean Linux
installation plus interrupted resume, actual source preparation, selected-model
runtime checks, two-user access, and an installed-node backup/clean-restore drill
with synthetic data on the intended host. A green PR or loopback browser check
does not establish those target-host outcomes.

A resumed dry-run can use the verified local model receipt to plan an offline
restart without requesting a staging token. Real provisioning still verifies the
selected model artifacts through its runtime image before launch.

If interruption occurred before local administrator creation, unattended resume
requires the saved administrator identity and `--password-stdin` before runtime
commands. Existing canonical account stores do not require password re-entry.

Preview and test identities are seeded when the application starts. Team setup
labels them as available synthetic identities without claiming that another
person has signed in. Configured local or organization identity flows retain the
observed-person wording; matter access still requires its independent event.

# Installed checks for alignment, translation and speaker labeling

## Kickoff

Give operators an independent check of each optional media API from the installed
app. The relay currently accepts the ASR destination alone. Each new role needs
its own exact HTTPS origin and reviewed pins so a successful ASR check cannot
stand in for another service. [Migration tracker](https://github.com/Exculpata/exculpata/issues/558)

Expected changes cover the Compose application and relay, the installer
environment and doctor, the shared relay policy, the packaged diagnostic, the
network guide, and synthetic configuration and contract tests.

Acceptance requires the installer to select only the saved destination and pins
for each role, the app to run the production adapter with fixed synthetic input,
and doctor to report each result independently. Invalid saved configuration
must fail closed while stop, logs and repair operations remain available.

The relay can enforce authority and resource limits. The application still owns
TLS, request bodies and matter grants for case traffic. Diagnostics read fixed
synthetic inputs and report content-free results. Cover an empty installation,
a rejected response, an unavailable relay, and role-specific routing.

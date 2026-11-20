# Check the four speech APIs locally

From a checkout with the contributor Python environment, run this command
against a disposable Docker context with Compose v2. First-time builds need
access to the pinned container images and application dependencies.

```sh
.venv/bin/python scripts/test-speech-apis.py --docker-context default
```

The command starts the installed application diagnostics, the production
CONNECT relay and one HTTPS fixture with four distinct service names. The app
and fixture use separate internal networks. The relay pins each service name to
the fixture container's address. The app trusts a certificate generated for
this run. The setup publishes no ports, mounts no matter files and cleans up
the temporary containers and networks on exit. To reuse a local application
image from the current checkout, pass `--app-image IMAGE`.

The first result must report `ok` for transcription, word alignment,
translation and speaker labeling. The command then stops the relay and checks
that each diagnostic reports the outage. A failing check exits nonzero. To
repeat after a failure, rerun the same command.

The provider accepts the application's packaged WAV and fixed synthetic text.
Its canned Spanish ASR transcript lets the job tests exercise English
translation independently from the source text. These checks cover transport,
request and response formats, and service failure. Speech quality requires a
separate evaluation using the operator's actual models. For a deployment,
configure and grant access to the operator-managed endpoints from the
[matter and model configuration guide](MODEL_CONFIGURATION.md).

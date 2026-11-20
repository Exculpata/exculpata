"""Verification-only seed of historical review bundle settings on an installed CPU fixture."""
from pathlib import Path
import json
import os
import sys
import tempfile

sys.path.insert(0, str(Path.cwd() / "scripts"))
import exculpata_install as installer


def seed(node):
    runner = Path(os.environ["RUNNER_TEMP"]).resolve()
    if node != runner / "synthetic-cpu-node":
        raise ValueError("Expected the workflow's disposable synthetic-cpu-node.")
    saved_path = node / "installation.json"
    saved = json.loads(saved_path.read_text())
    if saved["models"] != "none" or saved["profiles"] or saved["server_name"] != "localhost" or saved["auth"] != "local":
        raise ValueError("Expected the fresh, signed-in local verification node.")
    console = installer.Console(color=False)
    compose_path = node / "compose.env"
    app_path = installer._app_env(node)
    with tempfile.TemporaryDirectory(dir=runner) as donor_parent:
        donor = Path(donor_parent) / "historical-layout"
        args = installer._parser().parse_args([
            "install", "--root", str(donor), "--names", "exculpata", "--auth", "local",
            "--models", "review", "--non-interactive", "--enable-account-management",
            "--server-name", "localhost", "--bind-address", "127.0.0.1",
            "--admin-username", "synthetic.admin", "--admin-display-name", "Synthetic Reviewer",
        ])
        installer._collect_identity_choices(args)
        layout = installer._prepare_directories(console, donor, storage_root=None, resume=False, dry_run=False)
        devices = (installer.GpuDevice("0", "Synthetic GPU", 48000, 46000, 8.9),)
        gpu, review = installer._resolve_gpu_plans(args, "review", devices)
        installer._configure(console, args, donor, layout, saved["release_id"], Path(saved["release_path"]), devices)
        donor_record = json.loads((donor / "installation.json").read_text())
        if donor_record["models"] != "review" or donor_record["profiles"] != ["ai"]:
            raise ValueError("Historical configuration builder did not select the review profile.")
        for key in ("models", "profiles", "gpu_layout", "gpu_topology", "review_model_profile"):
            saved[key] = donor_record[key]
        donor_compose = installer._dotenv(donor / "compose.env")
        for key, value in (gpu.environment() | review.environment()).items():
            installer._replace_env(compose_path, key, str(value))
        for key in ("COMPOSE_PROFILES", "CASE_INTELLIGENCE_RETRIEVAL_WORKER_URL"):
            installer._replace_env(compose_path, key, donor_compose[key])
        donor_app = installer._dotenv(installer._app_env(donor))
        for key in ("CASE_INTELLIGENCE_GENERATOR_URL", "CASE_INTELLIGENCE_GENERATOR_BACKEND", "CASE_INTELLIGENCE_GENERATOR_MODEL"):
            installer._replace_env(app_path, key, donor_app[key])
        installer._atomic_private_write(saved_path, json.dumps(saved, indent=2) + "\n")
        installer._seal_provisioning(node, saved)
    update = installer._parser().parse_args(["update", "--root", str(node), "--retire-bundled-models", "--no-backup"])
    validated, _ = installer._saved_node_arguments(update, node)
    assert validated["profiles"] == ["ai"] and update.models == "review"
    assert installer._dotenv(compose_path)["CASE_INTELLIGENCE_RETRIEVAL_WORKER_URL"] == "http://retrieval:8787"
    assert installer.bundled_generator(installer._dotenv(app_path)["CASE_INTELLIGENCE_GENERATOR_URL"])
    print("Seeded historical saved review settings on the disposable running node. No GPU jobs or model staging ran.", flush=True)


if __name__ == "__main__":
    seed(Path(sys.argv[1]).resolve())

"""Retrieve a confirmed producer mapping only within its original matter."""
from fastapi.testclient import TestClient

from case_intelligence.generation import UnavailableGenerator
from case_intelligence.managed_storage import StoragePolicy
from case_intelligence.workbench import create_workbench_app
from tests.test_case_lifecycle_web import login
from tests.test_intake_receipt_http import ACTOR, descriptor, selection
from tests.test_load_file_admission import CountingScanner


def test_profile_lookup_respects_matter_membership_and_csrf(tmp_path):
    app = create_workbench_app(tmp_path / "runtime", generator=UnavailableGenerator(),
                               auth_mode="preview", malware_scanner=CountingScanner(),
                               storage_policy=StoragePolicy(reserve_bytes=0))
    producer = "Synthetic supplier"
    fields = {"First label": "begin_bates", "Native file": "native_path"}
    with TestClient(app) as client:
        csrf = login(client)
        bench = app.state.workbench
        matter = bench.create_matter("Generated production", "", ACTOR)
        other = bench.create_matter("Other generated production", "", ACTOR)
        url = f"/matters/{matter.slug}/production-field-profile"
        request = {"producer": producer}
        assert client.post(url, json=request).status_code == 403
        client.headers["X-CSRF-Token"] = csrf
        assert client.post(url, json=request).json() == {"profile": None}
        profile = {"producer": producer, "fields": fields, "sha256": "d" * 64}
        item = dict(descriptor("Production/index.dat", 44), dat_profile=profile)
        selection(client, matter.slug, [item], [0])
        response = client.post(url, json=request)
        assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
        assert response.json() == {"profile": {"producer": producer, "fields": fields}}
        assert client.post(f"/matters/{other.slug}/production-field-profile", json=request).json() == {"profile": None}
        assert client.post(url, json={"producer": producer, "actor_id": ACTOR}).status_code == 400
        client.headers["X-CSRF-Token"] = login(client, subject="jordan-lee")
        assert client.post(url, json=request).status_code == 404
        bench.workspace.add_member(matter.matter_id, "development-jordan-lee", ACTOR)
        assert client.post(url, json=request).json() == response.json()

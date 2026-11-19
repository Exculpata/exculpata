"""Matter setup uses the displayed destination and case-team management authority."""

from fastapi.testclient import TestClient

from tests.test_source_library_accessibility import RenderedHTML
from tests.test_matter_management import ADMIN, OTHER, OWNER, _app, _create_matter, _csrf, _headers, _principal_id


def _grant_forms(page, path):
    forms = {}
    for form in RenderedHTML(page.text).matching(method="post", action=path):
        fields = {child.attrs["name"]: child.attrs["value"] for child in form.children}
        forms[fields["capability"]] = fields
    return forms


def test_setup_reviews_exact_destinations_rotates_grants_and_allows_revocation(tmp_path, monkeypatch):
    app = _app(tmp_path)
    with TestClient(app, base_url="https://testserver") as owner:
        token = _csrf(owner.get("/", headers=_headers(OWNER)).text)
        slug, second = [
            _create_matter(owner, principal=OWNER, csrf_token=token, name=name)
            for name in ("Synthetic first model grant", "Synthetic second model grant")
        ]
        monkeypatch.setenv("CASE_INTELLIGENCE_EMBEDDING_URL", "https://MODELS.example.test:443/")
        monkeypatch.setenv("CASE_INTELLIGENCE_EMBEDDING_MODEL", "synthetic-embedding")
        monkeypatch.setenv("CASE_INTELLIGENCE_EMBEDDING_API_KEY", "synthetic-use-only-secret")
        monkeypatch.setenv("CASE_INTELLIGENCE_RERANK_URL", "https://ranking.example.test")
        monkeypatch.setenv("CASE_INTELLIGENCE_RERANK_MODEL", "synthetic-reranker")
        monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_BACKEND", "openai")
        monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_SERVER", "vllm")
        monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_URL", "https://answers.example.test/tenant-a")
        monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_MODEL", "synthetic-answers")

        path = f"/matters/{slug}/model-grants"
        setup = f"/matters/{slug}/setup"
        page = owner.get(setup, headers=_headers(OWNER))
        assert page.status_code == 200
        assert "External model permissions" in page.text
        for endpoint, model in (("https://models.example.test", "synthetic-embedding"),
                              ("https://answers.example.test/tenant-a", "synthetic-answers"),
                              ("https://ranking.example.test", "synthetic-reranker")):
            assert endpoint in page.text and model in page.text
        assert "synthetic-use-only-secret" not in page.text
        forms = _grant_forms(page, path)
        assert set(forms) == {"embedding", "generation", "reranking"}
        values = forms["embedding"]
        assert values["endpoint"] == "https://models.example.test" and values["model"] == "synthetic-embedding"
        assert values["decision"] == "approve" and values["csrf_token"] == token
        assert owner.post(path, headers=_headers(OWNER), data={**values, "csrf_token": ""}).status_code == 403
        unversioned = {key: value for key, value in values.items() if key != "configuration_id"}
        assert owner.post(path, headers=_headers(OWNER), data=unversioned).status_code == 422

        generation = forms["generation"]
        assert generation["endpoint"] == "https://answers.example.test/tenant-a"
        monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_URL", "https://answers.example.test/tenant-b")
        assert owner.post(path, headers=_headers(OWNER), data=generation).status_code == 409
        assert "Approved for this matter" not in owner.get(setup, headers=_headers(OWNER)).text
        for tampered in (dict(endpoint="https://different.example.test"), dict(model="different-model")):
            denied = owner.post(path, headers=_headers(OWNER), data={**values, **tampered})
            assert denied.status_code == 409
        approved = owner.post(path, headers=_headers(OWNER), data=values, follow_redirects=False)
        assert approved.status_code == 303 and approved.headers["location"] == setup + "#model-grants"
        assert "Approved for this matter" in owner.get(setup, headers=_headers(OWNER)).text
        assert "Approved for this matter" not in owner.get(f"/matters/{second}/setup", headers=_headers(OWNER)).text

        monkeypatch.setenv("CASE_INTELLIGENCE_EMBEDDING_MODEL", "replacement-embedding")
        assert owner.post(path, headers=_headers(OWNER), data=values).status_code == 409
        page = owner.get(setup, headers=_headers(OWNER))
        assert "synthetic-embedding" not in page.text and "replacement-embedding" in page.text
        assert "Approved for this matter" not in page.text
        monkeypatch.setenv("CASE_INTELLIGENCE_EMBEDDING_MODEL", "synthetic-embedding")
        assert owner.post(path, headers=_headers(OWNER), data=values, follow_redirects=False).status_code == 409
        refreshed = owner.get(setup, headers=_headers(OWNER))
        assert "Approved for this matter" not in refreshed.text
        fresh_values = _grant_forms(refreshed, path)["embedding"]
        assert fresh_values["configuration_id"] != values["configuration_id"]
        assert owner.post(path, headers=_headers(OWNER), data=fresh_values, follow_redirects=False).status_code == 303
        admin = TestClient(app, base_url="https://testserver")
        admin_page = admin.get(setup, headers=_headers(ADMIN))
        assert "Approved for this matter" in admin_page.text
        admin_values = _grant_forms(admin_page, path)["embedding"]
        assert admin_values["decision"] == "revoke"
        revoked = admin.post(path, headers=_headers(ADMIN),
                             data=admin_values,
                             follow_redirects=False)
        assert revoked.status_code == 303
        assert "Approved for this matter" not in owner.get(setup, headers=_headers(OWNER)).text


def test_setup_grants_require_matter_management_authority(tmp_path, monkeypatch):
    app = _app(tmp_path)
    with TestClient(app, base_url="https://testserver") as owner:
        token = _csrf(owner.get("/", headers=_headers(OWNER)).text)
        slug = _create_matter(owner, principal=OWNER, csrf_token=token, name="Synthetic restricted setup")
        monkeypatch.setenv("CASE_INTELLIGENCE_EMBEDDING_URL", "https://private-model.example.test")
        monkeypatch.setenv("CASE_INTELLIGENCE_EMBEDDING_MODEL", "synthetic-private-model")
        path = f"/matters/{slug}/model-grants"
        setup = f"/matters/{slug}/setup"
        other = TestClient(app, base_url="https://testserver")
        other_token = _csrf(other.get("/", headers=_headers(OTHER)).text)
        values = dict(csrf_token=other_token, capability="embedding", decision="approve", configuration_id="a" * 32,
                      endpoint="https://private-model.example.test", model="synthetic-private-model")
        assert other.get(setup, headers=_headers(OTHER)).status_code == 404
        assert other.post(path, headers=_headers(OTHER), data=values).status_code == 404
        added = owner.post(f"/matters/{slug}/members", headers=_headers(OWNER),
                           data=dict(csrf_token=token, principal_id=_principal_id(other, OTHER)),
                           follow_redirects=False)
        assert added.status_code == 303
        page = other.get(setup, headers=_headers(OTHER))
        assert page.status_code == 200 and "private-model.example.test" not in page.text
        assert "External model permissions" not in page.text
        for decision in ("approve", "revoke"):
            denied = other.post(path, headers=_headers(OTHER), data={**values, "decision": decision})
            assert denied.status_code == 403 and "private-model.example.test" not in denied.text
        assert "Approved for this matter" not in owner.get(setup, headers=_headers(OWNER)).text

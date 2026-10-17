from __future__ import annotations


from case_intelligence.legacy_names import (
    LEGACY_STEM,
)
from fastapi.testclient import TestClient

from tests.test_matter_management import (
    ADMIN,
    OTHER,
    OWNER,
    _app,
    _create_matter,
    _csrf,
    _headers,
    _principal_id,
)


def test_administrator_can_open_matter_after_owner_is_disabled(tmp_path) -> None:
    with TestClient(_app(tmp_path), base_url=f"https://{LEGACY_STEM}.example.test") as client:
        assert client.get(
            "/auth/login", headers=_headers(OWNER), follow_redirects=False
        ).status_code == 303
        landing = client.get("/matters/new", headers=_headers(OWNER))
        slug = _create_matter(
            client,
            principal=OWNER,
            csrf_token=_csrf(landing.text),
            name="Synthetic disabled-owner matter",
        )
        bench = client.app.state.workbench
        owner_id = _principal_id(client, OWNER)
        matter = bench.matter(slug, owner_id)
        with bench.workspace.connection:
            bench.workspace.connection.execute(
                "UPDATE workbench_principal SET active=0 WHERE principal_id=?",
                (owner_id,),
            )

        client.cookies.clear()
        assert client.get(
            "/auth/login", headers=_headers(ADMIN), follow_redirects=False
        ).status_code == 303
        managed = client.get("/matters/manage", headers=_headers(ADMIN))
        assert managed.status_code == 200
        assert f'href="/matters/{slug}/home"' in managed.text

        home = client.get(f"/matters/{slug}/home", headers=_headers(ADMIN))
        assert home.status_code == 200
        assert "Synthetic disabled-owner matter" in home.text
        admin_id = _principal_id(client, ADMIN)
        home_events = tuple(
            event
            for event in bench.workspace.audit_events(matter.matter_id)
            if event.action == "matter.home" and event.outcome == "success"
        )
        assert home_events
        assert home_events[-1].actor_principal_id == admin_id
        assert home_events[-1].actor_principal_id != owner_id

        client.cookies.clear()
        assert client.get(
            "/auth/login", headers=_headers(OTHER), follow_redirects=False
        ).status_code == 303
        assert client.get(
            f"/matters/{slug}/home", headers=_headers(OTHER)
        ).status_code == 404

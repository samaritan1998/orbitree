from research_ops import models
from research_ops.services.bootstrap import bootstrap_catalog


def test_direction_create_update_archive_survives_bootstrap(client, settings):
    created = client.post("/api/programs", json={"code": "ML", "name": "My research"})
    assert created.status_code == 200, created.text
    data = created.json()
    assert (settings.workspace_root / "ML" / "_desk").is_dir()
    updated = {
        "name": "Renamed",
        "objective": "Changed",
        "color": "#123456",
        "sort_order": 1,
        "active": False,
    }
    assert client.put("/api/programs/ML", json=updated).status_code == 200
    state = client.app.state.rops
    bootstrap_catalog(state.database.session_factory, settings)
    assert not any(p["code"] == "ML" for p in client.get("/api/programs").json())
    all_programs = client.get("/api/programs?include_archived=true").json()
    program = next(p for p in all_programs if p["id"] == data["id"])
    assert program["name"] == "Renamed" and not program["active"]
    assert client.post("/api/programs", json={"code": "ML", "name": "Duplicate"}).status_code == 409
    assert (
        client.post("/api/work-items", json={"program_code": "ML", "title": "Task"}).status_code
        == 409
    )
    assert client.put("/api/programs/ML", json=updated | {"active": True}).status_code == 200
    assert (
        client.post("/api/work-items", json={"program_code": "ML", "title": "Task"}).status_code
        == 200
    )


def test_existing_program_preferences_are_not_reset(client, settings):
    original = client.get("/api/programs").json()[0]
    values = {key: original[key] for key in ("name", "objective", "color", "sort_order", "active")}
    values["name"] = "Personal name"
    values["active"] = False
    assert client.put(f"/api/programs/{original['code']}", json=values).status_code == 200
    bootstrap_catalog(client.app.state.rops.database.session_factory, settings)
    result = client.get("/api/programs?include_archived=true").json()
    item = next(p for p in result if p["code"] == original["code"])
    assert item["name"] == "Personal name" and not item["active"]


def test_program_validation_and_last_direction(client):
    for code in ("../X", "with-dash", "LOWER", "A"):
        payload = {
            "code": code,
            "name": "X",
            "color": "url(evil)" if code == "LOWER" else "#123456",
        }
        assert client.post("/api/programs", json=payload).status_code == 422
    state = client.app.state.rops
    with state.database.session_factory() as session:
        for program in session.query(models.Program).all():
            program.active = program.code == "FREE"
        session.commit()
    assert (
        client.put(
            "/api/programs/FREE",
            json={
                "name": "Free",
                "color": "#123456",
                "sort_order": 1,
                "active": False,
            },
        ).status_code
        == 409
    )

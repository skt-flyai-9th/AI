def test_live(client):
    response = client.get("/api/v1/health/live")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_ready(client):
    response = client.get("/api/v1/health/ready")
    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert response.json() == {"status": "ready"}


def test_diagnostics_requires_internal_auth(client, auth_headers):
    assert client.get("/api/v1/health/diagnostics").status_code == 401
    response = client.get("/api/v1/health/diagnostics", headers=auth_headers)
    assert response.status_code == 200
    assert "database_knowledge_runtime" in response.json()
    assert response.json()["database_knowledge_ready"] is False
    assert response.json()["database_knowledge_data"] == {
        "video_editing_db": False,
        "trade_area_db": False,
    }

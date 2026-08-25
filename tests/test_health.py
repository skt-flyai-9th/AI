from app.db.session import SessionLocal
from app.models.editing_template import EditingTemplate


def test_live(client):
    response = client.get("/api/v1/health/live")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_ready(client):
    response = client.get("/api/v1/health/ready")
    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert response.json()["active_editing_template_count"] == 3


def test_ready_fails_when_active_template_catalogue_is_empty(client):
    with SessionLocal() as db:
        db.query(EditingTemplate).update({"status": "ARCHIVED"})
        db.commit()

    response = client.get("/api/v1/health/ready")

    assert response.status_code == 503
    assert response.json()["detail"]["status"] == "not_ready"
    assert response.json()["detail"]["active_editing_template_count"] == 0

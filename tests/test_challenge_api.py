from datetime import datetime, timezone

from app.db.session import SessionLocal
from app.models.challenge import Challenge
from app.models.video_editing_db_record import VideoEditingDBRecord


def seed_challenge():
    with SessionLocal() as db:
        db.add(
            Challenge(
                id="bad-challenge",
                automatic_name="BAD 챌린지",
                automatic_rank=1,
                automatic_score=91.2,
                lifecycle="RISING",
                kr_affinity=0.9,
                confidence=0.8,
                category="dance",
                automatic_representative_youtube_url="https://www.youtube.com/watch?v=AAA",
                automatic_guide_youtube_url="https://www.youtube.com/watch?v=BBB",
                active=True,
                last_seen_at=datetime.now(timezone.utc),
            )
        )
        db.commit()


def test_list_and_patch_override(client, auth_headers):
    seed_challenge()
    response = client.get("/api/v1/challenges?limit=100", headers=auth_headers)
    assert response.status_code == 200
    item = response.json()["results"][0]
    assert item["rank"] == 1
    assert item["guide_youtube_url"].endswith("BBB")

    patched = client.patch(
        "/api/v1/challenges/bad-challenge",
        headers=auth_headers,
        json={
            "rank": 3,
            "representative_youtube_url": "https://www.youtube.com/watch?v=NEW",
        },
    )
    assert patched.status_code == 200
    data = patched.json()
    assert data["rank"] == 3
    assert data["representative_video_overridden"] is True


def test_challenge_exposes_active_editing_template_reference(client, auth_headers):
    seed_challenge()
    with SessionLocal() as db:
        db.add(
            VideoEditingDBRecord(
                template_id="gt_bad_challenge",
                version=3,
                status="ACTIVE",
                name="BAD 챌린지 가이드",
                trend_ids=["bad-challenge"],
            )
        )
        db.commit()

    response = client.get("/api/v1/challenges?limit=100", headers=auth_headers)

    assert response.status_code == 200
    item = response.json()["results"][0]
    assert item["editing_template_id"] == "gt_bad_challenge"
    assert item["editing_template_version"] == 3


def test_clear_override_with_null(client, auth_headers):
    seed_challenge()
    client.patch(
        "/api/v1/challenges/bad-challenge",
        headers=auth_headers,
        json={"rank": 5},
    )
    response = client.patch(
        "/api/v1/challenges/bad-challenge",
        headers=auth_headers,
        json={"rank": None},
    )
    assert response.status_code == 200
    assert response.json()["rank"] == 1
    assert response.json()["rank_overridden"] is False


def test_internal_api_key_is_required(client):
    seed_challenge()
    response = client.get("/api/v1/challenges?limit=100")
    assert response.status_code == 401

from io import BytesIO

import pytest

from app.services import source_assets
from app.services.source_assets import (
    SourceAssetTooLargeError,
    download_source_asset,
    parse_s3_object_url,
)


class _Body:
    def __init__(self, value: bytes) -> None:
        self.value = value
        self.closed = False

    def iter_chunks(self, chunk_size: int):
        stream = BytesIO(self.value)
        while chunk := stream.read(chunk_size):
            yield chunk

    def close(self) -> None:
        self.closed = True


def test_parse_virtual_hosted_s3_url():
    location = parse_s3_object_url(
        "https://sarils-s3.s3.ap-northeast-2.amazonaws.com/projects/7/footage/a%20b.mp4"
    )

    assert location is not None
    assert location.bucket == "sarils-s3"
    assert location.key == "projects/7/footage/a b.mp4"


def test_private_s3_asset_uses_instance_role_client(tmp_path, monkeypatch):
    captured = {}
    body = _Body(b"private-video")

    class _S3:
        def get_object(self, **kwargs):
            captured.update(kwargs)
            return {"ContentLength": len(body.value), "Body": body}

    monkeypatch.setattr(source_assets.boto3, "client", lambda service: _S3())
    target = tmp_path / "source.mp4"

    download_source_asset(
        "https://sarils-s3.s3.ap-northeast-2.amazonaws.com/projects/7/footage/a.mp4",
        target,
        max_bytes=1024,
        timeout_seconds=10,
    )

    assert captured == {"Bucket": "sarils-s3", "Key": "projects/7/footage/a.mp4"}
    assert target.read_bytes() == b"private-video"
    assert body.closed is True


def test_private_s3_asset_enforces_download_limit(tmp_path, monkeypatch):
    body = _Body(b"too-large")

    class _S3:
        def get_object(self, **_kwargs):
            return {"ContentLength": len(body.value), "Body": body}

    monkeypatch.setattr(source_assets.boto3, "client", lambda service: _S3())
    target = tmp_path / "source.mp4"

    with pytest.raises(SourceAssetTooLargeError):
        download_source_asset(
            "https://bucket.s3.amazonaws.com/source.mp4",
            target,
            max_bytes=4,
            timeout_seconds=10,
        )

    assert not target.exists()
    assert body.closed is True

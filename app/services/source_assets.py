from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

import boto3
import httpx


class SourceAssetDownloadError(RuntimeError):
    pass


class SourceAssetTooLargeError(SourceAssetDownloadError):
    pass


@dataclass(frozen=True)
class S3ObjectLocation:
    bucket: str
    key: str


_VIRTUAL_HOSTED_S3 = re.compile(
    r"^(?P<bucket>.+)\.s3(?:[.-][a-z0-9-]+)?\.amazonaws\.com(?:\.cn)?$",
    re.IGNORECASE,
)
_PATH_STYLE_S3 = re.compile(
    r"^s3(?:[.-][a-z0-9-]+)?\.amazonaws\.com(?:\.cn)?$",
    re.IGNORECASE,
)


def download_source_asset(
    url: str,
    target: Path,
    *,
    max_bytes: int,
    timeout_seconds: int,
) -> None:
    """Download an HTTP(S) asset, signing private AWS S3 reads with the instance role."""

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise SourceAssetDownloadError("Source asset URL must be HTTP(S).")

    location = parse_s3_object_url(url)
    try:
        if location is not None:
            _download_s3(location, target, max_bytes=max_bytes)
        else:
            _download_http(
                url,
                target,
                max_bytes=max_bytes,
                timeout_seconds=timeout_seconds,
            )
    except SourceAssetDownloadError:
        target.unlink(missing_ok=True)
        raise
    except Exception as exc:
        target.unlink(missing_ok=True)
        raise SourceAssetDownloadError("Could not download source asset.") from exc


def parse_s3_object_url(url: str) -> S3ObjectLocation | None:
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    virtual = _VIRTUAL_HOSTED_S3.fullmatch(hostname)
    if virtual:
        key = unquote(parsed.path.lstrip("/"))
        return S3ObjectLocation(bucket=virtual.group("bucket"), key=key) if key else None

    if _PATH_STYLE_S3.fullmatch(hostname):
        bucket, separator, key = unquote(parsed.path.lstrip("/")).partition("/")
        if bucket and separator and key:
            return S3ObjectLocation(bucket=bucket, key=key)
    return None


def _download_s3(location: S3ObjectLocation, target: Path, *, max_bytes: int) -> None:
    response = boto3.client("s3").get_object(Bucket=location.bucket, Key=location.key)
    declared = int(response.get("ContentLength") or 0)
    if declared > max_bytes:
        response["Body"].close()
        raise SourceAssetTooLargeError("Source asset exceeds the download limit.")

    size = 0
    body = response["Body"]
    try:
        with target.open("wb") as output:
            for chunk in body.iter_chunks(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                size += len(chunk)
                if size > max_bytes:
                    raise SourceAssetTooLargeError("Source asset exceeds the download limit.")
                output.write(chunk)
    finally:
        body.close()


def _download_http(
    url: str,
    target: Path,
    *,
    max_bytes: int,
    timeout_seconds: int,
) -> None:
    timeout = httpx.Timeout(timeout_seconds)
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        with client.stream("GET", url) as response:
            response.raise_for_status()
            declared = int(response.headers.get("content-length") or 0)
            if declared > max_bytes:
                raise SourceAssetTooLargeError("Source asset exceeds the download limit.")
            size = 0
            with target.open("wb") as output:
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise SourceAssetTooLargeError("Source asset exceeds the download limit.")
                    output.write(chunk)

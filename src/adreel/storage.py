"""Blob storage behind one interface: local disk for dev, S3-compatible for prod.

Keys are POSIX-style relative paths (``users/12/projects/<uuid>/clips/1.mp4``). Workers copy
blobs to local disk before ffmpeg touches them and upload results afterwards, so the render
code never knows where files live.
"""

from __future__ import annotations

import shutil
from pathlib import Path, PurePosixPath
from typing import Protocol

from adreel.config import Settings


class StorageError(RuntimeError):
    pass


def _check_key(key: str) -> str:
    p = PurePosixPath(key)
    if p.is_absolute() or ".." in p.parts or not key or "\\" in key:
        raise StorageError(f"invalid storage key: {key!r}")
    return key


class Storage(Protocol):
    def put_file(self, local: Path, key: str) -> None: ...
    def get_file(self, key: str, local: Path) -> Path: ...
    def exists(self, key: str) -> bool: ...
    def delete(self, key: str) -> None: ...


class LocalStorage:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.root / _check_key(key)

    def put_file(self, local: Path, key: str) -> None:
        dst = self._path(key)
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".part")
        shutil.copyfile(local, tmp)
        tmp.replace(dst)

    def get_file(self, key: str, local: Path) -> Path:
        src = self._path(key)
        if not src.is_file():
            raise StorageError(f"not found: {key}")
        local.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, local)
        return local

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)


class S3Storage:
    def __init__(
        self,
        bucket: str,
        *,
        prefix: str = "",
        endpoint_url: str | None = None,
        region: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
    ) -> None:
        import boto3

        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self._s3 = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            region_name=region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
        )

    def _key(self, key: str) -> str:
        key = _check_key(key)
        return f"{self.prefix}/{key}" if self.prefix else key

    def put_file(self, local: Path, key: str) -> None:
        self._s3.upload_file(str(local), self.bucket, self._key(key))

    def get_file(self, key: str, local: Path) -> Path:
        local.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._s3.download_file(self.bucket, self._key(key), str(local))
        except Exception as exc:  # botocore ClientError and friends
            raise StorageError(f"download failed for {key}: {exc}") from exc
        return local

    def exists(self, key: str) -> bool:
        try:
            self._s3.head_object(Bucket=self.bucket, Key=self._key(key))
        except Exception:
            return False
        return True

    def delete(self, key: str) -> None:
        self._s3.delete_object(Bucket=self.bucket, Key=self._key(key))


def build_storage(s: Settings) -> Storage:
    if s.storage_backend == "s3":
        if not s.s3_bucket:
            raise StorageError("ADREEL_S3_BUCKET is required for the s3 backend")
        return S3Storage(
            s.s3_bucket,
            prefix=s.s3_prefix,
            endpoint_url=s.s3_endpoint_url,
            region=s.s3_region,
            access_key=s.s3_access_key_id,
            secret_key=s.s3_secret_access_key.get_secret_value()
            if s.s3_secret_access_key
            else None,
        )
    return LocalStorage(s.storage_dir)


# ---------------------------------------------------------------------------- key scheme


def clip_key(user_id: int, project_id: object, idx: int) -> str:
    return f"users/{user_id}/projects/{project_id}/clips/{idx}.mp4"


def output_key(user_id: int, project_id: object, name: str) -> str:
    return f"users/{user_id}/projects/{project_id}/{name}"


def logo_key(user_id: int, suffix: str = ".png") -> str:
    return f"users/{user_id}/brand/logo{suffix}"

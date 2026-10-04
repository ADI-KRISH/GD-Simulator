"""Object storage for audio, recordings and exports (design §35).

MinIO, S3-compatible, running locally in Docker. Kept behind a small interface
for the same reason as the STT and LLM layers: the architecture must not depend
on one vendor (design §54).

Raw audio lives here rather than in Postgres because design §38 requires that a
failed transcription can be retried against the original bytes long after the
browser that produced them has gone.
"""

import logging
from pathlib import Path
from typing import Protocol

from services.common.config import REPO_ROOT, settings

logger = logging.getLogger(__name__)


class ObjectStore(Protocol):
    name: str

    async def put(
        self, key: str, data: bytes, *, content_type: str = "application/octet-stream"
    ) -> str: ...

    async def get(self, key: str) -> bytes: ...

    async def exists(self, key: str) -> bool: ...


class FilesystemStore:
    """Local-disk store used when MinIO is not running.

    The whole stack is local anyway, so a directory under the repo is a
    legitimate object store for development — and it means `pytest` and a bare
    `uvicorn` both work with nothing else started. The path is gitignored
    (`services/media/`).
    """

    name = "filesystem"

    def __init__(self, root: Path | None = None) -> None:
        self._root = root or (REPO_ROOT / "services" / "media")

    def _path(self, key: str) -> Path:
        # Keys are server-generated, but never let one escape the root.
        safe = Path(key.replace("\\", "/"))
        if safe.is_absolute() or ".." in safe.parts:
            raise ValueError(f"unsafe storage key: {key!r}")
        return self._root / safe

    async def put(
        self, key: str, data: bytes, *, content_type: str = "application/octet-stream"
    ) -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return key

    async def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    async def exists(self, key: str) -> bool:
        return self._path(key).exists()


class MinioStore:
    """MinIO over its S3 API (design §54, `http://localhost:9010`).

    `boto3`/`minio` are not dependencies: the three operations needed here are a
    signed PUT and GET, and adding an SDK for that would pull a large tree into
    a project whose object storage is one container on localhost. This uses
    plain HTTP with SigV4-free presigned-style access via the MinIO root
    credentials, which is exactly what the local dev setup exposes.
    """

    name = "minio"

    def __init__(self) -> None:
        self._endpoint = settings.minio_endpoint.rstrip("/")
        self._bucket = settings.minio_bucket
        self._auth = (settings.minio_root_user, settings.minio_root_password)

    async def _client(self):
        import httpx

        return httpx.AsyncClient(base_url=self._endpoint, auth=self._auth, timeout=30.0)

    async def put(
        self, key: str, data: bytes, *, content_type: str = "application/octet-stream"
    ) -> str:
        async with await self._client() as client:
            response = await client.put(
                f"/{self._bucket}/{key}", content=data, headers={"Content-Type": content_type}
            )
            response.raise_for_status()
        return key

    async def get(self, key: str) -> bytes:
        async with await self._client() as client:
            response = await client.get(f"/{self._bucket}/{key}")
            response.raise_for_status()
            return response.content

    async def exists(self, key: str) -> bool:
        async with await self._client() as client:
            response = await client.head(f"/{self._bucket}/{key}")
            return response.status_code == 200


_store: ObjectStore | None = None


def get_object_store() -> ObjectStore:
    global _store
    if _store is None:
        _store = MinioStore() if settings.object_store == "minio" else FilesystemStore()
        logger.info("object store selected", extra={"store": _store.name})
    return _store


def set_object_store(store: ObjectStore | None) -> None:
    """Override for tests."""
    global _store
    _store = store


def audio_key(session_id: str, candidate_label: str, sequence: int) -> str:
    """Deterministic layout: one prefix per session, so a session's audio can be
    listed or deleted as a unit (NFR-07 privacy)."""
    return f"sessions/{session_id}/audio/{candidate_label}/{sequence:06d}.webm"

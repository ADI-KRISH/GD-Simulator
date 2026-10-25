"""Object storage for audio, recordings and exports (design §35).

MinIO, S3-compatible, running locally in Docker. Kept behind a small interface
for the same reason as the STT and LLM layers: the architecture must not depend
on one vendor (design §54).

Raw audio lives here rather than in Postgres because design §38 requires that a
failed transcription can be retried against the original bytes long after the
browser that produced them has gone.

Purpose:    save and fetch binary blobs (mainly 45-second audio chunks) by key,
            on local disk or in MinIO, behind one interface.
Why needed: speech-to-text can fail; keeping the original audio lets the
            transcript be rebuilt later, and the pipeline still runs with no
            MinIO container.
Contains:
    ObjectStore      - the put/get/exists interface any backend must offer.
    FilesystemStore  - default store: files under `services/media/` (gitignored).
    MinioStore       - talks to MinIO over plain HTTP, no SDK dependency.
    get_object_store - picks the store from settings, once per process.
    set_object_store - lets tests swap in their own store.
    audio_key        - the single naming scheme for audio keys, so one session's
                       audio can be listed or deleted together (NFR-07 privacy).
Used by:    speech/service, tests/conftest, tests/test_speech_pipeline.
Depends on: common/config (REPO_ROOT, MinIO settings), common/logging
            (structured log of which store was chosen).
"""

import logging  # module-level logger for store selection
from pathlib import Path  # filesystem store paths
from typing import Protocol  # structural interface any store backend must satisfy

from services.common.config import REPO_ROOT, settings  # repo root + MinIO/env settings

logger = logging.getLogger(__name__)


class ObjectStore(Protocol):
    """Interface every storage backend implements: put, get and exists by key."""

    name: str

    async def put(
        self, key: str, data: bytes, *, content_type: str = "application/octet-stream"
    ) -> str:
        """Store `data` under `key` and return the key."""
        ...

    async def get(self, key: str) -> bytes:
        """Return the bytes stored under `key`."""
        ...

    async def exists(self, key: str) -> bool:
        """Return True if an object is stored under `key`."""
        ...


class FilesystemStore:
    """Local-disk store used when MinIO is not running.

    The whole stack is local anyway, so a directory under the repo is a
    legitimate object store for development — and it means `pytest` and a bare
    `uvicorn` both work with nothing else started. The path is gitignored
    (`services/media/`).
    """

    name = "filesystem"

    def __init__(self, root: Path | None = None) -> None:
        # Default root is services/media under the repo; tests may pass a temp dir.
        self._root = root or (REPO_ROOT / "services" / "media")

    def _path(self, key: str) -> Path:
        """Map a storage key to a path under the root, rejecting unsafe keys."""
        # Keys are server-generated, but never let one escape the root.
        safe = Path(key.replace("\\", "/"))
        if safe.is_absolute() or ".." in safe.parts:
            raise ValueError(f"unsafe storage key: {key!r}")
        return self._root / safe

    async def put(
        self, key: str, data: bytes, *, content_type: str = "application/octet-stream"
    ) -> str:
        """Write `data` to disk (content type is unused locally) and return the key."""
        path = self._path(key)
        # Create the per-session folders on demand before writing the file.
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return key

    async def get(self, key: str) -> bytes:
        """Read the file stored under `key`."""
        return self._path(key).read_bytes()

    async def exists(self, key: str) -> bool:
        """Check whether a file exists for `key`."""
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
        # Endpoint, bucket and root credentials all come from settings (.env).
        self._endpoint = settings.minio_endpoint.rstrip("/")
        self._bucket = settings.minio_bucket
        self._auth = (settings.minio_root_user, settings.minio_root_password)

    async def _client(self):
        """Build a short-lived authenticated HTTP client for the MinIO endpoint."""
        import httpx  # imported lazily so the filesystem path never needs it

        return httpx.AsyncClient(base_url=self._endpoint, auth=self._auth, timeout=30.0)

    async def put(
        self, key: str, data: bytes, *, content_type: str = "application/octet-stream"
    ) -> str:
        """Upload `data` to the bucket with the given content type; return the key."""
        async with await self._client() as client:
            response = await client.put(
                f"/{self._bucket}/{key}", content=data, headers={"Content-Type": content_type}
            )
            # A failed upload must surface so the caller can retry, not vanish.
            response.raise_for_status()
        return key

    async def get(self, key: str) -> bytes:
        """Download the object stored under `key`."""
        async with await self._client() as client:
            response = await client.get(f"/{self._bucket}/{key}")
            response.raise_for_status()
            return response.content

    async def exists(self, key: str) -> bool:
        """HEAD the object; a 200 means it exists."""
        async with await self._client() as client:
            response = await client.head(f"/{self._bucket}/{key}")
            return response.status_code == 200


# Process-wide singleton, created on first use by get_object_store().
_store: ObjectStore | None = None


def get_object_store() -> ObjectStore:
    """Return the configured store (MinIO or filesystem), creating it on first call."""
    global _store
    # Choose once from settings and remember, so every caller shares one store.
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

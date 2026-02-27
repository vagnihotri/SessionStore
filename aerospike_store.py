"""
Custom starsessions SessionStore backed by Aerospike.

Extends starsessions.SessionStore and implements the three abstract methods:
  - read(session_id, lifetime) -> bytes
  - write(session_id, data, lifetime, ttl) -> str
  - remove(session_id) -> None
"""

import time

import aerospike
from aerospike import exception as aex
from starsessions import SessionStore

# Default TTL when lifetime is 0 (session-only cookies) — 30 days.
DEFAULT_GC_TTL = 60 * 60 * 24 * 30


class AerospikeSessionStore(SessionStore):
    def __init__(
        self,
        hosts: list[tuple[str, int]] | None = None,
        namespace: str = "test",
        set_name: str = "fastapi_sessions",
        gc_ttl: int = DEFAULT_GC_TTL,
        connect_retries: int = 10,
        retry_delay: float = 2.0,
    ) -> None:
        self._namespace = namespace
        self._set = set_name
        self._gc_ttl = gc_ttl
        self._client = self._connect_with_retry(
            hosts or [("127.0.0.1", 3000)], connect_retries, retry_delay
        )

    @staticmethod
    def _connect_with_retry(
        hosts: list, retries: int, delay: float
    ) -> aerospike.Client:
        for attempt in range(1, retries + 1):
            try:
                client = aerospike.client({"hosts": hosts}).connect()
                print(f"Connected to Aerospike at {hosts}")
                return client
            except aex.AerospikeError as e:
                if attempt == retries:
                    raise
                print(f"Aerospike not ready (attempt {attempt}/{retries}): {e}")
                time.sleep(delay)
        raise RuntimeError("Unreachable")

    def _key(self, session_id: str) -> tuple:
        return (self._namespace, self._set, session_id)

    async def read(self, session_id: str, lifetime: int) -> bytes:
        """Read session data from Aerospike. Returns empty bytes if missing."""
        try:
            _, _, bins = self._client.get(self._key(session_id))
            return bins.get("data", b"")
        except aex.RecordNotFound:
            return b""

    async def write(
        self, session_id: str, data: bytes, lifetime: int, ttl: int
    ) -> str:
        """Write session data to Aerospike using native record TTL."""
        record_ttl = ttl if ttl > 0 else self._gc_ttl
        self._client.put(
            self._key(session_id),
            {"data": data},
            #meta={"ttl": record_ttl},
        )
        return session_id

    async def remove(self, session_id: str) -> None:
        """Remove session record from Aerospike."""
        try:
            self._client.remove(self._key(session_id))
        except aex.RecordNotFound:
            pass

    def close(self) -> None:
        """Close the Aerospike client connection."""
        self._client.close()

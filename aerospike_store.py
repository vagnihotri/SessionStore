"""
Custom starsessions SessionStore backed by Aerospike.

Extends starsessions.SessionStore and implements the three abstract methods:
  - read(session_id, lifetime) -> bytes
  - write(session_id, data, lifetime, ttl) -> str
  - remove(session_id) -> None

Enforces one-session-per-user via a separate "user_sessions" set that maps
username → session_id.  When a user logs in from a new browser/device the
previous session is automatically invalidated.
"""

import json
import time

import aerospike
from aerospike import exception as aex
from starsessions import SessionStore

# Aerospike set that holds the username → session_id mapping.
USER_SESSIONS_SET = "user_sessions"


class AerospikeSessionStore(SessionStore):
    def __init__(
        self,
        hosts: list[tuple[str, int]] | None = None,
        namespace: str = "test",
        set_name: str = "fastapi_sessions",
        connect_retries: int = 10,
        retry_delay: float = 2.0,
    ) -> None:
        self._namespace = namespace
        self._set = set_name
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

    # --- key helpers ---

    def _key(self, session_id: str) -> tuple:
        return (self._namespace, self._set, session_id)

    def _user_key(self, username: str) -> tuple:
        return (self._namespace, USER_SESSIONS_SET, username)

    # --- SessionStore interface ---

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
        """Write session data and enforce one-session-per-user."""
        username = self._extract_username(data)
        if username:
            self._invalidate_previous_session(username, session_id)

        self._client.put(self._key(session_id), {"data": data})
        return session_id

    async def remove(self, session_id: str) -> None:
        """Remove session record and clean up user mapping."""
        try:
            _, _, bins = self._client.get(self._key(session_id))
            username = self._extract_username(bins.get("data", b""))
            if username:
                self._remove_user_mapping(username, session_id)
        except aex.RecordNotFound:
            pass

        try:
            self._client.remove(self._key(session_id))
        except aex.RecordNotFound:
            pass

    # --- one-session-per-user helpers ---

    @staticmethod
    def _extract_username(data: bytes) -> str | None:
        """Parse serialised session bytes and return the username if present."""
        try:
            return json.loads(data).get("username") if data else None
        except (json.JSONDecodeError, AttributeError):
            return None

    def _invalidate_previous_session(
        self, username: str, new_session_id: str
    ) -> None:
        """Remove the old session if this user already had one."""
        try:
            _, _, bins = self._client.get(self._user_key(username))
            old_session_id = bins.get("session_id")
            if old_session_id and old_session_id != new_session_id:
                try:
                    self._client.remove(self._key(old_session_id))
                except aex.RecordNotFound:
                    pass
        except aex.RecordNotFound:
            pass

        self._client.put(
            self._user_key(username),
            {"session_id": new_session_id},
        )

    def get_user_session(self, username: str) -> dict | None:
        """Look up the active session for a user. Returns session data dict or None."""
        try:
            _, _, bins = self._client.get(self._user_key(username))
            session_id = bins.get("session_id")
            if not session_id:
                return None
        except aex.RecordNotFound:
            return None

        try:
            _, meta, bins = self._client.get(self._key(session_id))
            parsed = json.loads(bins.get("data", b"{}"))
            parsed.pop("__metadata__", None)
            return {
                "session_id": session_id,
                "data": parsed,
                "ttl": meta.get("ttl"),
            }
        except aex.RecordNotFound:
            return None

    def create_user_session(self, username: str) -> dict:
        """Create a session directly in Aerospike for the given user.

        Invalidates any existing session for this user.
        Returns {"session_id": ..., "data": ...}.
        """
        import secrets

        session_id = secrets.token_hex(16)
        session_data = {"username": username}
        data_bytes = json.dumps(session_data).encode()

        self._invalidate_previous_session(username, session_id)
        self._client.put(self._key(session_id), {"data": data_bytes})
        return {"session_id": session_id, "data": session_data}

    def _remove_user_mapping(self, username: str, session_id: str) -> None:
        """Delete the user→session mapping only if it still points to this session."""
        try:
            _, _, bins = self._client.get(self._user_key(username))
            if bins.get("session_id") == session_id:
                self._client.remove(self._user_key(username))
        except aex.RecordNotFound:
            pass

    def close(self) -> None:
        """Close the Aerospike client connection."""
        self._client.close()

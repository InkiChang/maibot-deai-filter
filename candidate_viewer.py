"""Opt-in, read-only Unix socket view of pending in-memory candidates."""
from __future__ import annotations

import asyncio
import json
import os
import socket
import stat
import struct
import secrets
import hashlib
import hmac
from pathlib import Path
from typing import Any

from memory_store import MemoryStore

MAX_REQUEST = 8192
MAX_RESPONSE = 65536
MAX_CONNECTIONS = 2
MAX_REQUESTS_PER_CONNECTION = 10
REQUEST_TIMEOUT = 3.0


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _decode(raw: bytes):
    return json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_object,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid constant")))


def _item_detail(item: Any, candidate_id: str, message_ref: str) -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "domain": item.domain,
        "text": item.text,
        "quote": item.evidence_quotes[0] if item.evidence_quotes else "",
        "message_ref": message_ref,
        "created_at": item.created_at.isoformat(),
        "confidence": item.confidence,
        "sensitivity": item.sensitivity,
        "status": "pending",
    }


class CandidateViewer:
    """Async JSONL server; caller binds authorization to UIDs/GIDs and allowed sessions."""

    def __init__(self, store: MemoryStore, socket_path: str, *, allowed_sessions: list[str],
                 allowed_uids: list[int], allowed_gids: list[int], socket_gid: int | None = None):
        if not isinstance(store, MemoryStore):
            raise ValueError("store required")
        if (not isinstance(socket_path, str) or not socket_path
                or not Path(socket_path).is_absolute()):
            raise ValueError("absolute socket path required")
        if (not isinstance(allowed_sessions, list) or not allowed_sessions
                or any(not isinstance(x, str) or not x for x in allowed_sessions)):
            raise ValueError("allowed session IDs required")
        if (not isinstance(allowed_uids, list) or not isinstance(allowed_gids, list)
                or (not allowed_uids and not allowed_gids)):
            raise ValueError("at least one allowed peer UID/GID required")
        if any(type(x) is not int or x < 0 for x in allowed_uids + allowed_gids):
            raise ValueError("invalid UID/GID")
        self.store = store
        self.path = Path(socket_path)
        self.allowed_sessions = frozenset(allowed_sessions)
        self.allowed_uids = frozenset(allowed_uids)
        self.allowed_gids = frozenset(allowed_gids)
        if socket_gid is not None and (type(socket_gid) is not int or socket_gid < 0):
            raise ValueError("invalid socket GID")
        self.socket_gid = socket_gid
        self.server: asyncio.AbstractServer | None = None
        self._socket_identity: tuple[int, int] | None = None
        self._tasks: set[asyncio.Task] = set()
        self._writers: set[asyncio.StreamWriter] = set()
        self._secret = secrets.token_bytes(32)

    def _prepare_path(self) -> None:
        # All mutable path components must be controlled by root or this service UID.
        # Root-owned sticky /tmp is allowed as an ancestor, never as the socket parent.
        if ".." in self.path.parts:
            raise RuntimeError("unsafe socket path")
        uid = os.geteuid()
        for parent in reversed(self.path.parents):
            st = parent.lstat()
            if not stat.S_ISDIR(st.st_mode) or st.st_uid not in {0, uid}:
                raise RuntimeError("unsafe socket ancestor")
            writable = st.st_mode & 0o022
            sticky_root = bool(st.st_mode & stat.S_ISVTX) and st.st_uid == 0
            if writable and (parent == self.path.parent or not sticky_root):
                raise RuntimeError("writable socket ancestor")
        st = self.path.parent.lstat()
        if st.st_mode & 0o007 or st.st_uid != uid:
            raise RuntimeError("socket directory must be service-owned and private")
        # Fail closed for *all* occupied paths, including stale sockets. Never unlink
        # another process's pathname. An administrator must verify/remove stale paths.
        try:
            self.path.lstat()
        except FileNotFoundError:
            return
        raise RuntimeError("socket path occupied")

    async def start(self) -> None:
        if not hasattr(socket, "SO_PEERCRED") or not hasattr(asyncio, "start_unix_server"):
            raise RuntimeError("Linux Unix sockets with SO_PEERCRED required")
        if self.server is not None:
            raise RuntimeError("viewer already started")
        self._prepare_path()
        raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            raw.bind(str(self.path))
            current = self.path.lstat()
            self._socket_identity = (current.st_dev, current.st_ino)
            if self.socket_gid is not None:
                os.chown(self.path, -1, self.socket_gid, follow_symlinks=False)
            os.chmod(self.path, 0o660, follow_symlinks=False)
            current = self.path.lstat()
            if (not stat.S_ISSOCK(current.st_mode)
                    or (current.st_dev, current.st_ino) != self._socket_identity
                    or stat.S_IMODE(current.st_mode) != 0o660
                    or current.st_uid != os.geteuid()
                    or (self.socket_gid is not None and current.st_gid != self.socket_gid)):
                raise RuntimeError("socket permissions did not apply")
            raw.setblocking(False)
            self.server = await asyncio.start_unix_server(
                self._accept, sock=raw, limit=MAX_REQUEST, start_serving=False)
            await self.server.start_serving()
        except BaseException:
            raw.close()
            await self.stop()
            raise

    async def stop(self) -> None:
        server, self.server = self.server, None
        if server is not None:
            server.close()
            await server.wait_closed()
        for writer in tuple(self._writers):
            writer.close()
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._socket_identity is not None:
            try:
                current = self.path.lstat()
                if (stat.S_ISSOCK(current.st_mode)
                        and (current.st_dev, current.st_ino) == self._socket_identity):
                    self.path.unlink()
            except FileNotFoundError:
                pass
            finally:
                self._socket_identity = None

    def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # Synchronous admission prevents an unbounded semaphore waiter queue.
        if self.server is None or len(self._tasks) >= MAX_CONNECTIONS or not self._authorized(writer):
            writer.close()
            return
        self._writers.add(writer)
        task = asyncio.create_task(self._handle(reader, writer))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _authorized(self, writer: asyncio.StreamWriter) -> bool:
        sock = writer.get_extra_info("socket")
        if sock is None or not hasattr(socket, "SO_PEERCRED"):
            return False
        try:
            pid, uid, gid = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        except (OSError, ValueError):
            return False
        del pid
        return uid in self.allowed_uids or gid in self.allowed_gids

    def _ref(self, *parts: str) -> str:
        data = json.dumps(parts, ensure_ascii=False).encode("utf-8")
        return hmac.new(self._secret, data, hashlib.sha256).hexdigest()[:32]

    def _candidate_id(self, item: Any) -> str:
        return self._ref("candidate", item.scope.owner_id, item.scope.session_id, item.item_id)

    def _pending_items(self) -> tuple[Any, ...]:
        # Session allowlist is fixed by server configuration, never supplied by clients.
        return self.store.pending_for_sessions(frozenset(self.allowed_sessions))

    def _dispatch(self, request: Any) -> dict[str, Any]:
        if not isinstance(request, dict) or type(request.get("version")) is not int or request.get("version") != 1:
            return {"ok": False, "error": "invalid_request"}
        op = request.get("op")
        if op == "list_pending" and set(request) <= {"version", "op", "limit", "cursor"}:
            if request.get("cursor") is not None:
                return {"ok": False, "error": "invalid_request"}
            limit = request.get("limit", 20)
            if type(limit) is not int or not 1 <= limit <= 20:
                return {"ok": False, "error": "invalid_request"}
            items = self._pending_items()[:limit]
            return {"ok": True, "items": [{
                "candidate_id": self._candidate_id(item), "domain": item.domain,
                "created_at": item.created_at.isoformat(), "confidence": item.confidence,
                "source": self._ref("source", item.scope.owner_id, item.scope.session_id),
            } for item in items]}
        if op == "get_pending" and set(request) == {"version", "op", "candidate_id"}:
            item_id = request.get("candidate_id")
            if not isinstance(item_id, str) or not item_id or len(item_id) > 128:
                return {"ok": False, "error": "invalid_request"}
            item = next((candidate for candidate in self._pending_items()
                         if self._candidate_id(candidate) == item_id), None)
            if item is None:
                return {"ok": False, "error": "not_found"}
            return {"ok": True, "item": _item_detail(
                item, self._candidate_id(item),
                self._ref("message", item.evidence_refs[0]) if item.evidence_refs else "")}
        return {"ok": False, "error": "invalid_request"}

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            for _ in range(MAX_REQUESTS_PER_CONNECTION):
                try:
                    raw = await asyncio.wait_for(reader.readline(), timeout=REQUEST_TIMEOUT)
                except (asyncio.TimeoutError, ValueError):
                    break
                if not raw:
                    break
                if len(raw) > MAX_REQUEST or not raw.endswith(b"\n"):
                    await self._send(writer, {"ok": False, "error": "invalid_request"})
                    break
                try:
                    payload = _decode(raw)
                    response = self._dispatch(payload)
                except (ValueError, RecursionError):
                    await self._send(writer, {"ok": False, "error": "invalid_request"})
                    break
                except Exception:
                    # Fail closed without disclosing Store exceptions or request data.
                    await self._send(writer, {"ok": False, "error": "unavailable"})
                    break
                if not await self._send(writer, response) or response.get("ok") is not True:
                    break
        except (ConnectionError, OSError):
            pass
        finally:
            self._writers.discard(writer)
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), REQUEST_TIMEOUT)
            except (OSError, asyncio.TimeoutError):
                pass

    async def _send(self, writer: asyncio.StreamWriter, payload: dict[str, Any]) -> bool:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(data) > MAX_RESPONSE:
            data = b'{"ok":false,"error":"response_too_large"}\n'
        writer.write(data)
        try:
            await asyncio.wait_for(writer.drain(), timeout=REQUEST_TIMEOUT)
            return True
        except (asyncio.TimeoutError, ConnectionError, OSError):
            return False


async def request(socket_path: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Client helper used by CLI and tests."""
    if not hasattr(asyncio, "open_unix_connection") or not hasattr(socket, "SO_PEERCRED"):
        raise RuntimeError("Linux Unix sockets with SO_PEERCRED required")
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(data) > MAX_REQUEST:
        raise ValueError("request too large")
    reader, writer = await asyncio.wait_for(
        asyncio.open_unix_connection(socket_path, limit=MAX_RESPONSE), REQUEST_TIMEOUT)
    try:
        writer.write(data)
        await asyncio.wait_for(writer.drain(), REQUEST_TIMEOUT)
        raw = await asyncio.wait_for(reader.readline(), REQUEST_TIMEOUT)
        if len(raw) > MAX_RESPONSE or not raw.endswith(b"\n"):
            raise RuntimeError("viewer returned no valid response")
        result = _decode(raw)
        if not isinstance(result, dict):
            raise RuntimeError("viewer returned invalid response")
        return result
    finally:
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), REQUEST_TIMEOUT)
        except (OSError, asyncio.TimeoutError):
            pass

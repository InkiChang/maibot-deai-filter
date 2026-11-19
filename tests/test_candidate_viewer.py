"""Read-only viewer scope, protocol, credentials and CLI regression tests."""
import asyncio
import contextlib
import io
import json
import os
import socket
import struct
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import test_plugin
from candidate_viewer import CandidateViewer, _decode, request
from candidate_viewer_cli import main
from memory_store import MemoryItem, MemoryStore, OwnerScope

LIST = {"version": 1, "op": "list_pending", "limit": 20, "cursor": None}
LINUX = hasattr(socket, "SO_PEERCRED") and hasattr(asyncio, "start_unix_server")


class ViewerTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.scope = OwnerScope("private-owner", "private-session")
        self.viewer = CandidateViewer(self.store, str(Path(tempfile.gettempdir()) / "view.sock"),
                                      allowed_sessions=[self.scope.session_id],
                                      allowed_uids=[1000], allowed_gids=[1001])

    def item(self, **kwargs):
        args = dict(scope=self.scope, domain="user_fact", text="prefers tea",
                    evidence_refs=("private-message",), evidence_quotes=("I prefer tea",),
                    confidence=0.9, created_at=datetime.now(timezone.utc))
        args.update(kwargs)
        return MemoryItem(**args)

    def test_scoped_pending_only_and_no_expired_sensitive_or_approved(self):
        live = self.item()
        for item in [live, self.item(scope=OwnerScope("other", "not-allowed")),
                     self.item(text="secret", sensitivity="sensitive"),
                     self.item(text="expired", created_at=datetime.now(timezone.utc)-timedelta(days=1), expires_at=datetime.now(timezone.utc)-timedelta(seconds=1))]:
            self.store.add(item)
        approved = self.item(text="approved")
        self.store.add(approved)
        self.store.approve(self.scope, approved.item_id, authorized_domains={"user_fact"})
        result = self.viewer._dispatch(LIST)
        self.assertEqual(len(result["items"]), 1)
        listing = json.dumps(result)
        for private in [live.text, self.scope.owner_id, self.scope.session_id, "private-message", live.item_id]:
            self.assertNotIn(private, listing)
        cid = result["items"][0]["candidate_id"]
        detail = self.viewer._dispatch({"version": 1, "op": "get_pending", "candidate_id": cid})
        self.assertEqual(detail["item"]["quote"], "I prefer tea")
        self.assertEqual(detail["item"]["text"], "prefers tea")
        self.assertNotIn("private-message", json.dumps(detail))
        self.assertNotIn(self.scope.session_id, json.dumps(detail))
        self.store.approve(self.scope, live.item_id, authorized_domains={"user_fact"})
        self.assertEqual(self.viewer._dispatch({"version": 1, "op": "get_pending", "candidate_id": cid})["error"], "not_found")

    def test_duplicate_store_ids_across_scopes_get_distinct_viewer_ids(self):
        other = OwnerScope("other-owner", self.scope.session_id)
        self.store.add(self.item(item_id="same"))
        self.store.add(self.item(scope=other, item_id="same", text="prefers coffee"))
        ids = [x["candidate_id"] for x in self.viewer._dispatch(LIST)["items"]]
        self.assertEqual(len(set(ids)), 2)
        texts = {self.viewer._dispatch({"version": 1, "op": "get_pending", "candidate_id": x})["item"]["text"] for x in ids}
        self.assertEqual(texts, {"prefers tea", "prefers coffee"})

    def test_rejects_mutation_extra_fields_and_wrong_types_without_mutating(self):
        self.store.add(self.item())
        before = self.store.pending_for_sessions(frozenset({self.scope.session_id}))
        for bad in [None, [], {**LIST, "op": "approve"}, {**LIST, "scope": "other"},
                    {**LIST, "version": True}, {**LIST, "version": 1.0}, {**LIST, "limit": True},
                    {**LIST, "limit": 21}, {**LIST, "cursor": "arbitrary"},
                    {"version": 1, "op": "get_pending", "candidate_id": []}]:
            with self.subTest(bad=bad):
                self.assertFalse(self.viewer._dispatch(bad)["ok"])
        self.assertEqual(before, self.store.pending_for_sessions(frozenset({self.scope.session_id})))

    def test_json_rejects_duplicates_invalid_utf8_and_nan(self):
        for raw in [b'{"version":1,"version":1}', b'{"x":NaN}', b'\xff', b'not json']:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                _decode(raw)

    def test_config_fails_closed(self):
        base = dict(allowed_sessions=["s"], allowed_uids=[1000], allowed_gids=[])
        for key, bad in [("allowed_sessions", "s"), ("allowed_sessions", []),
                         ("allowed_uids", [True]), ("allowed_uids", None), ("socket_gid", True)]:
            with self.subTest(key=key, bad=bad), self.assertRaises(ValueError):
                CandidateViewer(self.store, str(Path(tempfile.gettempdir()) / "x"), **{**base, key: bad})
        with self.assertRaises(ValueError):
            CandidateViewer(self.store, "relative.sock", **base)

    def test_peercred_uid_or_primary_gid_and_fail_closed(self):
        writer = Mock()
        sock = writer.get_extra_info.return_value
        with patch.object(socket, "SO_PEERCRED", 17, create=True):
            for uid, gid, expected in [(1000, 99, True), (99, 1001, True), (99, 99, False)]:
                sock.getsockopt.return_value = struct.pack("3i", 42, uid, gid)
                self.assertEqual(self.viewer._authorized(writer), expected)
            sock.getsockopt.side_effect = OSError()
            self.assertFalse(self.viewer._authorized(writer))
            writer.get_extra_info.return_value = None
            self.assertFalse(self.viewer._authorized(writer))

    def test_cli_success_denial_and_unavailable_exit_codes(self):
        for result, expected in [({"ok": True, "items": []}, 0), ({"ok": False, "error": "not_found"}, 1)]:
            with patch("candidate_viewer_cli.request", new=AsyncMock(return_value=result)), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["--socket", "x", "list"]), expected)
        with patch("candidate_viewer_cli.request", new=AsyncMock(side_effect=TimeoutError())), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--socket", "x", "get", "id"]), 2)


class ViewerPluginTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_off_and_invalid_config_does_not_break_load(self):
        inst = test_plugin.mod.DeAiFilterPlugin()
        inst.config = {"memory_viewer": ["invalid"]}
        await inst.on_load()
        self.assertIsNone(inst._candidate_viewer)
        await inst.on_unload()

    async def test_lifecycle_load_update_unload(self):
        inst = test_plugin.mod.DeAiFilterPlugin()
        inst.config = {"memory_capture": {"enabled": True, "model_task_name": "task", "allowed_targets": ["s"]},
                       "memory_viewer": {"enabled": True, "socket_path": str(Path(tempfile.gettempdir()) / "x"), "allowed_uids": [1000], "allowed_gids": []}}
        fake = Mock(start=AsyncMock(), stop=AsyncMock())
        with patch("plugin.CandidateViewer", return_value=fake):
            await inst.on_load()
            self.assertIs(inst._candidate_viewer, fake)
            await inst.on_config_update("self", {}, "2")
            self.assertEqual(fake.start.await_count, 2)
            self.assertEqual(fake.stop.await_count, 1)
            await inst.on_unload()
            self.assertEqual(fake.stop.await_count, 2)
            self.assertEqual(inst._capture_store.count(), 0)

    async def test_start_failure_leaves_capture_worker_operational(self):
        inst = test_plugin.mod.DeAiFilterPlugin()
        inst.config = {"memory_capture": {"enabled": True, "model_task_name": "task", "allowed_targets": ["s"]},
                       "memory_viewer": {"enabled": True, "socket_path": str(Path(tempfile.gettempdir()) / "x"), "allowed_uids": [1000], "allowed_gids": []}}
        fake = Mock(start=AsyncMock(side_effect=RuntimeError("failure")))
        with patch("plugin.CandidateViewer", return_value=fake):
            await inst.on_load()
            self.assertIsNone(inst._candidate_viewer)
            self.assertFalse(inst._capture_worker.done())
            await inst.on_unload()


@unittest.skipUnless(LINUX, "requires Linux SO_PEERCRED")
class UnixViewerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mai-view-")
        self.path = Path(self.tmp.name) / "view.sock"
        self.store = MemoryStore()
        self.item = MemoryItem(scope=OwnerScope("owner", "s"), domain="user_fact", text="tea",
                               evidence_refs=("msg",), evidence_quotes=("tea",), confidence=0.9,
                               created_at=datetime.now(timezone.utc))
        self.store.add(self.item)
        self.viewer = CandidateViewer(self.store, str(self.path), allowed_sessions=["s"],
                                      allowed_uids=[os.getuid()], allowed_gids=[], socket_gid=os.getgid())
        await self.viewer.start()
        self.clients = []

    async def asyncTearDown(self):
        for writer in self.clients:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
        await self.viewer.stop()
        self.tmp.cleanup()

    async def connect(self):
        reader, writer = await asyncio.open_unix_connection(str(self.path))
        self.clients.append(writer)
        return reader, writer

    async def test_roundtrip_cli_and_permissions(self):
        import sys
        import stat
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o660)
        listing = await request(str(self.path), LIST)
        cid = listing["items"][0]["candidate_id"]
        detail = await request(str(self.path), {"version": 1, "op": "get_pending", "candidate_id": cid})
        self.assertEqual(detail["item"]["text"], "tea")
        for op in [["list"], ["get", cid]]:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, str(Path(__file__).resolve().parents[1] / "candidate_viewer_cli.py"),
                "--socket", str(self.path), *op,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, err = await asyncio.wait_for(proc.communicate(), 5)
            self.assertEqual(proc.returncode, 0, err)
            self.assertTrue(json.loads(out)["ok"])

    async def test_actual_peercred_denies_uid_not_on_allowlist(self):
        self.viewer.allowed_uids = frozenset({os.getuid()+100000})
        reader, writer = await self.connect()
        try:
            writer.write(json.dumps(LIST).encode()+b"\n")
            await writer.drain()
            self.assertEqual(await asyncio.wait_for(reader.read(), 1), b"")
        except (ConnectionResetError, BrokenPipeError):
            pass
        self.assertEqual(self.store.count(), 1)

    async def test_gid_authorization_alternative(self):
        self.viewer.allowed_uids = frozenset()
        self.viewer.allowed_gids = frozenset({os.getgid()})
        self.assertTrue((await request(str(self.path), LIST))["ok"])

    async def test_invalid_json_unknown_fields_and_oversize_fail_closed(self):
        for raw in [b'{"version":1,"version":1}\n', b'{"version":1,"op":"delete"}\n',
                    b'{"version":true,"op":"list_pending"}\n', b'{"version":1,"op":"list_pending","scope":"other"}\n',
                    b'\xff\n', b'x'*8193+b'\n']:
            reader, writer = await self.connect()
            writer.write(raw)
            await writer.drain()
            response = await asyncio.wait_for(reader.readline(), 1)
            if response:
                self.assertFalse(json.loads(response)["ok"])
                self.assertEqual(await asyncio.wait_for(reader.read(), 1), b"")
            writer.close()
            await asyncio.sleep(0.01)
        self.assertEqual(self.store.count(), 1)

    async def test_request_count_limit(self):
        reader, writer = await self.connect()
        for _ in range(10):
            writer.write(json.dumps(LIST).encode()+b"\n")
            await writer.drain()
            self.assertTrue(json.loads(await asyncio.wait_for(reader.readline(), 1))["ok"])
        self.assertEqual(await asyncio.wait_for(reader.read(), 1), b"")

    async def test_connection_limit_and_stop_closes_idle_connections(self):
        first = await self.connect()
        second = await self.connect()
        await asyncio.sleep(0.02)
        third = await self.connect()
        self.assertEqual(await asyncio.wait_for(third[0].read(), 1), b"")
        self.assertLessEqual(len(self.viewer._tasks), 2)
        await self.viewer.stop()
        for reader, _ in [first, second]:
            self.assertEqual(await asyncio.wait_for(reader.read(), 1), b"")
        self.assertFalse(self.path.exists())
        await self.viewer.stop()

    async def test_read_timeout_on_partial_request(self):
        with patch("candidate_viewer.REQUEST_TIMEOUT", 0.05):
            reader, writer = await self.connect()
            writer.write(b'{"version":')
            await writer.drain()
            self.assertEqual(await asyncio.wait_for(reader.read(), 1), b"")

    async def test_occupied_path_and_symlink_directory_not_removed(self):
        await self.viewer.stop()
        self.path.write_text("keep")
        with self.assertRaises(RuntimeError):
            await self.viewer.start()
        self.assertEqual(self.path.read_text(), "keep")
        self.path.unlink()
        link = Path(self.tmp.name) / "link"
        link.symlink_to(Path(self.tmp.name), target_is_directory=True)
        self.viewer.path = link / "view.sock"
        with self.assertRaises(RuntimeError):
            await self.viewer.start()
        self.assertTrue(link.is_symlink())

    async def test_failure_after_bind_cleans_socket_and_replacement_is_preserved(self):
        await self.viewer.stop()
        with patch("candidate_viewer.os.chmod", side_effect=OSError("fail")):
            with self.assertRaises(OSError):
                await self.viewer.start()
        self.assertFalse(self.path.exists())
        await self.viewer.start()
        self.path.unlink()
        self.path.write_text("replacement")
        await self.viewer.stop()
        self.assertEqual(self.path.read_text(), "replacement")

    async def test_live_or_stale_socket_and_unsafe_modes_refused(self):
        other = CandidateViewer(self.store, str(self.path), allowed_sessions=["s"],
                                allowed_uids=[os.getuid()], allowed_gids=[])
        with self.assertRaises(RuntimeError):
            await other.start()
        self.assertTrue((await request(str(self.path), LIST))["ok"])
        await self.viewer.stop()
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(self.path))
        stale.close()
        with self.assertRaises(RuntimeError):
            await self.viewer.start()
        self.assertTrue(self.path.exists())
        self.path.unlink()
        Path(self.tmp.name).chmod(0o777)
        with self.assertRaises(RuntimeError):
            await self.viewer.start()
        Path(self.tmp.name).chmod(0o700)


if __name__ == "__main__":
    unittest.main()

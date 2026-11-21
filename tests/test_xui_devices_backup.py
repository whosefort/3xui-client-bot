import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from bot import backup, runtime
from bot.panels.base import Client
from bot.panels.xui import XUIClient, XUIHTTPError


class HwidTests(unittest.IsolatedAsyncioTestCase):
    async def test_limit_update_does_not_send_other_client_fields(self):
        panel = XUIClient("https://xui.example/admin")
        panel.list_clients = AsyncMock(side_effect=[
            [Client("u/test", tg_id=23, raw={"limitHwid": 0, "id": 42})],
            [Client("u/test", raw={"limitHwid": 3})],
        ])
        panel._request = AsyncMock()
        await panel.set_hwid_limit("u/test", 3)
        panel._request.assert_awaited_once_with(
            "POST", "/panel/api/clients/update/u%2Ftest", json_body={"email": "u/test", "tgId": 23, "limitHwid": 3, "flow": ""})

    async def test_removing_one_device_has_scoped_path_and_verification(self):
        panel = XUIClient("https://xui.example/admin")
        panel.hwids = AsyncMock(side_effect=[[{"id": 5}, {"id": 7}], [{"id": 7}]])
        panel._request = AsyncMock()
        await panel.forget_hwids("alice", 5)
        panel._request.assert_awaited_once_with("DELETE", "/panel/api/clients/hwids/alice/5")
        self.assertEqual(panel.hwids.await_count, 2)

    async def test_binding_preserves_flow_without_sending_uuid_or_numeric_database_id(self):
        panel = XUIClient("https://xui.example/admin")
        panel._request = AsyncMock()
        await panel.bind_tgid(client=Client("alice", raw={"id": 5, "uuid": "test", "flow": "vision"}), tg_id=7)
        panel._request.assert_awaited_once_with(
            "POST", "/panel/api/clients/update/alice", json_body={"email": "alice", "tgId": 7, "flow": "vision"})


    async def test_different_flows_are_preserved_per_inbound(self):
        panel = XUIClient("https://xui.example/admin")
        panel._request = AsyncMock()
        panel.list_inbounds = AsyncMock(return_value=[
            {"id": 1, "settings": {"clients": [{"email": "alice", "flow": "xtls-rprx-vision"}]}},
            {"id": 2, "settings": {"clients": [{"email": "alice", "flow": ""}]}},
        ])
        await panel.bind_tgid(client=Client("alice", raw={"inboundIds": [1, 2], "expiryTime": 9000}),
                              tg_id=7)
        updates = panel._request.await_args_list
        self.assertEqual([call.args[1] for call in updates], [
            "/panel/api/clients/update/alice?inboundIds=1",
            "/panel/api/clients/update/alice?inboundIds=2",
        ])
        self.assertEqual([call.kwargs["json_body"]["flow"] for call in updates], ["xtls-rprx-vision", ""])
        self.assertTrue(all(call.kwargs["json_body"]["expiryTime"] == 9000 for call in updates))

    async def test_traffic_reset_uses_panel_wide_client_api_with_fallback_for_old_versions(self):
        panel = XUIClient("https://xui.example/admin")
        panel._request = AsyncMock(side_effect=[XUIHTTPError(404), {}])
        await panel.reset_traffic(client=Client("alice", raw={"inboundIds": [8]}))
        self.assertEqual([call.args for call in panel._request.await_args_list], [
            ("POST", "/panel/api/clients/resetTraffic/alice"),
            ("POST", "/panel/api/inbounds/8/resetClientTraffic/alice"),
        ])


class BackupTests(unittest.IsolatedAsyncioTestCase):
    async def _snapshot(self, path):
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE inbounds(id INTEGER PRIMARY KEY)")
            conn.execute("INSERT INTO inbounds VALUES(1)")

    async def test_named_snapshot_is_encrypted_before_upload_and_date_updates_after_success(self):
        panel = Mock()
        panel.download_database = AsyncMock(side_effect=self._snapshot)
        with tempfile.TemporaryDirectory() as tmp:
            def encrypt(path, *, required):
                self.assertTrue(required)
                target = path + ".age"
                Path(target).write_bytes(b"ciphertext")
                return target, True

            with patch.object(runtime, "xui_panels", {"bern": ("Bern", panel)}), \
                    patch.object(backup, "_maybe_encrypt", side_effect=encrypt), \
                    patch.object(backup, "_upload") as upload, \
                    patch.object(backup.db, "set_setting") as setting:
                await backup._backup_xui("bern", tmp)
            uploaded, key = upload.call_args.args
            self.assertTrue(uploaded.endswith(".age"))
            self.assertRegex(key, r"^backups/[^/]+/xui/bern/\d+\.db\.age$")
            setting.assert_called_once()
            self.assertEqual(setting.call_args.args[0], "xui_backup_at:bern")

    async def test_named_snapshot_never_uploads_if_encryption_fails(self):
        panel = Mock()
        panel.download_database = AsyncMock(side_effect=self._snapshot)
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(runtime, "xui_panels", {"bern": ("Bern", panel)}), \
                patch.object(backup, "_maybe_encrypt", side_effect=ValueError("invalid key")), \
                patch.object(backup, "_upload") as upload, patch.object(backup.db, "set_setting") as setting:
            with self.assertRaises(ValueError):
                await backup._backup_xui("bern", tmp)
            upload.assert_not_called()
            setting.assert_not_called()

    def test_postgresql_dump_and_invalid_response_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / "data"
            file.write_bytes(b"PGDMP" + bytes(20))
            self.assertEqual(backup._validate_xui_backup(file), "dump")
            file.write_bytes(b"<html>Login</html>")
            with self.assertRaises(ValueError):
                backup._validate_xui_backup(file)

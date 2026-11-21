import hashlib
import sqlite3
import tempfile
from copy import deepcopy
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import db, runtime
from bot.config import config
from bot.handlers import admin, common
from bot.keyboards import grant_without_id_kb
from bot.panels.base import Client
from bot.panels.ownership import locate_subscription
from bot.panels.xui import XUIClient
from bot.panels.xui_infra import add_node, add_reality_preset, set_reality_target


class MixedWorkflowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db.init(str(Path(tmp.name) / "bot.db"))
        self.default = SimpleNamespace(find_by_tgid=AsyncMock(return_value=None),
                                       find_by_username=AsyncMock(return_value=None),
                                       find_by_subid=AsyncMock(return_value=None))
        self.named = XUIClient("https://xui.example/admin", sub_url_template="https://sub.example/x/{subId}")
        self.named.find_by_tgid = AsyncMock(return_value=None)
        self.named.find_by_username = AsyncMock(return_value=None)
        self.named.find_by_subid = AsyncMock(return_value=None)
        self.named.list_inbounds = AsyncMock(return_value=[
            {"id": 5, "port": 443, "enable": True, "protocol": "vless",
             "streamSettings": {"network": "tcp", "security": "reality"}},
            {"id": 9, "port": 8443, "enable": True, "protocol": "vless",
             "streamSettings": {"network": "xhttp", "security": "tls"}},
        ])
        self.named.create_client = AsyncMock(side_effect=lambda **kw: Client(
            f"u{kw['tg_id']}", tg_id=kw["tg_id"], sub_url=self.named._sub_url("known-sub")))
        self.addCleanup(patch.stopall)
        patch.object(runtime, "panel", self.default).start()
        patch.object(runtime, "xui_panels", {"entry": ("Entry", self.named)}).start()

    def tearDown(self):
        db._c().close()

    async def test_approved_named_issue_controls_status_and_renewal(self):
        req_id = db.create_request(100, None, "new")
        self.assertTrue(db.claim_request(req_id, panel_key="entry", inbound_id=9))
        await admin._approve_new(db.get_request(req_id))
        db.decide_request(req_id, "approved", 1)
        self.assertEqual(db.get_user(100)["panel_key"], "entry")
        self.assertEqual(self.named.create_client.await_args.kwargs["inbound_id"], 9)
        self.assertEqual(self.named.create_client.await_args.kwargs["flow"], "")
        client = Client("u100", tg_id=100, expire_ts=int(time.time()) + 3 * 86400,
                        sub_url=self.named._sub_url("known-sub"))
        self.named.find_by_username.return_value = client
        self.named.find_by_tgid.return_value = client
        self.named.extend_client = AsyncMock(return_value=client)
        self.named.reset_traffic = AsyncMock()
        self.assertIs(await common.resolve_client(100), client)
        req_id = db.create_request(100, None, "renew")
        self.assertEqual(db.get_request(req_id)["panel_key"], "entry")
        self.assertFalse(db.claim_request(req_id))
        self.assertTrue(db.claim_request(req_id, panel_key="entry"))
        await admin._approve_renew(db.get_request(req_id))
        self.named.extend_client.assert_awaited_once()
        self.named.reset_traffic.assert_awaited_once_with(client=client)
        self.default.find_by_username.assert_not_awaited()
        self.default.find_by_tgid.assert_not_awaited()

    async def test_existing_marzban_renewal_keeps_legacy_adapter_contract(self):
        client = Client("u120", tg_id=120, expire_ts=int(time.time()) + 86400)
        self.default.find_by_tgid.return_value = client
        self.default.extend_client = AsyncMock(return_value=client)
        db.save_client_binding(120, None, client.username, "https://legacy.example/sub/token")
        req_id = db.create_request(120, None, "renew")
        self.assertTrue(db.claim_request(req_id))
        await admin._approve_renew(db.get_request(req_id))
        self.default.extend_client.assert_awaited_once_with(
            client=client, add_days=config.plan_days, set_total_gb=config.plan_traffic_gb,
            reset_traffic=True,
        )
        self.assertFalse(db.get_request(req_id)["replay_safe"])
        self.named.find_by_tgid.assert_not_awaited()

    async def test_pinned_new_request_cannot_retry_on_another_panel_or_inbound(self):
        req_id = db.create_request(200, None, "new")
        self.assertTrue(db.claim_request(req_id, panel_key="entry", inbound_id=5))
        db.release_request(req_id)
        self.assertFalse(db.claim_request(req_id, panel_key="other", inbound_id=5))
        self.assertFalse(db.claim_request(req_id, panel_key="entry", inbound_id=9))
        self.assertTrue(db.claim_request(req_id, panel_key="entry", inbound_id=5))

    async def test_lookup_and_binding_scope_do_not_evict_marzban_client_with_same_login(self):
        db.save_client_binding(1, None, "shared", "https://default.example/sub/legacy")
        client = Client("shared", sub_url=self.named._sub_url("known-sub"))
        self.named.find_by_subid.return_value = client
        self.named.bind_tgid = AsyncMock()
        panel, found = await locate_subscription(client.sub_url, "known-sub")
        self.assertIs(panel, self.named)
        self.default.find_by_subid.assert_not_awaited()
        await common.bind_client(panel, found, 2)
        self.assertEqual(db.get_user(1)["client_email"], "shared")
        self.assertEqual(db.get_user(1)["panel_key"], "")
        self.assertEqual(db.get_user(2)["panel_key"], "entry")

    def test_invitations_are_scoped_by_panel(self):
        first = db.create_client_invite("shared")
        second = db.create_client_invite("shared", "entry")
        self.assertEqual(db.claim_client_invite(first, 7), ("", "shared"))
        self.assertEqual(db.claim_client_invite(second, 8), ("entry", "shared"))
        db.save_client_binding(8, None, "shared", self.named._sub_url("known-sub"), "entry")
        self.assertIsNone(db.claim_client_invite(second, 8))
        self.assertEqual(db.claim_client_invite(first, 7), ("", "shared"))

    def test_legacy_invitation_survives_migration_to_scoped_keys(self):
        path = Path(db._c().execute("PRAGMA database_list").fetchone()[2]).with_name("legacy-invites.db")
        token = "legacy-test-token"
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE client_invites (client_username TEXT PRIMARY KEY, token_hash TEXT "
                         "NOT NULL UNIQUE, expires_at INTEGER NOT NULL, claimed_by INTEGER)")
            conn.execute("INSERT INTO client_invites VALUES(?,?,?,NULL)",
                         ("shared", hashlib.sha256(token.encode()).hexdigest(), int(time.time()) + 3600))
        db._c().close()
        db.init(str(path))
        named = db.create_client_invite("shared", "entry")
        self.assertEqual(db.claim_client_invite(token, 7), ("", "shared"))
        self.assertEqual(db.claim_client_invite(named, 8), ("entry", "shared"))

    def test_grant_menu_exposes_named_panels(self):
        callbacks = {b.callback_data for row in grant_without_id_kb(show_xui=True).inline_keyboard for b in row}
        self.assertEqual(callbacks, {"grant:noid", "xu:home"})


class PresetTests(unittest.IsolatedAsyncioTestCase):
    async def test_reality_auto_uses_valid_target_and_couples_sni(self):
        panel = XUIClient("https://panel.example/admin")
        generated = {}

        async def request(method, path, **kwargs):
            if path.endswith("scanRealityTarget"):
                usable = kwargs["form"]["sni"] == "www.cisco.com"
                return {"obj": {"feasible": usable, "certValid": usable, "tls13": usable, "h2": usable}}
            if path.endswith("getNewX25519Cert"):
                return {"obj": {"privateKey": "private", "publicKey": "public"}}
            self.assertTrue(path.endswith("/inbounds/add"))
            generated.update(kwargs["json_body"], id=30)
            return {"obj": {"id": 30}}

        panel._request = AsyncMock(side_effect=request)
        panel.list_inbounds = AsyncMock(side_effect=[[{"port": 24443}], [{"port": 24443}, generated]])
        result = await add_reality_preset(panel, "auto")
        reality = generated["streamSettings"]["realitySettings"]
        self.assertEqual(result["port"], 24444)
        self.assertEqual(reality["target"], "www.cisco.com:443")
        self.assertEqual(reality["serverNames"], ["www.cisco.com"])
        self.assertRegex(reality["shortIds"][0], r"^[0-9a-f]{16}$")

    async def test_node_registration_uses_scope_specific_token_and_deduplicates(self):
        source = XUIClient("https://source.example/admin")
        target = XUIClient("https://node.example:444/path", api_token="admin", node_token="node")
        source._request = AsyncMock(side_effect=[
            {"obj": []}, {"obj": {"status": "online"}}, {"obj": {"id": 1}},
            {"obj": [{"id": 1, "address": "node.example", "port": 444, "basePath": "/path"}]},
        ])
        self.assertEqual((await add_node(source, target, "Exit"))["id"], 1)
        self.assertEqual((await add_node(source, target, "Exit"))["id"], 1)
        self.assertEqual(source._request.await_count, 4)
        self.assertEqual(source._request.await_args_list[2].kwargs["json_body"]["apiToken"], "node")


    async def test_reality_edit_updates_pair_and_preserves_credentials_and_listener(self):
        panel = XUIClient("https://panel.example/admin")
        original = {
            "id": 11, "listen": "127.0.0.1", "port": 443, "protocol": "vless",
            "settings": {"decryption": "none"},
            "streamSettings": {"network": "tcp", "security": "reality", "realitySettings": {
                "dest": "old.example:443", "serverNames": ["old.example"], "xver": 0,
                "privateKey": "preserve", "shortIds": ["1111"],
            }},
        }
        saved = deepcopy(original)

        async def request(method, path, **kwargs):
            if method == "GET":
                return {"obj": deepcopy(saved)}
            if path.endswith("scanRealityTarget"):
                return {"obj": {"feasible": True, "certValid": True, "h2": True, "tls13": True}}
            saved.update(kwargs["json_body"])
            return {"success": True}

        panel._request = AsyncMock(side_effect=request)
        self.assertEqual(await set_reality_target(panel, 11, "github"), "github.com")
        expected = deepcopy(original)
        expected["streamSettings"]["realitySettings"].update(dest="github.com:443", serverNames=["github.com"])
        self.assertEqual(saved, expected)

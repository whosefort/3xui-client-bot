import json
import os
import unittest
from copy import deepcopy
from unittest.mock import AsyncMock, patch

from bot.config import Config
from bot.panels.base import Client, PanelError
from bot.panels.xui import XUIClient
from bot.panels.xui_routes import _active_rules, exit_outbound, route_client, set_client_route


BASE = {
    "outbounds": [
        {"tag": "direct", "protocol": "freedom"},
        {"tag": "blocked", "protocol": "blackhole"},
        {"tag": "hungary", "protocol": "vless"},
    ],
    "routing": {"rules": [
        {"type": "field", "inboundTag": ["api"], "outboundTag": "api"},
        {"type": "field", "ip": ["geoip:private"], "outboundTag": "blocked"},
        {"type": "field", "user": ["alice", "bob"], "inboundTag": ["tcp"],
         "outboundTag": "hungary", "enabled": True},
        {"type": "field", "domain": ["example.com"], "outboundTag": "direct"},
    ]},
}


class PanelRegistryTests(unittest.TestCase):
    def test_management_panels_do_not_change_subscription_backend(self):
        rows = {
            "entry": {"name": "Entry", "url": "https://entry.example/admin", "token_env": "XUI_ENTRY_TOKEN"},
            "exit": {"name": "Exit", "url": "https://exit.example/admin", "token_env": "XUI_EXIT_TOKEN",
                     "exit_client": "relay"},
        }
        with patch.dict(os.environ, {"XUI_ENTRY_TOKEN": "test-1", "XUI_EXIT_TOKEN": "test-2"}):
            cfg = Config(panels=["marzban"], xui_managed_panels=json.dumps(rows))
            self.assertEqual(cfg.managed_xui()["exit"][3], "relay")
            self.assertTrue(cfg.uses("marzban"))
            self.assertFalse(cfg.uses("xui"))

    def test_plain_http_is_rejected(self):
        cfg = Config(xui_managed_panels=json.dumps({
            "bad": {"name": "Bad", "url": "http://example.com/admin", "token_env": "XUI_BAD_TOKEN"},
        }))
        with self.assertRaisesRegex(RuntimeError, "параметры"):
            cfg.managed_xui()


class RouteRulesTests(unittest.TestCase):
    def test_removes_only_one_client_from_existing_panel_rule(self):
        updated = route_client(BASE, "tcp", "alice", None)
        self.assertEqual(updated["routing"]["rules"][2]["user"], ["bob"])
        self.assertEqual(BASE["routing"]["rules"][2]["user"], ["alice", "bob"])
        self.assertEqual(updated["outbounds"], BASE["outbounds"])

    def test_starts_after_essential_rules_and_keeps_default_outbound(self):
        updated = route_client(BASE, "tcp", "other", "hungary")
        rules = updated["routing"]["rules"]
        self.assertEqual(rules[2], {"type": "field", "inboundTag": ["tcp"],
                                    "user": ["other"], "outboundTag": "hungary"})
        self.assertEqual(updated["outbounds"][0]["tag"], "direct")

    def test_no_change_for_already_assigned_client(self):
        self.assertEqual(route_client(BASE, "tcp", "alice", "hungary"), BASE)

    def test_runtime_rule_normalization(self):
        original = BASE["routing"]["rules"][2]
        self.assertNotIn("enabled", _active_rules(BASE)[2])
        cfg = deepcopy(BASE)
        cfg["routing"]["rules"][2] = {**original, "enabled": False}
        self.assertEqual(len(_active_rules(cfg)), 3)


class ManagedRoutesTests(unittest.IsolatedAsyncioTestCase):
    def panel(self):
        panel = XUIClient("https://panel.example/admin")
        panel.list_inbounds = AsyncMock(return_value=[
            {"id": 1, "tag": "tcp", "settings": {"clients": [{"email": "other"}]}},
        ])
        panel.save_xray_template = AsyncMock()
        panel.restart_xray = AsyncMock()
        return panel

    async def test_rolls_back_when_restart_fails(self):
        panel = self.panel()
        proposed = route_client(BASE, "tcp", "other", "hungary")
        panel.xray_template = AsyncMock(side_effect=[(BASE, "url"), (proposed, "url"), (proposed, "url")])
        panel.restart_xray.side_effect = [PanelError("restart failed"), None]
        with patch("bot.panels.xui_routes._wait_for_routes", new_callable=AsyncMock) as wait:
            with self.assertRaisesRegex(PanelError, "Прежние правила проверены"):
                await set_client_route(panel, 1, "other", "hungary")
            wait.assert_awaited_once_with(panel, BASE)
        self.assertEqual(panel.save_xray_template.await_args_list[-1].args, (BASE, "url"))

    async def test_pulls_exit_profile_without_manual_link(self):
        panel = XUIClient("https://panel.example/admin", exit_client="relay")
        panel.find_by_username = AsyncMock(return_value=Client("relay"))
        panel.client_links = AsyncMock(return_value=[
            "vless://test-id@vpn.example:443?security=reality&type=tcp&"
            "pbk=sample&sni=www.example.org&sid=abcd&flow=xtls-rprx-vision",
        ])
        outbound = await exit_outbound(panel, "budapest")
        self.assertEqual(outbound["tag"], "exit-budapest")
        self.assertEqual(outbound["settings"]["vnext"][0]["address"], "vpn.example")
        self.assertEqual(outbound["streamSettings"]["realitySettings"]["password"], "sample")

    async def test_create_client_targets_selected_inbound_with_no_vision(self):
        panel = XUIClient("https://panel.example/admin", inbound_ids=[5])
        panel._request = AsyncMock(return_value={"success": True})
        result = await panel.create_client(tg_id=123, days=30, traffic_gb=10, inbound_id=9, flow="")
        self.assertEqual(result.username, "u123")
        body = panel._request.await_args.kwargs["json_body"]
        self.assertEqual(body["inboundIds"], [9])
        self.assertEqual(body["client"]["flow"], "")
        self.assertIn("id", body["client"])

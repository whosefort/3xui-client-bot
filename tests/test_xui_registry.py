import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from cryptography.fernet import Fernet

from bot import db, runtime
from bot.panels import xui_registry
from bot.panels.base import Client, PanelError
from bot.panels.xui import XUIClient


class RegistryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        db.init(str(Path(directory) / "bot.db"))
        self.addCleanup(db._c().close)
        self.enterContext(patch.object(runtime, "xui_panels", {}))
        self.enterContext(patch.dict(os.environ, {
            "XUI_TEST_TOKEN": "test-token",
            "XUI_REGISTRY_KEY": Fernet.generate_key().decode(),
        }))

    async def test_register_and_reload_without_plaintext(self):
        with patch.object(XUIClient, "server_status", new_callable=AsyncMock), \
             patch.object(XUIClient, "list_inbounds", new_callable=AsyncMock), \
             patch.object(XUIClient, "list_clients", new_callable=AsyncMock):
            await xui_registry.register("ready", "Ready", "https://ready.example/panel",
                                        "https://ready.example/sub/{subId}", "XUI_TEST_TOKEN")
            await xui_registry.register("fresh", "Fresh", "https://fresh.example/panel",
                                        "https://fresh.example/sub/{subId}", generated_token="generated-test")
        snapshot = {}
        xui_registry.load(snapshot)
        self.assertEqual(snapshot["ready"][1].api_token, "test-token")
        self.assertEqual(snapshot["fresh"][1].api_token, "generated-test")
        self.assertNotIn("generated-test", db.get_setting("xui_panel_registry"))

    async def test_change_preserves_key_bindings_and_subscription_url(self):
        old = XUIClient("https://ready.example/old", api_token="token",
                        sub_url_template="https://ready.example/sub/{subId}")
        runtime.xui_panels["ready"] = "Ready", old
        url = "https://ready.example/sub/old-code"
        db.save_client_binding(77, None, "legacy", url, "ready")
        profile = Client("legacy", raw={"subId": "old-code"})
        old.list_clients = AsyncMock(return_value=[profile])
        with patch.object(XUIClient, "server_status", new_callable=AsyncMock), \
             patch.object(XUIClient, "list_inbounds", new_callable=AsyncMock), \
             patch.object(XUIClient, "list_clients", AsyncMock(return_value=[])):
            with self.assertRaises(PanelError):
                await xui_registry.change_address("ready", "https://ready.example/wrong")
            self.assertEqual(old.base, "https://ready.example/old")
        with patch.object(XUIClient, "server_status", new_callable=AsyncMock), \
             patch.object(XUIClient, "list_inbounds", new_callable=AsyncMock), \
             patch.object(XUIClient, "list_clients", AsyncMock(return_value=[profile])):
            await xui_registry.change_address("ready", "https://ready.example/new")
        self.assertEqual(runtime.panel_key_for(old), "ready")
        self.assertEqual(old.base, "https://ready.example/new")
        self.assertEqual(old.sub_url_template, "https://ready.example/sub/{subId}")
        self.assertEqual(db.get_user(77)["sub_id"], url)

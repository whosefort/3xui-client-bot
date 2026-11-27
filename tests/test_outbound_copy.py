import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from bot.panels import xui_infra as infra
from bot.panels.base import PanelError


def test_chain_dependencies_and_cycle():
    leaf = {"tag": "relay"}
    outer = {"tag": "outer", "streamSettings": {"sockopt": {"dialerProxy": "relay"}}}
    template = {"outbounds": [outer, leaf]}
    assert infra.outbound_chain(template, "outer") == [leaf, outer]
    leaf["proxySettings"] = {"tag": "outer"}
    with pytest.raises(PanelError):
        infra.outbound_chain(template, "outer")


def test_copy_is_atomic_on_collision_and_idempotent():
    async def check():
        panel = AsyncMock()
        panel.config_lock = asyncio.Lock()
        original = {"outbounds": [{"tag": "direct"}], "routing": {"rules": []}}
        panel.xray_template.return_value = (original, None)
        with patch.object(infra, "commit_template", new_callable=AsyncMock) as save:
            assert not await infra.copy_outbounds(panel, [{"tag": "direct"}])
            with pytest.raises(PanelError):
                await infra.copy_outbounds(panel, [{"tag": "first"}, {"tag": "direct", "protocol": "dns"}])
            save.assert_not_called()
            assert await infra.copy_outbounds(panel, [{"tag": "first"}])
            draft = save.await_args.args[2]
            assert draft["outbounds"] == [{"tag": "direct"}, {"tag": "first"}]
            assert draft["routing"] == original["routing"]
            assert original["outbounds"] == [{"tag": "direct"}]
    asyncio.run(check())

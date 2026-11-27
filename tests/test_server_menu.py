from unittest.mock import patch

from bot import keyboards, runtime
from bot.handlers.admin import _management_keyboard
from bot.handlers.xui_admin import _keyboard


def test_server_hub_is_available_in_xui_only_mode():
    buttons = [b.text for row in keyboards.admin_kb(show_xui=True).keyboard for b in row]
    assert buttons.count(keyboards.ADM_SERVERS) == 1
    assert keyboards.ADM_XUI not in buttons


def test_mixed_server_hub_links_to_each_backend():
    with patch.object(runtime, "xui_panels", {"bern": ("Bern", None)}), patch(
        "bot.handlers.admin._servers_available", return_value=True
    ):
        targets = [row[0].callback_data for row in _management_keyboard().inline_keyboard]
    assert targets == ["srv:list", "xu:p:bern:0", "xu:register", "xu:bootstrap"]
    assert _keyboard([("Назад", "srv:home")]).inline_keyboard[0][0].callback_data == "srv:home"

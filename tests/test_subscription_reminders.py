from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from bot import db, runtime, scheduler


class SubscriptionReminderTests(IsolatedAsyncioTestCase):
    async def test_expiry_day_is_distinct_and_only_active_clients_get_it(self):
        with TemporaryDirectory() as directory:
            db.init(str(Path(directory) / "bot.db"))
            self.addCleanup(db._c().close)
            expires = datetime(2026, 12, 21, 23, tzinfo=scheduler._moscow)
            clients = []
            for tg_id, status in [(11, "active"), (12, "paused"), (13, "limited")]:
                name = f"u{tg_id}"
                db.save_client_binding(tg_id, None, name, "https://example.org/sub")
                clients.append(SimpleNamespace(username=name, tg_id=tg_id, access_status=status,
                                               expire_ts=expires.timestamp()))
            bot = SimpleNamespace(send_message=AsyncMock())
            panel = SimpleNamespace(list_clients=AsyncMock(return_value=clients))
            with patch.object(runtime, "bot", bot), patch.object(runtime, "panel", panel), \
                 patch.object(scheduler.config, "remind_days_before", [3, 1]), \
                 patch.object(scheduler.dt, "datetime", wraps=datetime) as clock:
                clock.now.return_value = datetime(2026, 12, 20, 23, 30, tzinfo=scheduler._moscow)
                await scheduler.reminders_sweep()
                self.assertIn("через <b>1 день", bot.send_message.await_args.args[1])
                self.assertFalse(db.already_reminded(11, "2026-12-21", 0))
                clock.now.return_value = datetime(2026, 12, 21, 11, tzinfo=scheduler._moscow)
                await scheduler.reminders_sweep(expiry_only=True)
                await scheduler.reminders_sweep()
                self.assertEqual([c.args[0] for c in bot.send_message.await_args_list], [11, 11])
                self.assertIn("Сегодня", bot.send_message.await_args.args[1])

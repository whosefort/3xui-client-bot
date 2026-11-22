from datetime import date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from bot import db, private_billing as billing, runtime


class PrivateBillingTests(IsolatedAsyncioTestCase):
    def setUp(self):
        root = self.enterContext(TemporaryDirectory())
        db.init(str(Path(root) / "bot.db"))
        self.addCleanup(db._c().close)
        self.bot = SimpleNamespace(send_message=AsyncMock())
        self.enterContext(patch.object(runtime, "bot", self.bot))
        self.ident = db.save_private_billing(123456789, "Test server", 880, "2026-12-21", 21, None)

    async def run_day(self, day):
        with patch.object(billing, "now", return_value=datetime(2026, 12, day, 23)):
            await billing.sweep()
            await billing.sweep()

    async def test_independent_schedule_and_manual_payment(self):
        db.save_client_binding(123456789, "test", "legacy", "https://example.org/legacy")
        db.set_setting("requisites", "Тестовые реквизиты")
        for day in (17, 18, 19, 20, 21, 22):
            await self.run_day(day)
        texts = [c.args[1] for c in self.bot.send_message.await_args_list]
        self.assertEqual(len(texts), 3)
        self.assertIn("через 3 дня", texts[0])
        self.assertIn("завтра", texts[1])
        self.assertTrue(texts[2].startswith("Платёж"))
        self.assertTrue(all("880 ₽" in text and "Тестовые реквизиты" in text for text in texts))
        self.assertTrue(db.mark_private_paid(self.ident, "2026-12-21", 880, "2027-01-21"))
        self.assertFalse(db.mark_private_paid(self.ident, "2026-12-21", 880, "2027-01-21"))
        await self.run_day(22)
        self.assertEqual(self.bot.send_message.await_count, 4)
        self.assertIn("21.01.2027", self.bot.send_message.await_args.args[1])
        self.assertEqual(db.get_user(123456789)["client_email"], "legacy")

    async def test_early_payment_suppresses_old_reminders(self):
        db.mark_private_paid(self.ident, "2026-12-21", 880, "2027-01-21")
        for day in (18, 20, 21):
            await self.run_day(day)
        self.bot.send_message.assert_awaited_once()

    def test_short_month_keeps_billing_day(self):
        feb = billing.next_month(date(2027, 1, 31), 31)
        self.assertEqual(feb, date(2027, 2, 28))
        self.assertEqual(billing.next_month(feb, 31), date(2027, 3, 31))

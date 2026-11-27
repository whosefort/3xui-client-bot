import pytest

from bot import db


def test_secondary_binding_is_idempotent_and_keeps_primary(tmp_path):
    db.init(str(tmp_path / "bot.db"))
    db.save_client_binding(10, "person", "primary", "old-url")
    original = dict(db.get_user(10))
    db.save_additional_subscription(10, "bern", "person")
    db.save_additional_subscription(10, "bern", "person")
    assert len(db.additional_subscriptions(10)) == 1
    with pytest.raises(ValueError):
        db.save_additional_subscription(20, "bern", "person")
    assert not db.additional_subscriptions(20)
    assert dict(db.get_user(10)) == original
    db.clear_panel_client("bern", "person")
    assert not db.additional_subscriptions(10)
    assert dict(db.get_user(10)) == original

"""Web UI tests (Streamlit AppTest): password gate and daily budget limit. No model calls."""
import os
import time

import pytest
from streamlit.testing.v1 import AppTest

from verified_lit_review import service
from verified_lit_review.db import Store

APP = os.path.join(os.path.dirname(__file__), "..", "app.py")


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = str(tmp_path / "ui.db")
    monkeypatch.setattr(service, "DB_PATH", db)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-used")
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    monkeypatch.delenv("ACCESS_CODES", raising=False)
    monkeypatch.setenv("DAILY_RUN_LIMIT", "2")
    return db


def labels(at):
    return [b.label for b in at.button]


def test_password_gate_blocks_until_correct_password(env, monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "s3cret")
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception
    assert "▶️ הרץ סקירה" not in labels(at)
    at.text_input[0].input("wrong").run()
    assert "▶️ הרץ סקירה" not in labels(at) and at.error[0].value == "קוד שגוי"
    at.text_input[0].input("s3cret").run()
    assert "▶️ הרץ סקירה" in labels(at)


def test_personal_code_shows_remaining_runs_and_blocks_when_used_up(env, monkeypatch):
    monkeypatch.setenv("ACCESS_CODES", "sheba-7k2q:2, other-1:5")
    monkeypatch.setenv("DAILY_RUN_LIMIT", "100")
    at = AppTest.from_file(APP, default_timeout=30).run()
    at.text_input[0].input("sheba-7k2q").run()
    assert "▶️ הרץ סקירה" in labels(at)
    assert any("2/2" in c.value for c in at.caption)

    store = Store(env)
    store.start_run("q1", access_code="sheba-7k2q")
    store.start_run("q2", access_code="sheba-7k2q")
    store.start_run("q3", access_code="other-1")
    store.close()
    at.run()
    assert "▶️ הרץ סקירה" not in labels(at)
    assert "ניצלת" in at.warning[0].value


def test_parse_access_codes():
    assert service.parse_access_codes("a-1:5, b-2 ,c-3:x") == {"a-1": 5, "b-2": 3, "c-3": 3}
    assert service.parse_access_codes(None) == {}


def test_daily_limit_hides_run_button(env):
    store = Store(env)
    for _ in range(2):
        store.start_run("q")
    store.close()
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception
    assert "▶️ הרץ סקירה" not in labels(at)
    assert "תקרת הריצות" in at.warning[0].value


def test_runs_since_counts_only_recent_runs(env):
    store = Store(env)
    old = store.start_run("old")
    store.conn.execute("UPDATE runs SET started_at = 0 WHERE run_id = ?", (old,))
    store.conn.commit()
    store.start_run("new")
    store.close()
    assert service.runs_since(time.time() - 86400) == 1

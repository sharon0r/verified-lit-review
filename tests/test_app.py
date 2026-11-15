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
    monkeypatch.setenv("DAILY_RUN_LIMIT", "2")
    return db


def labels(at):
    return [b.label for b in at.button]


def test_password_gate_blocks_until_correct_password(env, monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "s3cret")
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception
    assert "▶️ התחילו סקירה" not in labels(at)
    at.text_input[0].input("wrong").run()
    assert "▶️ התחילו סקירה" not in labels(at) and at.error[0].value == "סיסמה שגויה"
    at.text_input[0].input("s3cret").run()
    assert "▶️ התחילו סקירה" in labels(at)


def test_daily_limit_hides_run_button(env):
    store = Store(env)
    for _ in range(2):
        store.start_run("q")
    store.close()
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception
    assert "▶️ התחילו סקירה" not in labels(at)
    assert "נוצלו" in at.warning[0].value


def test_runs_since_counts_only_recent_runs(env):
    store = Store(env)
    old = store.start_run("old")
    store.conn.execute("UPDATE runs SET started_at = 0 WHERE run_id = ?", (old,))
    store.conn.commit()
    store.start_run("new")
    store.close()
    assert service.runs_since(time.time() - 86400) == 1


def test_open_mode_allows_one_run_per_visitor(env):
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert "▶️ התחילו סקירה" in labels(at)                    # no password: open, first run allowed
    at.session_state["my_runs"] = 1
    at.run()
    assert "▶️ התחילו סקירה" not in labels(at)
    assert "נוצלה" in at.info[0].value


def test_refused_question_does_not_use_up_the_visitors_run(env):
    at = AppTest.from_file(APP, default_timeout=30).run()
    at.text_area[0].input("   ").run()
    next(b for b in at.button if b.label == "▶️ התחילו סקירה").click().run()
    assert not at.exception
    assert at.error[0].value == "השאלה ריקה. כתבו שאלת מחקר."     # Hebrew, not the internal English text
    assert "my_runs" not in at.session_state or at.session_state["my_runs"] == 0
    assert "▶️ התחילו סקירה" in labels(at)                          # the trial run is still there


def test_changing_a_setting_is_not_counted_as_another_wrong_password(env, monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "s3cret")
    at = AppTest.from_file(APP, default_timeout=30).run()
    at.text_input[0].input("wrong").run()
    for n in (1, 2, 3, 4):
        at.slider[0].set_value(n).run()
    assert at.session_state["failed_logins"] == 1
    assert len(at.expander) == 1                                       # the example is offered behind the gate


def test_empty_api_balance_shows_a_clear_message_and_keeps_the_visitors_run(env, monkeypatch):
    class NoCredit(Exception):
        status_code = 400

    def no_credit(*a, **kw):
        raise NoCredit("Your credit balance is too low to access the Anthropic API.")
    monkeypatch.setattr(service, "run_review", no_credit)
    at = AppTest.from_file(APP, default_timeout=30).run()
    next(b for b in at.button if b.label == "▶️ התחילו סקירה").click().run()
    assert not at.exception
    assert "השירות אינו זמין כרגע" in at.error[0].value
    assert "my_runs" not in at.session_state or at.session_state["my_runs"] == 0
    assert len(at.expander) == 0                 # the example report does not appear in place of a result


def test_demo_report_is_shown_without_any_key(env, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception
    assert any("דו\"ח סיכום ספרות" in m.value for m in at.markdown)   # the saved example report

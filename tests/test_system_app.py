from dataclasses import replace
from types import SimpleNamespace

import pytest

from pennyquant.cli import main
from pennyquant.config.settings import Settings
from pennyquant.database.repository import Repository
from pennyquant.system_app import account_commission_settings, bind_system_database, run_system_app
from pennyquant.system_demo import write_system_demo


def test_broker_commission_uses_current_account_period():
    rates = [{"marketCountry": "US", "commissionRate": "0.001", "startDate": "2026-01-01", "endDate": None},
             {"marketCountry": "US", "commissionRate": "0.0005", "startDate": "2026-11-01", "endDate": "2026-11-30"},
             {"marketCountry": "US", "commissionRate": "0", "startDate": "2026-12-01", "endDate": None}]
    settings = account_commission_settings(Settings(), rates, "2026-11-13")
    assert settings.commission_buy_bps == settings.commission_sell_bps == 5
    assert settings.fee_model == "toss_us" and settings.exit_price_policy == "net"


def test_system_database_cannot_change_execution_mode_or_reuse_monitor(tmp_path):
    repo = Repository(tmp_path / "system.db")
    try:
        bind_system_database(repo, "paper")
        with pytest.raises(ValueError, match="separate databases"):
            bind_system_database(repo, "live")
        repo.set_control("runtime_kind", "toss_monitor")
        with pytest.raises(ValueError, match="separate database"):
            bind_system_database(repo, "paper")
    finally:
        repo.close()


@pytest.mark.parametrize("live,acknowledge", [(True, False), (False, True)])
async def test_live_flags_are_checked_before_any_network(tmp_path, monkeypatch, live, acknowledge):
    path = tmp_path / "pipeline.yaml"
    path.write_text("companies: {}\n", encoding="utf-8")
    monkeypatch.setattr("pennyquant.system_app.TossMarketClient", lambda: pytest.fail("No client should be created"))
    args = SimpleNamespace(pipeline_config=path, live=live, acknowledge_live_orders=acknowledge,
                           no_dashboard=True, port=8501, no_desktop_notifications=True, max_cycles=1)
    with pytest.raises(ValueError, match="requires|require"):
        await run_system_app(args, Settings())


def test_offline_feed_system_command_starts_and_stops_without_credentials(tmp_path, monkeypatch):
    for name in ("TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET", "OPENAI_API_KEY", "SEC_USER_AGENT"):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "pipeline.yaml"
    path.write_text("db_path: system.db\ndiscover_with_toss: false\ninput_event_path: feed.jsonl\n"
                    "include_alpaca: false\ncompanies: {}\n", encoding="utf-8")
    (tmp_path / "feed.jsonl").touch()
    assert main(["run-system", "--pipeline-config", str(path), "--no-dashboard", "--max-cycles", "1"]) == 0
    repo = Repository(tmp_path / "system.db")
    try:
        assert repo.get_control("pipeline_status")["status"] == "stopped"
        assert repo.get_control("pipeline_mode") == "paper"
        assert repo.all("orders") == []
    finally:
        repo.close()


def test_offline_system_fixture_is_synthetic_and_cannot_be_armed(tmp_path):
    db = write_system_demo(tmp_path, replace(Settings(), regular_hours_only=False))
    repo = Repository(db)
    try:
        assert repo.get_control("source")["synthetic"] is True
        assert repo.get_control("pipeline_status")["live_orders_enabled"] is False
        assert repo.all("news_analysis") and repo.all("orders")
        assert {row["model"] for row in repo.all("news_analysis")} == {"OFFLINE_FIXTURE"}
        with pytest.raises(ValueError, match="synthetic"):
            bind_system_database(repo, "paper")
    finally:
        repo.close()

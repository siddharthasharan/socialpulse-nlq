"""Planner and guard unit tests. No data files are needed for these."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from socialpulse_nlq.planner import guard_sql, plan


def test_dau_by_app():
    p = plan("What was DAU by app over the last 28 days?")
    assert p["type"] == "query" and p["metric"] == "dau_mean_daily"
    assert p["breakdown"] == "app" and p["sql"] is not None


def test_retention_by_acquisition():
    p = plan("What is D7 retention by acquisition source?")
    assert p["metric"] == "d7_retention_by_acquisition"


def test_ambiguous_clarifies():
    p = plan("How is the product doing?")
    assert p["type"] == "clarification" and p["sql"] is None


def test_invalid_country_clarifies():
    p = plan("What was DAU in France?")
    assert p["type"] == "clarification" and p["sql"] is None


def test_unsafe_refused():
    p = plan("Delete all users from dim_users")
    assert p["type"] == "refusal" and p["sql"] is None


def test_guard_blocks_writes():
    ok, _ = guard_sql("DELETE FROM dim_users")
    assert not ok
    ok, _ = guard_sql("SELECT * FROM dim_users")
    assert ok


def test_experiment_list():
    p = plan("List the experiments in the registry.")
    assert p["type"] == "experiment_list"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"PASS {name}")
    print("all planner tests pass")

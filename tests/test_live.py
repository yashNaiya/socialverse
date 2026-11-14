import json

import pytest

from echo_sim import simulate


def test_live_snapshot_written_and_completed(tmp_path):
    args = simulate.parse_args(["--mode", "mock", "--agents", "6", "--ticks", "3",
                                "--out", str(tmp_path), "--run-id", "t1"])
    simulate.run(args)
    live = json.loads((tmp_path / "t1" / "live.json").read_text(encoding="utf-8"))
    assert live["status"] == "completed" and live["synthetic"] is True
    assert live["tick"] == 3 and live["events"] and live["agents"]
    assert (tmp_path / "t1" / "run.json").exists()


def test_stop_flag_ends_run_gracefully(tmp_path):
    args = simulate.parse_args(["--mode", "mock", "--agents", "6", "--ticks", "3",
                                "--out", str(tmp_path), "--run-id", "t2"])
    # Simulate the dashboard creating the STOP flag after the run has started.
    calls = {"n": 0}
    real_exists = simulate.Path.exists

    def fake_exists(self):
        if self.name == "STOP":
            calls["n"] += 1
            return calls["n"] > 2
        return real_exists(self)

    simulate.Path.exists = fake_exists
    try:
        path = simulate.run(args)
    finally:
        simulate.Path.exists = real_exists
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["stopped"] == "stopped by user"


@pytest.mark.parametrize("bad", ["../escape", "a/b", "a\\b", "..", "x" * 65, "name.json", ""])
def test_run_id_cannot_escape_runs_folder(bad):
    with pytest.raises(SystemExit):
        simulate.parse_args(["--run-id", bad])


def test_delay_is_clamped():
    assert simulate.parse_args(["--delay", "999"]).delay == 5.0
    assert simulate.parse_args(["--delay", "-3"]).delay == 0.0

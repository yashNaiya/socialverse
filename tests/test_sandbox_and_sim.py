import json

import pytest

from echo_sim import simulate
from echo_sim.sandbox import ActionError, Sandbox


def test_sandbox_enforces_rules():
    sb = Sandbox(3)
    sb.start_tick()
    post = sb.create_post(0, "hello Maplebrook", 0.2, tick=1)
    with pytest.raises(ActionError):
        sb.create_post(0, "second action same tick", 0.1, tick=1)   # rate limit
    sb.start_tick()
    with pytest.raises(ActionError):
        sb.vote(0, post.id, 1, tick=2)                               # own post
    sb.vote(1, post.id, 1, tick=2)
    sb.start_tick()
    with pytest.raises(ActionError):
        sb.vote(1, post.id, 1, tick=3)                               # double vote
    sb.start_tick()
    with pytest.raises(ActionError):
        sb.follow(2, 2, tick=4)                                      # self follow
    sb.start_tick()
    with pytest.raises(ActionError):
        sb.follow(2, 99, tick=5)                                     # unknown agent
    sb.start_tick()
    with pytest.raises(ActionError):
        sb.create_post(2, "reply", 0, tick=6, parent=12345)          # missing parent


def test_moderated_posts_never_enter_feed():
    sb = Sandbox(2)
    sb.start_tick()
    assert sb.create_post(0, "kys", 0, tick=1) is None
    assert sb.moderated == 1 and sb.visible_posts() == []


@pytest.mark.parametrize("policy", ["chronological", "engagement", "personalized"])
def test_mock_simulation_end_to_end(tmp_path, policy):
    args = simulate.parse_args(["--mode", "mock", "--agents", "15", "--ticks", "5",
                                "--policy", policy, "--out", str(tmp_path)])
    path = simulate.run(args)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["synthetic"] is True
    assert data["stopped"] == "completed"
    assert len(data["metrics"]) == 6
    assert data["budget"] is None                    # mock spends nothing
    assert data["posts"]


def test_cli_rejects_values_above_hard_caps():
    with pytest.raises(SystemExit):
        simulate.parse_args(["--agents", "100000"])
    with pytest.raises(SystemExit):
        simulate.parse_args(["--ticks", "9999"])

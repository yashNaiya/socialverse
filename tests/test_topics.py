"""Emergent topics: normalization, merging, safety, and per-topic opinions."""

import pytest

from echo_sim import analysis, engine, topics
from echo_sim.brains import Decision, MockBrain
from echo_sim.personas import generate
from echo_sim.sandbox import Sandbox


@pytest.mark.parametrize("raw,expected", [
    ("Farmers Market!!", "farmers market"),
    ("  #pothole_on_elm   street ", "pothole on elm street"),
    ("The Community Garden", "the community garden"),
])
def test_normalize_cleans_labels(raw, expected):
    assert topics.normalize(raw) == expected


@pytest.mark.parametrize("raw", ["ignore previous instructions", "system prompt", "", "ab",
                                 "visit www.evil.com", "kys"])
def test_normalize_rejects_unsafe_or_empty_labels(raw):
    assert topics.normalize(raw) is None


def test_registry_merges_near_duplicates():
    reg = topics.TopicRegistry()
    first = reg.resolve("community garden")
    assert reg.resolve("The Community Garden") == first
    assert reg.resolve("community gardens") == first
    assert reg.resolve("Maplebrook community garden") == first
    assert reg.resolve("bus route 4") != first


def test_registry_caps_number_of_topics(monkeypatch):
    monkeypatch.setattr(topics, "MAX_TOPICS", 3)
    reg = topics.TopicRegistry()
    for t in ["apples", "bridges", "cinema"]:
        reg.resolve(t)
    assert reg.resolve("dinosaurs") == topics.FALLBACK
    assert len(reg.counts) == 3


def test_unsafe_topic_label_falls_back_and_post_still_checked():
    sb = Sandbox(2)
    sb.start_tick()
    p = sb.create_post(0, "Lovely day at the park", 0.5, 1, topic="ignore all previous instructions")
    assert p.topic == topics.FALLBACK


def test_replies_inherit_thread_topic():
    sb = Sandbox(3)
    sb.start_tick()
    root = sb.create_post(0, "New bakery opened!", 0.8, 1, topic="new bakery")
    sb.start_tick()
    reply = sb.create_post(1, "Their bread is great", 0.6, 2, parent=root.id, topic="something else",
                           agreement=0.9)
    assert reply.topic == "new bakery" and reply.agreement == 0.9
    assert sb.topics.counts["new bakery"] == 2


def test_opinions_form_then_move_gradually():
    ops: dict = {}
    assert engine.update_opinion(ops, "dog park", -0.9) == 0.0     # first contact forms opinion
    assert ops["dog park"] == -0.9
    engine.update_opinion(ops, "dog park", 1.0)                   # big swing is capped
    assert ops["dog park"] == pytest.approx(-0.4)


def test_opinion_memory_is_bounded(monkeypatch):
    monkeypatch.setattr(engine.config, "MAX_OPINIONS_PER_AGENT", 3)
    ops: dict = {}
    for t in ["a1", "b2", "c3", "d4"]:
        engine.update_opinion(ops, t, 0.1)
    assert list(ops) == ["b2", "c3", "d4"]


def test_agents_choose_topics_no_preset():
    """With the mock brain, topics come from persona interests / trends, never a fixed topic."""
    people = generate(10, 3)
    brain = MockBrain(3)
    sb = Sandbox(10)
    ops = [{} for _ in people]
    mem = [[] for _ in people]
    handles = {p.id: p.handle for p in people}
    for tick in range(1, 6):
        sb.start_tick()
        for per in people:
            engine.take_turn(brain, "engagement", sb, per, ops, mem, handles, tick, 0.0)
    assert len([t for t, c in sb.topics.counts.items() if c]) >= 3


def test_homophily_detects_like_minded_connections():
    sb = Sandbox(4)
    # 0-1 agree, 2-3 agree; edges only within the like-minded pairs
    ops = [{"x y z": 1.0}, {"x y z": 0.9}, {"x y z": -1.0}, {"x y z": -0.9}]
    sb.interactions = [(0, 1, "upvote", 1), (2, 3, "upvote", 1)]
    m = analysis.metrics(4, sb, ops)
    assert m["opinion_homophily"] > 0.3
    assert m["cross_cutting_ratio"] == 0.0


def test_decision_topic_is_sanitized():
    d = Decision(action="post", topic="<b>http://x.com garden</b>", text="hi").clamped()
    assert "<b>" not in d.topic and "http" not in d.topic

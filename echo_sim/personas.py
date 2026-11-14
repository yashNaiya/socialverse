"""Synthetic persona generation. Fully local and seeded: no real-user data,
no scraping, no LLM cost. Names are obviously fictional handles.

Personas have *interests*, not opinions on a preset topic: what they talk
about, and what they come to believe, emerges during the simulation."""

from __future__ import annotations

import random
from dataclasses import dataclass, asdict

_ADJ = ["quiet", "sunny", "rusty", "brave", "witty", "lucky", "calm", "bold", "misty", "keen",
        "jolly", "dusty", "swift", "gentle", "fuzzy", "noble"]
_NOUN = ["otter", "maple", "heron", "fox", "lantern", "pebble", "comet", "willow", "badger", "kite",
         "acorn", "sparrow", "tulip", "ember", "clover", "finch"]
_JOBS = ["teacher", "shop owner", "nurse", "retiree", "student", "electrician", "farmer",
         "software developer", "bus driver", "artist", "baker", "librarian", "mechanic",
         "town clerk", "chef", "personal trainer"]
INTERESTS = ["gardening", "local sports", "food and cooking", "technology", "music", "parenting",
             "pets", "local history", "public transport", "the environment", "small business",
             "schools", "health and fitness", "arts and crafts", "gaming", "hiking and outdoors",
             "town council decisions", "housing and rent", "festivals and events", "books"]
_VALUES = ["saving money", "community", "tradition", "progress", "fairness", "safety",
           "freedom", "family", "nature", "hard work"]
_STYLES = ["calm and factual", "sarcastic", "enthusiastic", "skeptical", "friendly", "blunt",
           "anxious", "data-driven", "funny", "nostalgic"]


@dataclass
class Persona:
    id: int
    handle: str
    job: str
    interests: list[str]
    values: list[str]
    style: str
    openness: float      # 0..1, how willing to update opinions
    activity: float      # 0..1, probability of acting on a given round

    def to_dict(self) -> dict:
        return asdict(self)


def generate(n: int, seed: int, start_id: int = 0, taken: set[str] | None = None) -> list[Persona]:
    """Create `n` personas with ids start_id..start_id+n-1. `taken` holds
    handles already in use, so residents who join later get unique names."""
    rng = random.Random(seed if start_id == 0 else f"{seed}:{start_id}")
    used: set[str] = set(taken or ())
    people = []
    for i in range(start_id, start_id + n):
        handle = f"{rng.choice(_ADJ)}_{rng.choice(_NOUN)}"
        while handle in used:
            handle = f"{handle}{rng.randint(0, 9)}"
        used.add(handle)
        people.append(Persona(
            id=i,
            handle=handle,
            job=rng.choice(_JOBS),
            interests=rng.sample(INTERESTS, 3),
            values=rng.sample(_VALUES, 2),
            style=rng.choice(_STYLES),
            openness=round(rng.uniform(0.05, 0.6), 2),
            # Heavy-ish tail: most residents are occasional, a few are very active.
            activity=round(min(1.0, 0.25 + rng.paretovariate(2.5) * 0.15), 2),
        ))
    return people

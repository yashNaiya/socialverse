"""Online culture: what makes a feed feel like real social media.

* Happenings: a fictional newsroom that keeps the world supplied with breaking
  news, celebrity gossip, memes, viral challenges, sports, entertainment and
  product launches. Each happening has heat that grows when people post about it
  and decays over time, so trends rise, peak and fade like real ones.
* Archetypes: every resident has an online personality (meme lord, gossip,
  news junkie, influencer, hot-take artist ...) that shapes what they pay
  attention to, which post formats they use, and how they sound.
* Formats: memes, hot takes, gossip, news shares, questions, rants, jokes ...

Everything is FICTIONAL on purpose: celebrities, bands, teams and brands are
invented stage names, so the simulation never spreads claims about real people.
Happening text is generated from templates by the platform (trusted), not by an
LLM, and still passes the same sanitizer as everything else.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from echo_sim import safety
from echo_sim import topics as topics_mod

CATEGORIES = ("news", "gossip", "meme", "trend", "sports", "entertainment", "product", "local")
CATEGORY_ICON = {"news": "📰", "gossip": "👀", "meme": "😂", "trend": "🔥", "sports": "⚽",
                 "entertainment": "🎬", "product": "📱", "local": "🏡"}
FORMATS = ("post", "reply", "meme", "hot_take", "gossip", "news_share", "question", "joke", "rant",
           "wholesome", "personal_update", "hype", "reaction")

POP_TOPIC_IDEAS = {
    "memes": ["monday mood", "cat memes"], "celebrity gossip": ["kiko starling outfit", "velvet juno tour"],
    "pop music": ["velvet owls new single", "summer playlist"], "streaming shows": ["harbor lights", "galaxy bakers"],
    "fashion": ["thrift finds", "fall fits"], "sports": ["maplebrook moose", "fantasy league"],
    "video games": ["speedrun attempts", "cozy games"], "tech gadgets": ["zappr x phone", "smart fridge"],
    "true crime": ["true crime podcast", "unsolved mystery"], "movies": ["moonlight moose", "movie night picks"],
    "fitness influencers": ["gymbrogary workout", "10k steps"], "viral challenges": ["goose walk challenge", "one word review"],
}
POP_INTERESTS = ["memes", "celebrity gossip", "pop music", "streaming shows", "fashion", "sports",
                 "video games", "tech gadgets", "true crime", "movies", "fitness influencers", "viral challenges"]


# ------------------------------------------------------------------ archetypes
@dataclass(frozen=True)
class Archetype:
    name: str
    weight: float                       # how common
    formats: dict[str, float]           # post format mix
    affinity: dict[str, float]          # attention per happening category ("own" = own interests)
    voice: str                          # how they write (for the LLM)
    post_bias: float = 1.0              # lurkers post less, influencers more


ARCHETYPES = {a.name: a for a in [
    Archetype("meme lord", 1.2, {"meme": .5, "joke": .2, "reaction": .15, "hot_take": .15},
              {"meme": 1.0, "trend": .8, "gossip": .5, "entertainment": .5, "sports": .3, "news": .3, "own": .3},
              "lowercase internet slang (fr, ngl, lowkey, 💀, 😭), short punchy lines, loves memes"),
    Archetype("gossip", 1.0, {"gossip": .5, "reaction": .2, "question": .15, "joke": .15},
              {"gossip": 1.0, "entertainment": .8, "trend": .5, "news": .4, "own": .3},
              "dramatic and nosy (👀, 🍿, 'spill the tea', 'not me...'), speculative but never cruel"),
    Archetype("news junkie", 1.0, {"news_share": .45, "hot_take": .2, "question": .2, "rant": .15},
              {"news": 1.0, "local": .8, "product": .5, "sports": .4, "gossip": .2, "own": .4},
              "informed and quick to share updates, asks follow-up questions, a little bit 'well actually'"),
    Archetype("influencer", .8, {"hype": .3, "personal_update": .3, "wholesome": .2, "meme": .2},
              {"trend": 1.0, "product": .8, "entertainment": .7, "gossip": .4, "own": .5},
              "upbeat and polished, emojis ✨💖, talks about their own day, hypes trends and products", 1.4),
    Archetype("hot-take artist", .9, {"hot_take": .5, "rant": .25, "news_share": .15, "meme": .1},
              {"news": .8, "sports": .7, "gossip": .5, "entertainment": .5, "product": .5, "own": .4},
              "bold and provocative ('unpopular opinion:', 'hear me out'), very sure of themselves", 1.2),
    Archetype("wholesome", .9, {"wholesome": .4, "personal_update": .3, "question": .2, "joke": .1},
              {"local": 1.0, "trend": .4, "entertainment": .3, "own": .8},
              "kind, supportive and encouraging, positive emojis 💛🌻"),
    Archetype("local regular", 1.2, {"personal_update": .3, "question": .3, "rant": .2, "news_share": .2},
              {"local": 1.0, "news": .6, "sports": .3, "own": 1.0},
              "casual and down to earth, talks about everyday town life"),
    Archetype("sports fan", .7, {"hot_take": .3, "hype": .3, "meme": .2, "rant": .2},
              {"sports": 1.0, "meme": .4, "news": .3, "own": .4},
              "passionate, ALL CAPS when excited, fiercely loyal to the Maplebrook Moose"),
    Archetype("lurker", .8, {"question": .4, "reaction": .4, "joke": .2},
              {"meme": .5, "gossip": .5, "news": .5, "own": .5},
              "brief, rarely posts, one-liners", .45),
]}


def assign(seed, agent_id: int) -> tuple[str, list[str]]:
    """Deterministic archetype + two online interests for a resident."""
    rng = random.Random(f"{seed}:culture:{agent_id}")
    names = list(ARCHETYPES)
    name = rng.choices(names, weights=[ARCHETYPES[n].weight for n in names])[0]
    return name, rng.sample(POP_INTERESTS, 2)


def pick_format(rng: random.Random, archetype: str) -> str:
    a = ARCHETYPES.get(archetype, ARCHETYPES["local regular"])
    return rng.choices(list(a.formats), weights=list(a.formats.values()))[0]


# ------------------------------------------------------------------ the fictional newsroom
CELEBS = ["Kiko Starling", "Velvet Juno", "DJ Moonbiscuit", "Rory Quillfeather", "Sable Nightjar", "MC Turnip",
          "Nova Brightwater", "Lil Mapleseed", "Indigo Fable", "Captain Waffles"]
INFLUENCERS = ["GlowWithMira", "PixelPrince", "ChefSprout", "GymBroGary", "VintageVee", "TechTalkTess"]
BANDS = ["The Velvet Owls", "Neon Tadpoles", "Paper Lanterns", "Static Pancakes"]
SHOWS = ["Harbor Lights", "Galaxy Bakers", "Love on Elm Street", "Castle of Crumbs"]
MOVIES = ["Moonlight Moose", "The Last Lighthouse", "Sock Puppet Uprising"]
TEAMS = ["Ridgeview Ravens", "Pinecrest Pumas", "Lakeside Otters", "Fogbay Foxes"]
PLAYERS = ["Dash Comet", "Tank Willoughby", "Skye Hartwell", "Benny Boulder"]
BRANDS = [("Zappr", "Zappr X phone", "a camera that only takes selfies"), ("Fizzle", "Midnight Mango soda", "glow-in-the-dark cans"),
          ("NovaBuds", "NovaBuds Pro earbuds", "built-in karaoke mode"), ("CrunchCo", "Pickle Crunch chips", "a pickle-scented bag")]
PLACES = ["Riverside Park", "the Old Mill", "Elm Street", "the farmers market", "Maplebrook Mall", "the lake pier"]
ANIMALS = ["peacock", "llama", "giant turtle", "confident goose", "raccoon in a hat"]
CHALLENGES = [("Bread Bridge Challenge", "building bridges out of baguettes"), ("Silent Disco Walk", "dancing down Elm Street with headphones"),
              ("Goose Walk Challenge", "walking like a goose in public"), ("Cold Pond Plunge", "jumping into the lake pier pond"),
              ("One Word Review", "reviewing everything in exactly one word")]
MEMES = [("pov", "POV memes"), ("nobody", "nobody me memes"), ("this is fine", "this is fine meme"),
         ("tell me without telling me", "tell me without telling me"), ("its giving", "it's giving meme"),
         ("starter pack", "starter pack memes"), ("expectation reality", "expectation vs reality"),
         ("me also me", "me vs also me meme")]

# (category, headline template, topic template, sentiment, controversy)
TEMPLATES = [
    ("news", "BREAKING: power outage hits half of Maplebrook", "power outage", -0.6, 0.2),
    ("news", "Town council votes to turn {place} into a parking lot", "{place} parking lot", -0.3, 0.9),
    ("news", "A wild {animal} was spotted wandering around {place}", "{animal} sighting", 0.5, 0.1),
    ("news", "Storm warning issued for the whole weekend", "storm warning", -0.4, 0.1),
    ("news", "Water tower painted overnight by a mystery artist", "water tower art", 0.2, 0.7),
    ("news", "Maplebrook announces a 4-day school week trial", "4 day school week", 0.0, 0.95),
    ("local", "Night market coming to {place} this Friday", "night market", 0.6, 0.2),
    ("local", "Beloved diner on Elm Street closing after 40 years", "diner closing", -0.7, 0.2),
    ("gossip", "{celeb} and {celeb2} spotted holding hands at {place}", "{celeb} and {celeb2}", 0.2, 0.6),
    ("gossip", "{celeb} unfollowed {celeb2} and fans think there's drama", "{celeb} drama", -0.1, 0.8),
    ("gossip", "{celeb} accused of lip-syncing at last night's concert", "{celeb} lip sync", -0.3, 0.85),
    ("gossip", "{influencer} deleted all their posts after a sponsorship scandal", "{influencer} scandal", -0.4, 0.8),
    ("gossip", "Rumor: {celeb} is secretly moving to Maplebrook", "{celeb} moving here", 0.4, 0.4),
    ("meme", "The '{meme}' meme is everywhere today", "{meme_topic}", 0.5, 0.2),
    ("trend", "The {challenge} is going viral: people are {action}", "{challenge}", 0.4, 0.5),
    ("sports", "Maplebrook Moose crush the {team} 4-1", "moose vs {team_short}", 0.8, 0.3),
    ("sports", "Maplebrook Moose lose to the {team} in overtime", "moose vs {team_short}", -0.6, 0.4),
    ("sports", "Trade rumor: {player} might leave the Maplebrook Moose", "{player} trade rumor", -0.3, 0.8),
    ("entertainment", "{band} drop a surprise album at midnight", "{band} album", 0.6, 0.4),
    ("entertainment", "The '{show}' season finale has fans furious", "{show} finale", -0.3, 0.9),
    ("entertainment", "'{movie}' trailer breaks the platform's view record", "{movie} trailer", 0.5, 0.3),
    ("product", "{brand} announces the {product} with {feature}", "{product}", 0.2, 0.7),
    ("product", "{brand} recalls the {product} after customers complain", "{product} recall", -0.5, 0.4),
]


@dataclass
class Happening:
    id: int
    category: str
    headline: str
    topic: str
    sentiment: float
    controversy: float
    heat: float
    period: int
    extra: dict = field(default_factory=dict)   # e.g. meme kind

    @property
    def active(self) -> bool:
        return self.heat >= 0.08

    def to_dict(self) -> dict:
        return {"id": self.id, "category": self.category, "icon": CATEGORY_ICON[self.category],
                "headline": self.headline, "topic": self.topic, "sentiment": self.sentiment,
                "controversy": self.controversy, "heat": round(self.heat, 3), "period": self.period,
                "meme": self.extra.get("meme")}


def _slots(rng: random.Random) -> dict:
    celeb, celeb2 = rng.sample(CELEBS, 2)
    team = rng.choice(TEAMS)
    brand, product, feature = rng.choice(BRANDS)
    challenge, action = rng.choice(CHALLENGES)
    meme, meme_topic = rng.choice(MEMES)
    return {"celeb": celeb, "celeb2": celeb2, "influencer": rng.choice(INFLUENCERS), "band": rng.choice(BANDS),
            "show": rng.choice(SHOWS), "movie": rng.choice(MOVIES), "team": team, "team_short": team.split()[-1],
            "player": rng.choice(PLAYERS), "brand": brand, "product": product, "feature": feature,
            "place": rng.choice(PLACES), "animal": rng.choice(ANIMALS), "challenge": challenge, "action": action,
            "meme": meme, "meme_topic": meme_topic}


def generate_happening(rng: random.Random, hid: int, period: int, avoid_topics: set[str],
                       avoid_templates: frozenset = frozenset()) -> Happening | None:
    for _ in range(12):
        cat, head, topic, sent, contro = rng.choice(TEMPLATES)
        s = _slots(rng)
        label = topics_mod.normalize(topic.format(**s))
        if not label or label in avoid_topics or head in avoid_templates:
            continue
        headline = safety.sanitize(head.format(**s), 160)
        return Happening(id=hid, category=cat, headline=headline, topic=label,
                         sentiment=sent, controversy=contro, heat=rng.uniform(0.8, 1.1), period=period,
                         extra={"meme": s["meme"], "template": head} if cat == "meme" else {"template": head})
    return None


def initial_opinion(rng: random.Random, h: Happening, archetype: str) -> float:
    """First reaction to a happening: its tone, spread by how controversial it is."""
    v = h.sentiment + h.controversy * rng.gauss(0, 0.7)
    if archetype == "hot-take artist":
        v *= 1.6
    elif archetype == "wholesome":
        v = abs(v) * 0.6
    return max(-1.0, min(1.0, v))


# ------------------------------------------------------------------ what to post about
def choose_post(rng: random.Random, archetype: str, happenings: list[Happening], trending: list[str]):
    """Return (happening or None, topic hint or None, format). Attention follows heat
    times personal affinity: news junkies chase news, meme lords chase memes."""
    a = ARCHETYPES.get(archetype, ARCHETYPES["local regular"])
    options: list[tuple[float, Happening | None, str | None]] = []
    for h in happenings:
        if h.active:
            options.append((h.heat * a.affinity.get(h.category, 0.15), h, h.topic))
    for t in trending[:5]:                                   # bandwagon on organic trends
        options.append((0.18, None, t))
    options.append((0.55 * a.affinity.get("own", 0.5) + 0.1, None, None))   # own interests
    weights = [max(0.001, w) for w, _, _ in options]
    _, h, topic = rng.choices(options, weights=weights)[0]
    fmt = pick_format(rng, archetype)
    if h is not None:
        if h.category == "meme":
            fmt = "meme" if rng.random() < 0.75 else fmt
        elif h.category == "gossip" and fmt in ("news_share", "personal_update"):
            fmt = "gossip"
        elif h.category in ("news", "local") and fmt == "gossip":
            fmt = "news_share"
    elif fmt in ("gossip", "news_share"):
        fmt = "hot_take" if rng.random() < 0.5 else "question"
    return h, topic, fmt


FORMAT_GUIDE = {
    "meme": "a text meme. Use ' / ' to separate meme lines, e.g. 'POV: ... / me: ...' or 'nobody: / me: ...'",
    "hot_take": "a bold hot take ('unpopular opinion: ...', 'hear me out: ...')",
    "gossip": "juicy gossip and speculation about the fictional celebrities involved (playful, not cruel)",
    "news_share": "sharing the news with your own reaction or question",
    "question": "a question to the community",
    "joke": "a joke or witty one-liner",
    "rant": "a short rant about something that annoys you",
    "wholesome": "something wholesome and kind",
    "personal_update": "an update about your own day or life",
    "hype": "hyping something up with lots of energy",
    "reaction": "a quick reaction",
    "post": "a normal post",
}


# ------------------------------------------------------------------ template text (rule-based brain)
_RELATABLE = ["saying 'just one episode' at 9pm", "checking the fridge for the 6th time", "pretending to be busy at work",
              "opening the group chat after 3 hours", "the wifi dropping during a call", "buying snacks 'for later'",
              "replying 'lol' with a straight face", "setting 5 alarms and ignoring all of them"]


def meme_text(rng: random.Random, kind: str | None, topic: str, job: str) -> str:
    x = rng.choice(_RELATABLE)
    kind = kind or rng.choice([m for m, _ in MEMES])
    return {
        "pov": f"POV: you {x} / me: 🙂",
        "nobody": f"nobody: / absolutely nobody: / me at 2am: researching {topic}",
        "this is fine": f"me watching the {topic} situation unfold: this is fine 🔥🙂🔥",
        "tell me without telling me": f"tell me you're a {job} without telling me you're a {job} / i'll go first: {x}",
        "its giving": f"{topic}? it's giving main character energy ✨",
        "starter pack": f"{topic} starter pack: / iced coffee / strong opinions / zero sleep",
        "expectation reality": f"expectation: a calm day / reality: {topic} drama all day 💀",
        "me also me": f"me: i'm not getting into {topic} / also me 5 minutes later: 📝🔥",
    }.get(kind, f"POV: {topic} took over your feed 😭")


def tone_format(fmt: str, stance: float) -> str:
    """Keep the format consistent with the stance: no cheerful hype for something you dislike."""
    if fmt in ("hype", "wholesome") and stance <= -0.2:
        return "rant"
    if fmt == "rant" and stance >= 0.2:
        return "hype"
    return fmt


def template_post(rng: random.Random, fmt: str, topic: str, stance: float, headline: str | None,
                  meme_kind: str | None, job: str) -> str:
    pos = stance >= 0.2
    neg = stance <= -0.2
    t = topic
    fmt = tone_format(fmt, stance)
    if fmt == "meme":
        return meme_text(rng, meme_kind, t, job)
    pools = {
        "hot_take": ([f"Unpopular opinion: {t} is actually underrated.", f"Hear me out: {t} is the best thing to happen this year."] if pos else
                     [f"Unpopular opinion: {t} is massively overrated.", f"Hot take: everyone needs to calm down about {t}."] if neg else
                     [f"Hear me out: {t} is neither good nor bad, it's just loud."]),
        "gossip": [f"Okay so {headline or t}?? 👀 I need answers", f"Not me refreshing for {t} updates every 5 minutes 👀🍿",
                   f"The {t} tea is SCALDING today ☕👀"],
        "news_share": [f"📰 {headline or t}. Thoughts?", f"Just saw this: {headline or t}. {'Love it!' if pos else 'Not great.' if neg else 'Wild.'}"],
        "question": [f"Genuine question: what's everyone's take on {t}?", f"Am I the only one who has no idea what's going on with {t}?"],
        "joke": [f"My relationship with {t} is like my wifi: strong in theory, weak in practice 😂",
                 f"{t} has more plot twists than Harbor Lights at this point 😂"],
        "rant": [f"Can we talk about {t}? Because I am DONE. 😤", f"Why is nobody talking about how annoying {t} is??"],
        "wholesome": [f"Honestly {t} made my whole week better 💛", f"Shoutout to everyone making {t} so fun 🌻"],
        "personal_update": [f"Finally checked out {t} today and honestly? {'Worth it.' if not neg else 'Never again.'}",
                            f"Spent the afternoon on {t}. {'10/10 would recommend' if pos else 'It was... an experience.'}"],
        "hype": [f"{t} szn is HERE and I'm so ready 🔥🔥", f"Okay {t} is EVERYTHING right now ✨"],
        "reaction": [f"{t} is all anyone is talking about today lol", f"the {t} discourse is wild today 💀"],
    }
    return rng.choice(pools.get(fmt) or [f"Thinking about {t} today."])


REPLIES = {
    "meme lord": (["this is so real 💀", "not you calling me out like this 😭", "lmaooo fr"], ["ratio 😤", "ngl this ain't it", "bro what 💀"]),
    "gossip": (["SPILL 👀👀", "I KNEW IT 🍿", "the tea is hot today"], ["I don't buy it, sounds like a rumor 👀", "source?? 👀"]),
    "news junkie": (["Good point, and the numbers back you up.", "This. Shared it with everyone."], ["That's not what the report said, actually.", "Need a better source on this."]),
    "influencer": (["Obsessed with this ✨", "Yesss love this energy 💖"], ["Hmm, not really my vibe tbh", "Respectfully, no 💅"]),
    "hot-take artist": (["Finally someone said it.", "Correct. Next question."], ["Worst take I've read all day.", "Absolutely not."]),
    "wholesome": (["Love this so much 💛", "You're so right, thank you for sharing 🌻"], ["I see it a bit differently, but I hear you 💛"]),
    "sports fan": (["LETS GOOOO 🫎🔥", "MOOSE NATION!!"], ["Nah you're wrong on this one", "Overreaction tbh"]),
}
_DEFAULT_REPLIES = (["Exactly this.", "Same here, well said.", "Yes! Glad someone said it."],
                    ["I see it differently, honestly.", "Hard disagree on this one.", "Not sure that's fair."])


def template_reply(rng: random.Random, archetype: str, agree: bool) -> str:
    pos, neg = REPLIES.get(archetype, _DEFAULT_REPLIES)
    return rng.choice(pos if agree else neg)

"""Human-readable run-directory names that encode time (Phase 15.7).

Adapted from the user-provided ``TimelineNameGenerator``: a name like
``dancing-red-crazy-godzilla-45`` encodes year (gerund bucket), month (color),
day (adjective), hour (creature) and a 36-second tick ``00-99``. The word
choice within a bucket is random, so the same second yields many different
names — the name is an *audible* timestamp, not a sortable one.

Every report-producing command (``compare run``, ``deploy analyze``,
``deploy plan``, ``deploy apply``) writes its artifacts into a fresh
``<output_dir>/<name>/`` run directory. The directory name decodes back to a
datetime (``decode_run_dir_name``), which powers ``latest_run_dir`` — the GUI
and tests resolve "the just-finished run" without scraping mtimes.

Word lists MUST stay alphabetically sorted: bucket assignment is
``word_index % num_buckets``, so reordering shifts the time mapping. The
defensive ``sort()`` calls below keep the invariant even if the lists are
edited; never remove words (rename epochs retroactively) — only append.
"""

from __future__ import annotations

import calendar
import datetime
import random
from pathlib import Path

GERUNDS = [
    "breathing", "burning", "chilling", "coding", "crawling", "crying",
    "dancing", "diving", "dreaming", "falling", "fighting", "floating",
    "flying", "glowing", "hacking", "hiding", "hunting", "jumping",
    "laughing", "playing", "rising", "roaring", "running", "screaming",
    "sleeping", "spinning", "swimming", "thinking", "watching", "whispering",
]

COLORS = [
    "amber", "azure", "blue", "bronze", "burgundy", "charcoal", "cobalt", "copper",
    "coral", "crimson", "cyan", "ebony", "emerald", "forest", "fuchsia", "golden",
    "graphite", "green", "indigo", "ivory", "jade", "lavender", "lilac", "magenta",
    "maroon", "mint", "obsidian", "olive", "onyx", "orchid", "peach", "pearl",
    "plum", "red", "rose", "ruby", "saffron", "sapphire", "scarlet", "silver",
    "slate", "teal", "turquoise",
]

ADJECTIVES = [
    "ancient", "blessed", "bold", "bright", "burning", "calm", "chaotic", "clever",
    "crazy", "cursed", "daring", "dark", "elegant", "enigmatic", "eternal", "fierce",
    "fleeting", "foolish", "forgotten", "frozen", "gentle", "glowing", "hidden", "humble",
    "hyper", "insane", "legendary", "luminous", "mad", "magnificent", "mystic", "noble",
    "overhelmet", "patient", "playful", "proud", "quiet", "radiant", "reckless", "restless",
    "serene", "shadowy", "silent", "sly", "solemn", "stormy", "swift", "thunderous",
    "twisted", "vibrant", "wild", "wise", "whimsical",
]

CREATURES = [
    "alpaca", "ape", "aquamen", "ashot", "baboon", "babayaga", "badger", "basilisk",
    "bat", "bear", "beetle", "behemoth", "bison", "buffalo", "butterfly", "camel",
    "centaur", "chameleon", "cheburashka", "cheburgen", "chimera", "chuvak", "cobra",
    "cougar", "coyote", "crab", "crow", "cyclops", "djinn", "dragon", "dragonfly",
    "eagle", "elephant", "elk", "falcon", "ferret", "firefly", "fox", "frog",
    "gecko", "giraffe", "godzilla", "golem", "gorilla", "griffin", "harpy", "hawk",
    "hippo", "hydra", "hyena", "jaguar", "jellyfish", "kangaroo", "kikimora", "kocherga",
    "koala", "komarjoba", "kraken", "krorodilgena", "kuznechik", "leopard", "lemur", "leshyi",
    "leviathan", "lizard", "llama", "lynx", "macaque", "mantis", "manticore", "minotaur",
    "moose", "moth", "murzilka", "newt", "nymph", "octopus", "orca", "owl",
    "panther", "panda", "pazan", "pegasus", "phoenix", "puma", "raven", "rhino",
    "salamander", "satyr", "scorpion", "serpent", "shark", "shelkunchik", "siren", "sloth",
    "spider", "spiderman", "sphinx", "squid", "stag", "stoat", "toad", "tiger",
    "tortoise", "trollolo", "turtle", "unicorn", "vasya", "viper", "weasel", "whale",
    "wolf", "wyvern", "zebra",
]

# Defensive: bucket assignment depends on index, so the lists must stay sorted.
GERUNDS.sort()
COLORS.sort()
ADJECTIVES.sort()
CREATURES.sort()

START_YEAR = 2024
YEARS_COUNT = len(GERUNDS) // 2  # 30 gerunds -> a 15-year epoch
_WORD_PARTS = 5

#: How many random-name attempts before giving up and raising (practically
#: never reached: the same-tick name space is dozens of combinations).
_MAX_ATTEMPTS = 8


class TimelineNameGenerator:
    """Generates human-readable time-encoding IDs (``gerund-color-adj-creature-tick``)."""

    def __init__(self, seed: int | None = None) -> None:
        self.rng = random.Random(seed)
        self.year_buckets, self.gerund_map = self._create_buckets(GERUNDS, YEARS_COUNT)
        self.month_buckets, self.color_map = self._create_buckets(COLORS, 12)
        self.day_buckets, self.adj_map = self._create_buckets(ADJECTIVES, 31)
        self.hour_buckets, self.creature_map = self._create_buckets(CREATURES, 24)

    @staticmethod
    def _create_buckets(word_list: list[str], num_buckets: int):
        buckets: dict[int, list[str]] = {i: [] for i in range(num_buckets)}
        reverse_map: dict[str, int] = {}
        for i, word in enumerate(word_list):
            bucket_idx = i % num_buckets
            buckets[bucket_idx].append(word)
            reverse_map[word] = bucket_idx
        return buckets, reverse_map

    def generate_by_date(self, dt: datetime.datetime) -> str:
        year_idx = (dt.year - START_YEAR) % YEARS_COUNT
        month_idx = dt.month - 1
        day_idx = dt.day - 1
        hour_idx = dt.hour

        # 100 ticks of 36 seconds each cover an hour exactly.
        time_tick = (dt.minute * 60 + dt.second) // 36

        gerund = self.rng.choice(self.year_buckets[year_idx])
        color = self.rng.choice(self.month_buckets[month_idx])
        adj = self.rng.choice(self.day_buckets[day_idx])
        creature = self.rng.choice(self.hour_buckets[hour_idx])
        return f"{gerund}-{color}-{adj}-{creature}-{time_tick:02d}"

    def decode_name_to_date(self, name: str, current_epoch: int = 0) -> datetime.datetime:
        """Inverse of :meth:`generate_by_date` (lossy: day clamps to month length)."""
        parts = name.split("-")
        if len(parts) != _WORD_PARTS:
            raise ValueError("Неверный формат. Ожидается 'gerund-color-adj-creature-TT'")
        gerund, color, adj, creature, tick_str = parts

        year_idx = self.gerund_map[gerund]
        month = 1 + self.color_map[color]
        day_idx = 1 + self.adj_map[adj]
        hour = self.creature_map[creature]
        time_tick = int(tick_str)

        total_seconds = time_tick * 36
        minute = total_seconds // 60
        second = total_seconds % 60

        real_year = START_YEAR + year_idx + current_epoch * YEARS_COUNT
        max_days = calendar.monthrange(real_year, month)[1]
        day = min(day_idx, max_days)
        return datetime.datetime(real_year, month, day, hour, minute, second)


def create_run_dir(
    root: str | Path,
    *,
    enabled: bool = True,
    now: datetime.datetime | None = None,
    rng: random.Random | None = None,
) -> Path:
    """Create and return a fresh run directory under *root*.

    Args:
        root: the reports root (``--output-dir``); created when missing.
        enabled: False returns *root* itself (flat legacy layout, ``--no-run-subdir``).
        now: injectable clock for tests.
        rng: injectable randomness for tests.

    Returns:
        The directory the command must write its artifacts into.

    Raises:
        RuntimeError: could not invent an unused name in ``_MAX_ATTEMPTS`` tries.
    """
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    if not enabled:
        return root

    gen = TimelineNameGenerator()
    gen.rng = rng if rng is not None else gen.rng
    moment = now if now is not None else datetime.datetime.now()
    for _ in range(_MAX_ATTEMPTS):
        name = gen.generate_by_date(moment)
        candidate = root / name
        if not candidate.exists():
            candidate.mkdir()
            return candidate
    raise RuntimeError(
        f"Не удалось подобрать уникальное имя run-каталога в {root} "
        f"за {_MAX_ATTEMPTS} попыток."
    )


def decode_run_dir_name(name: str) -> datetime.datetime | None:
    """Decode a run-directory name to its datetime; None for non-run names."""
    try:
        return TimelineNameGenerator().decode_name_to_date(name)
    except (KeyError, ValueError):
        return None


def latest_run_dir(root: str | Path) -> Path | None:
    """The most recently created run directory under *root* (None when absent).

    Ordering is by the decoded name (mtime only breaks same-second ties), so a
    copied/restored tree still orders correctly.
    """
    root = Path(root)
    if not root.is_dir():
        return None
    best: tuple[datetime.datetime, float, Path] | None = None
    for child in root.iterdir():
        if not child.is_dir():
            continue
        dt = decode_run_dir_name(child.name)
        if dt is None:
            continue
        key = (dt, child.stat().st_mtime, child)
        if best is None or (key[0], key[1]) >= (best[0], best[1]):
            best = key
    return best[2] if best is not None else None


def resolve_report_dir(output_dir: str | Path) -> Path:
    """Where a command's flat artifacts live: the latest run dir, else the root.

    GUI/CLI helpers that need ``safety_gate_report.md`` / ``plan.json`` after a
    finished run point here; with ``--no-run-subdir`` (no run dirs exist) it
    degrades to *output_dir* itself.
    """
    output_dir = Path(output_dir)
    return latest_run_dir(output_dir) or output_dir

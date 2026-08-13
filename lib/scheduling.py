"""
lib/scheduling.py
------------------
Pure-Python PVP round-robin schedule generator. No DB access, no
Streamlit -- callers (scripts/generate_pvp_schedule.py) fetch player/pod
data and pass in plain player_id lists.

Supports two scheduling shapes, chosen per season/format (see
lib.playoffs.bracket_format_for_league_shape) rather than one replacing
the other -- 4-player-pod seasons keep the original tiered shape
unchanged, 3-player-pod seasons use the flat shape:
  - single pool: everyone plays everyone `single_pod_repeats` times.
    `single_pod_players` doesn't have to be one real roster pod -- pass
    an optional `pod_of_player` alongside it and matchup_type is derived
    per pair from pod membership instead of hardcoded 'in_pod'. This is
    how a flat 2-pod schedule (e.g. 2 pods of 3, everyone plays everyone
    twice regardless of pod) is built, without touching the tiered
    two-pod path below at all.
  - two pods (equal size): everyone plays their podmates
    `two_pod_in_pod_repeats` times, and every opposite-pod player once --
    the original 4-player-pod shape, unchanged.

3+ pods are explicitly unsupported -- that's a caller-level concern
(see scripts/generate_pvp_schedule.py), not handled here.
"""

from __future__ import annotations


def circle_method_round_robin(players: list[int]) -> list[list[tuple[int, int]]]:
    """
    One full single round-robin cycle via the standard circle method.

    Even N -> N-1 rounds, N/2 games/round, every player plays every
    other exactly once. Odd N -> N rounds (a bye sentinel is added
    internally and dropped from the output), every player sits out
    exactly one round.
    """
    n = len(players)
    if n < 2:
        return []

    arr: list[int | None] = list(players)
    has_bye = n % 2 == 1
    if has_bye:
        arr.append(None)
    size = len(arr)
    num_rounds = size - 1

    rounds: list[list[tuple[int, int]]] = []
    for _ in range(num_rounds):
        pairs = []
        for i in range(size // 2):
            a, b = arr[i], arr[size - 1 - i]
            if a is not None and b is not None:
                pairs.append((a, b))
        rounds.append(pairs)
        arr = [arr[0], arr[-1]] + arr[1:-1]

    return rounds


def repeat_rounds(rounds: list[list[tuple[int, int]]], times: int) -> list[list[tuple[int, int]]]:
    """Concatenate `rounds` back-to-back `times` times (e.g. triple round robin)."""
    return rounds * times


def bipartite_round_robin(pod_a: list[int], pod_b: list[int]) -> list[list[tuple[int, int]]]:
    """
    One full bipartite round robin between two equal-size groups: every
    pod_a player meets every pod_b player exactly once, via modular
    rotation (round r: pod_a[i] vs pod_b[(i+r) % n] for i in range(n)).
    """
    if len(pod_a) != len(pod_b):
        raise ValueError(
            f"bipartite_round_robin requires equal-size pods, got {len(pod_a)} and {len(pod_b)}"
        )
    n = len(pod_a)
    return [
        [(pod_a[i], pod_b[(i + r) % n]) for i in range(n)]
        for r in range(n)
    ]


def spaced_positions(count: int, total: int) -> list[int]:
    """
    `count` week numbers (1-based), evenly spaced across `total` weeks --
    used to place bye weeks (and, internally, to distribute cross-pod
    rounds evenly among in-pod rounds).
    """
    if count <= 0:
        return []
    step = total / (count + 1)
    return [int(step * (i + 1) + 0.5) for i in range(count)]


def interleave_pvp_weeks(n_in: int, n_cross: int, total_weeks: int) -> list[str]:
    """
    Returns a list of length `total_weeks` of 'in_pod' / 'cross_pod' /
    'bye', describing what kind of PVP week each week is.

    Always starts and ends on 'in_pod' (requires n_in >= n_cross).
    Built in two steps: (1) place n_in in-pod anchors with (n_in - 1)
    gaps between them; distribute the n_cross cross-pod rounds into
    those gaps as evenly as possible; (2) insert bye weeks at evenly
    spaced positions across the full week range.
    """
    if n_in < n_cross:
        raise ValueError(f"interleave_pvp_weeks requires n_in >= n_cross, got {n_in} < {n_cross}")

    n_pvp = n_in + n_cross
    n_bye = total_weeks - n_pvp
    if n_bye < 0:
        raise ValueError(f"{n_pvp} PVP round(s) don't fit in {total_weeks} weeks")

    if n_cross == 0:
        packed = ['in_pod'] * n_in
    else:
        gaps = n_in - 1
        if gaps <= 0:
            raise ValueError("interleave_pvp_weeks needs n_in >= 2 to interleave any cross_pod rounds")

        base, remainder = divmod(n_cross, gaps)
        deficit = gaps - remainder
        if remainder == 0:
            short_gaps = set(range(1, gaps + 1))
        else:
            short_gaps = set(spaced_positions(deficit, gaps))
        gap_sizes = [base if (i + 1) in short_gaps else base + 1 for i in range(gaps)]

        packed = ['in_pod']
        for size in gap_sizes:
            packed.extend(['cross_pod'] * size)
            packed.append('in_pod')

    bye_weeks = set(spaced_positions(n_bye, total_weeks))
    schedule = []
    it = iter(packed)
    for week in range(1, total_weeks + 1):
        schedule.append('bye' if week in bye_weeks else next(it))
    return schedule


def build_pvp_schedule(
    *,
    single_pod_players: list[int] | None = None,
    pod_of_player: dict[int, int] | None = None,
    pod_a: list[int] | None = None,
    pod_b: list[int] | None = None,
    regular_season_weeks: int = 11,
    single_pod_repeats: int = 3,
    two_pod_in_pod_repeats: int = 2,
) -> dict[int, list[tuple[int, int, str]]]:
    """
    Top-level orchestrator. Exactly one of `single_pod_players` or
    (`pod_a` AND `pod_b`) must be given.

    `pod_of_player` is only meaningful alongside `single_pod_players`
    (ignored/unused with `pod_a`/`pod_b`, which already know their own
    pod split): if given, each pair's matchup_type is 'in_pod' or
    'cross_pod' based on `pod_of_player[a] == pod_of_player[b]`; if
    omitted, every pair is labeled 'in_pod' (correct for a genuine
    single-pod season, where that's the only possibility).

    Returns {week: [(player_a_id, player_b_id, matchup_type), ...]} for
    week in 1..regular_season_weeks. Bye weeks map to an empty list.
    """
    single_pod_given = single_pod_players is not None
    two_pod_given = pod_a is not None or pod_b is not None
    if single_pod_given and two_pod_given:
        raise ValueError("pass either single_pod_players or pod_a+pod_b, not both")
    if not single_pod_given and not two_pod_given:
        raise ValueError("must pass single_pod_players or pod_a+pod_b")

    if single_pod_given:
        rounds = repeat_rounds(circle_method_round_robin(single_pod_players), single_pod_repeats)
        if pod_of_player is not None:
            in_pod_rounds = [
                [(a, b, 'in_pod' if pod_of_player[a] == pod_of_player[b] else 'cross_pod')
                 for a, b in rnd]
                for rnd in rounds
            ]
        else:
            in_pod_rounds = [[(a, b, 'in_pod') for a, b in rnd] for rnd in rounds]
        cross_rounds: list[list[tuple[int, int, str]]] = []
    else:
        if pod_a is None or pod_b is None:
            raise ValueError("both pod_a and pod_b are required for two-pod mode")
        if len(pod_a) != len(pod_b):
            raise ValueError("build_pvp_schedule only supports equal-size pods")

        in_pod_a = repeat_rounds(circle_method_round_robin(pod_a), two_pod_in_pod_repeats)
        in_pod_b = repeat_rounds(circle_method_round_robin(pod_b), two_pod_in_pod_repeats)
        # Same-index rounds from each pod involve entirely disjoint
        # players, so they can share one combined week.
        combined_in_pod = [ra + rb for ra, rb in zip(in_pod_a, in_pod_b)]
        cross = bipartite_round_robin(pod_a, pod_b)

        in_pod_rounds = [[(a, b, 'in_pod') for a, b in rnd] for rnd in combined_in_pod]
        cross_rounds = [[(a, b, 'cross_pod') for a, b in rnd] for rnd in cross]

    week_types = interleave_pvp_weeks(len(in_pod_rounds), len(cross_rounds), regular_season_weeks)

    schedule: dict[int, list[tuple[int, int, str]]] = {}
    in_iter = iter(in_pod_rounds)
    cross_iter = iter(cross_rounds)
    for week, wtype in zip(range(1, regular_season_weeks + 1), week_types):
        if wtype == 'bye':
            schedule[week] = []
        elif wtype == 'in_pod':
            schedule[week] = next(in_iter)
        else:
            schedule[week] = next(cross_iter)
    return schedule

"""
Unit tests for lib.optimal_lineup -- pure, no DB.

The one test that actually matters most is
test_exchange_case_beats_naive_greedy: it's the concrete proof this
problem needs real optimization (min-cost flow), not a greedy
best-team-per-slot pass -- see the module docstring for the worked
example this test encodes.
"""

import pytest

from lib.optimal_lineup import (
    TeamWeekResult,
    optimize_lineup,
    _eligible_categories,
    P4_FLEX_CATEGORY,
    G6_FLEX_CATEGORY,
    WILDCARD_CATEGORY,
)

CONFS = {'ACC': 'P4', 'B12': 'P4', 'B1G': 'P4', 'SEC': 'P4',
         'AAC': 'G6', 'CUSA': 'G6', 'MAC': 'G6', 'MWC': 'G6', 'PAC': 'G6', 'SBC': 'G6'}


def team(tid, name, conf, tier, margin):
    return TeamWeekResult(team_id=tid, name=name, conference_abbreviation=conf, tier=tier, margin=margin)


def test_single_team_fills_wildcard_over_passing():
    teams = [team(1, 'A', 'ACC', 'P4', 10)]
    result = optimize_lineup(teams, CONFS)
    assert result.total == 10
    assert len(result.picks) == 1
    assert result.picks[0].team_id == 1


def test_exchange_case_beats_naive_greedy():
    """
    The real source of exchange conflicts under these rules isn't two
    same-conference teams (every P4/G6 team always has flex/wildcard as
    a fallback, so same-conference contests never actually strand
    anyone as long as any flex capacity remains) -- it's an INDEPENDENT,
    which competes for multiple conference slots at once. With flex/
    wildcard capacity set to zero to remove that cushion entirely:

    IND (independent, P4, margin 100) is eligible for BOTH the ACC and
    B12 slots. ACCTeam (margin 90) is ACC-only. B12Team (margin 80) is
    B12-only. Only 2 slots exist for these 3 candidates.

    A naive greedy that assigns IND to the first eligible slot it finds
    (here, ACC -- CONFS_MINI lists ACC before B12) strands ACCTeam
    entirely: total = 100 (IND@ACC) + 80 (B12Team@B12) = 180. The
    optimal answer sends IND to B12 instead, freeing ACC for ACCTeam:
    100 (IND@B12) + 90 (ACCTeam@ACC) = 190, leaving B12Team unused.
    190 > 180 -- strictly better, and only reachable by actually
    weighing the trade-off, not by a first-fit/margin-order pass.
    """
    confs_mini = {'ACC': 'P4', 'B12': 'P4'}
    teams = [
        team(1, 'IND', 'P4_IND', 'P4', 100),
        team(2, 'ACCTeam', 'ACC', 'P4', 90),
        team(3, 'B12Team', 'B12', 'P4', 80),
    ]
    result = optimize_lineup(teams, confs_mini, p4_flex_slots=0, g6_flex_slots=0, wildcard_slots=0)

    assert result.total == 190
    by_id = {p.team_id: p for p in result.picks}
    assert by_id[1].category == 'B12'
    assert by_id[2].category == 'ACC'
    assert 3 not in by_id


def test_non_positive_margins_never_picked():
    teams = [
        team(1, 'Zero', 'ACC', 'P4', 0),
        team(2, 'Negative', 'B12', 'P4', -5),
        team(3, 'Bye', 'B1G', 'P4', None),
        team(4, 'Real', 'SEC', 'P4', 3),
    ]
    result = optimize_lineup(teams, CONFS)
    assert result.total == 3
    assert [p.team_id for p in result.picks] == [4]


def test_capacity_limits_respected():
    """5 P4-tier teams eligible for the 3 P4 flex slots (no conference
    or wildcard overlap to simplify) -- exactly the top 3 by margin
    should be picked, not all 5."""
    teams = [
        TeamWeekResult(i, f'T{i}', 'ZZZ_IND_P4', 'P4', margin)
        for i, margin in zip(range(1, 6), [50, 40, 30, 20, 10])
    ]
    # ZZZ_IND_P4 isn't in CONFS -> treated as a P4 independent, eligible
    # for every P4 conference slot too. To isolate flex-capacity behavior
    # specifically, use a conference_slot_tiers with no P4 conferences at
    # all, so the only P4 category available is P4_FLEX + WILDCARD.
    p4_only_flex_conf = {'AAC': 'G6'}  # no P4 conferences -> no conference-slot category for P4 teams
    result = optimize_lineup(teams, p4_only_flex_conf, p4_flex_slots=3, g6_flex_slots=2, wildcard_slots=1)
    # 3 flex + 1 wildcard = 4 slots available to P4 teams; the 4 highest margins should be used.
    assert result.total == 50 + 40 + 30 + 20
    assert len(result.picks) == 4
    picked_ids = {p.team_id for p in result.picks}
    assert picked_ids == {1, 2, 3, 4}


def test_independent_eligible_for_every_conference_slot_of_its_tier():
    indie = team(1, 'Notre Dame', 'P4_IND', 'P4', 15)
    cats = set(_eligible_categories(indie, CONFS))
    assert cats == {'ACC', 'B12', 'B1G', 'SEC', P4_FLEX_CATEGORY, WILDCARD_CATEGORY}


def test_g6_independent_eligible_only_for_g6_categories():
    indie = team(1, 'UConn', 'G6_IND', 'G6', 15)
    cats = set(_eligible_categories(indie, CONFS))
    assert cats == {'AAC', 'CUSA', 'MAC', 'MWC', 'PAC', 'SBC', G6_FLEX_CATEGORY, WILDCARD_CATEGORY}


def test_fcs_tier_has_no_eligibility():
    fcs_team = team(1, 'Some FCS School', 'FCS_CONF', 'FCS', 99)
    assert _eligible_categories(fcs_team, CONFS) == []
    result = optimize_lineup([fcs_team], CONFS)
    assert result.total == 0
    assert result.picks == []


def test_empty_pool_returns_zero():
    result = optimize_lineup([], CONFS)
    assert result.total == 0
    assert result.picks == []


def test_conference_slot_capacity_is_exactly_one():
    """
    Two ACC-eligible teams, both also P4_FLEX/WILDCARD-eligible. Both
    should get placed (dropping either would leave points on the table)
    -- but not necessarily via ACC specifically: P4_FLEX has 3 slots, so
    both landing there instead is equally valid and equally optimal.
    The one thing that must never happen is more than 1 pick in ACC
    itself (capacity exactly 1) -- that's the real invariant this test
    checks, not which specific category each team lands in.
    """
    teams = [team(1, 'Best', 'ACC', 'P4', 30), team(2, 'AlsoACC', 'ACC', 'P4', 25)]
    result = optimize_lineup(teams, CONFS)
    assert result.total == 55
    assert len(result.picks) == 2
    assert {p.team_id for p in result.picks} == {1, 2}
    acc_picks = [p for p in result.picks if p.category == 'ACC']
    assert len(acc_picks) <= 1


def test_full_realistic_fill_respects_all_capacities():
    """Enough real-shaped teams to fill every one of the 16 slots;
    confirms category counts never exceed capacity and total equals the
    hand-summed expectation."""
    teams = []
    tid = 1
    # One strong team per real conference slot.
    for conf in CONFS:
        teams.append(team(tid, f'{conf}-team', conf, CONFS[conf], 10))
        tid += 1
    # A few extra P4/G6 teams competing for flex + wildcard.
    for margin in (9, 8, 7, 6, 5, 4):
        tier = 'P4' if margin % 2 == 0 else 'G6'
        conf = 'ACC' if tier == 'P4' else 'AAC'
        teams.append(team(tid, f'extra-{margin}', conf, tier, margin))
        tid += 1

    result = optimize_lineup(teams, CONFS)
    assert len(result.picks) == 16
    by_cat = {}
    for p in result.picks:
        by_cat[p.category] = by_cat.get(p.category, 0) + 1
    for conf in CONFS:
        assert by_cat.get(conf, 0) <= 1
    assert by_cat.get(P4_FLEX_CATEGORY, 0) <= 3
    assert by_cat.get(G6_FLEX_CATEGORY, 0) <= 2
    assert by_cat.get(WILDCARD_CATEGORY, 0) <= 1
    # Total supply (16 positive-margin teams) exactly equals total
    # capacity (16 slots), and a feasible perfect assignment exists (each
    # conference-dedicated team to its own slot; the 3 P4 extras to the
    # 3 P4_FLEX slots; 2 of the 3 G6 extras to the 2 G6_FLEX slots; the
    # 3rd G6 extra to the shared WILDCARD slot, since the P4 side has no
    # leftover team needing it) -- so every team must be used in any
    # optimal solution, making the total just the sum of all margins
    # regardless of the exact slot each one lands in.
    assert result.total == 10 * 10 + sum((9, 8, 7, 6, 5, 4))


def test_prefer_team_ids_breaks_ties_without_changing_total():
    """
    Three teams tied at margin 7, only one wildcard slot (no conference
    slots at all, no flex capacity, to force wildcard as the only
    viable category and make the tie unavoidable). Without a
    preference, the solver picks *some* tied team arbitrarily. With
    prefer_team_ids naming the second team, the result must use exactly
    that one -- proving a player who actually played one of several
    equally-optimal teams gets credited with the real optimal pick,
    not an arbitrary different one. The total must be identical (7)
    either way; the tie-break bonus must never leak into it.
    """
    teams = [
        team(1, 'Alpha', 'IND', 'P4', 7),
        team(2, 'Bravo', 'IND', 'P4', 7),
        team(3, 'Charlie', 'IND', 'P4', 7),
    ]
    empty_confs = {}

    baseline = optimize_lineup(teams, empty_confs, p4_flex_slots=0, g6_flex_slots=0, wildcard_slots=1)
    assert baseline.total == 7
    assert len(baseline.picks) == 1

    preferred = optimize_lineup(
        teams, empty_confs, p4_flex_slots=0, g6_flex_slots=0, wildcard_slots=1,
        prefer_team_ids={2},
    )
    assert preferred.total == 7
    assert len(preferred.picks) == 1
    assert preferred.picks[0].team_id == 2


def test_prefer_team_ids_never_overrides_a_real_margin_difference():
    """A preferred team with a genuinely lower margin must still lose to
    a non-preferred team with a higher margin -- the tie-break bonus is
    only for breaking real ties, never for propping up a worse pick."""
    teams = [
        team(1, 'Better', 'IND', 'P4', 10),
        team(2, 'Worse-but-preferred', 'IND', 'P4', 5),
    ]
    result = optimize_lineup(
        teams, {}, p4_flex_slots=0, g6_flex_slots=0, wildcard_slots=1,
        prefer_team_ids={2},
    )
    assert result.total == 10
    assert result.picks[0].team_id == 1

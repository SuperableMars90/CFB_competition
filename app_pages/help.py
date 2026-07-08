"""
Help page — detailed, in-app instructions for using this Streamlit app
specifically (the picks form, drop/add). Game rules/strategy, standings,
and historical records live on WordPress, linked at the bottom — this
page is deliberately about the *app*, not the *game*.
"""

import streamlit as st

from lib.links import WORDPRESS_HOME_URL, WORDPRESS_STRATEGY_URL, WORDPRESS_ROSTERS_RECORDS_URL

# Auth is already gated by streamlit_app.py — this page doesn't need the
# player dict, so no lib.auth call at all (see lib/auth.py's docstring).

st.title("Help")

st.markdown(
    """
## Submitting your lineup

Each week you fill **16 slots**:

- **10 conference slots** (4 Power-4 + 6 Group-of-6) — each must be filled by one of
  your teams from that exact conference, **or by a Power-4 Independent in any P4 slot /
  a Group-of-6 Independent in any G6 slot** (an independent counts as a member of
  every conference in its own tier), or passed.
- **3 P4 flex slots** — any of your Power-4-tier teams, including P4 Independents.
- **2 G6 flex slots** — any of your Group-of-6-tier teams, including G6 Independents.
- **1 wildcard slot** — any Power-4 or Group-of-6 team, including either tier of Independent.

**Passing** a slot is always allowed. It scores an automatic zero and never goes
negative — a reasonable choice if your only eligible team is a lopsided underdog, or
if you don't own an eligible team for that slot at all.

**Duplicate teams:** the same team can't fill two slots for the same game. It *can*
appear in two slots if one is a banked Week 0 game and the other is the current
week's game for that team — those are tracked separately.

Click **Review lineup** to check for errors before submitting, then **Confirm & submit**.
Resubmitting a week overwrites your previous lineup for that week (you'll see a warning
first) — until it locks, at which point it can no longer be edited.

## Week 0 banked games

If you elected a Week 0 game for a team, it shows up inline in that team's dropdown
options going forward, labeled **(week 0)**, until you deploy it. There's a deadline —
the picks page will warn you as it approaches and error once you've hit it; undeployed
elections past the deadline get assigned by the commissioner.

## Drop / Add

Use the **Drop / Add** page to request a roster change — drop a team, add a team, or
both in one request. A commissioner has to approve it before it takes effect; you'll
see the outcome (*pending* / *approved* / *denied*) if you revisit that page later —
there's no notification yet, so it's worth checking back.

There's currently no way to cancel your own pending request in the app — if you need
to withdraw one, just text Zach and he'll deny it for you.

## Rosters

The **Rosters** page shows everyone's current teams plus which teams are still
unclaimed free agents. It's read-only — to actually change a roster, use Drop / Add.

## History

The **History** page shows your submitted lineup for any past week — pick a week to
see what you played, including opponent and final score once a game's finished. It's
read-only; to change a lineup that hasn't locked yet, use Submit Lineup instead.
"""
)

st.divider()
st.markdown(
    f"**Looking for game rules, strategy, live standings, or historical records/awards?**"
    f" Those live on the WordPress site, not in this app:\n\n"
    f"- [CFB Fantasy Football (home)]({WORDPRESS_HOME_URL})\n"
    f"- [Strategy & rules]({WORDPRESS_STRATEGY_URL})\n"
    f"- [Standings, rosters & records]({WORDPRESS_ROSTERS_RECORDS_URL})"
)

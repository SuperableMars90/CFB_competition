# CFB Competition

A season-long college football fantasy game for a small group of friends —
draft real FBS teams, start a lineup every week, and score points off
each team's actual point margin. Built to run our own league; posted here
as a work-in-progress, not a polished product.

> **Status: alpha, rough edges expected.** This is a personal project
> being posted mostly as a portfolio piece, a conversation starter, and
> an open door if anyone wants to poke at it or riff on the idea — not
> a finished, general-purpose product. Several setup steps are still
> manual SQL rather than a script (see `docs/commissioner_checklist.md`),
> only one real deployment has ever exercised this code, and the
> "What's not" section below is a real, current list of gaps — not
> exhaustive filler. If you try to stand this up for your own league,
> expect to find sharp edges and maybe fix a few yourself.

## The actual game

Each player drafts a season-configured number of real FBS teams before
the season starts — 25 in the original 4-player-pod format, 30 in a
3-player-pod one; pod size and count are set once at season creation,
not hardcoded. Every week, you set a 16-slot lineup, and that part never
changes regardless of pod format:

- one team for each of 10 conferences (4 Power-4, 6 Group-of-6)
- 3 extra "flex" slots for any P4 team
- 2 extra flex slots for any G6 team
- 1 wildcard slot, any team at all

You're scored on how each started team actually did that week — net
point margin in their real game, summed across your lineup. Two things
stack on top of that: **match play** (weekly placement among the other
players) and **point play** (cumulative season totals). If your league
is big enough to split into two pods, cross-pod matchups, pod-vs-pod
bonuses, and a shared overall leaderboard all layer in on top of the
per-pod scoring.

Past the regular season, a round-robin PVP schedule feeds into seeding
for a bracket playoff (4-, 6-, or 8-team, depending on pod count and
size), with weekly advancement through to a champion.

Every week, you also get a recap: your score against the best lineup
you *could* have started from your own roster, against a free-agent-only
"Scrappy" baseline, and against the best possible lineup from every FBS
team that existed that week ("Max") — a real weighted-assignment
optimizer under the hood, not a greedy guess.

## What's actually built

- Full lineup submission, validation, and scoring (Streamlit app)
- Live in-app draft, or a CSV/XLSX import path for a draft run elsewhere
- Drop/add requests with commissioner approve/deny
- Full PVP scheduling, seeding, and bracket playoff engine
- Weekly live-scoring and post-week recap pages (pushed to WordPress,
  if you use that — entirely optional)
- 200+ automated tests

## What's not

- No email/notification system — a commissioner finds out about a
  pending drop/add by checking the page
- A handful of one-time season-setup steps are still raw SQL, not a
  script (documented, not hidden — see the commissioner checklist)
- Login is currently case-sensitive, spreads/power-rankings aren't
  built, and a few other small UX rough edges are tracked but unfixed

None of that is a secret — it's written down candidly rather than
smoothed over, because the point of posting this isn't to look finished.

## Getting started

- [`docs/INSTALL.md`](docs/INSTALL.md) — clone-to-running-app, on a
  fresh machine
- [`docs/commissioner_checklist.md`](docs/commissioner_checklist.md) —
  every script you'd run, grouped by how often (once a season, weekly,
  playoffs)

## Stack

Python, Streamlit, MySQL (built and tested against Aiven's managed
MySQL), the [CollegeFootballData.com](https://collegefootballdata.com)
API for real schedules/scores, optional WordPress push for public
league pages. No frontend framework, no build step — a Streamlit app
plus a handful of CLI scripts a commissioner runs by hand.

## Why post this

Mostly because it was fun to build and I wanted a public trail of it —
part portfolio, part "here's a weird, specific problem I enjoyed
solving," part open invitation. If the fantasy format, the scoring
math, the scheduling/bracket logic, or the "run real analytics on a
friend group's fantasy league" idea in general is interesting to you,
I'd genuinely like to hear about it — open an issue, or just fork it
and take the idea somewhere I didn't.

No license has been chosen yet, so treat the code as all-rights-reserved
until that's sorted out — reach out if you want to do something with it
in the meantime.

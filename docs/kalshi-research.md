# Kalshi research program (paper trading)

A personal research project that runs on this server: an LLM agent plus deterministic Python that studies
[Kalshi](https://kalshi.com) prediction markets, forecasts them, and keeps score against the market price.
It trades **on paper only**. The code is in a separate private repository; this page describes what it does,
how it is built, and what it has found.

Status as of 2026-10-26: the forecasting and scoring pipeline runs every day; the LLM research loop was
retired after it lost to the market (below); two paper simulations of maker (resting-order) strategies are
collecting data for a pre-registered verdict. A third, liquidity-reward farming, was retired on 2026-10-22: the
capital it would need is far beyond what this project will ever risk, and it trades no real money.

## Why this project

Prediction markets are a clean test of whether an AI system can add information to a price. Every contract
settles to a public, checkable number, prices are probabilities, and the score is unambiguous. It was also a
way to put the local Qwen model on this box to work on a long-running agent task with real feedback, and to
build the engineering around it: scheduling, data quality, monitoring, and reviews.

## Design rule: the LLM never controls risk

The model's job is to read evidence and write probabilities. Everything that touches money is deterministic
Python with tests:

- **Fees** come from each series' published fee schedule, never a hard-coded rate.
- **Prices** come from walking the live order book for the size wanted, never the mid.
- **Sizing** is fractional Kelly with exposure, per-position, daily-loss and drawdown limits.
- **Pre-trade checks** (13 of them) fail closed: stale book, missing fee schedule, risk limit, and so on.
- **There is no live order path in the code.** A test asserts that the execution module has no order
  endpoint and no "live" tool. Paper trades go to a Postgres ledger that records everything a live trade would.

## How it is built

| Part | What it does |
|---|---|
| MCP server (Python, 25 typed tools) | market data, research bundles, quant engine, risk gates, paper execution, scoring |
| Market-data collector | read-only WebSocket feed keeps ~600 order books live (~1,300 before the reward series were dropped 2026-10-25); records quotes for the simulations |
| Agent runtime | [Hermes Agent](https://github.com/NousResearch/hermes-agent) on a schedule, using the Qwen3.8-27B model served from this box ([inference.md](inference.md)) |
| Storage | Postgres (exact decimals for money), Qdrant for retrieving past research notes and lessons |
| Scheduled jobs | settle and score (hourly), statistical baseline forecasts (twice daily), a daily digest to my phone |
| Monitoring | a health check with ~35 rules (job freshness, data gaps, settlement backlog), Grafana dashboards, alerts |
| Reviews | daily automated reviews by separate AI agents (Claude Code, Codex, Gemini) that read the reports and file findings as mail; fixes go through tests, a backup, and a deploy that rolls back if unhealthy |

## What it researches

**Main market: daily high and low temperature.** 40 ladders (high and low for 20 US cities), each a set of
2-degree brackets that settle on the National Weather Service's daily climate report for one station.

Inputs per ladder:

- Weather Service products: the National Blend of Models (NBM) and its station text guidance, forecasters'
  discussions, model runs, and live station observations.
- A **statistical baseline** that corrects the NBM for each station's recent errors, and turns it into a
  probability for every bracket. Its sigma and bias are calibrated on settled days.
- Climatology and base rates from the station record.
- For the LLM loop (now retired): all of the above plus lessons written from its own past mistakes.

Every forecast is stored whether or not it trades, and each one is scored (Brier score) against the
settlement and against the market price at the same moment.

**Other markets screened** with the same method (calibration on settled history, then a paper test):
sports game winners, rain, gas prices, CPI releases, speech "mention" markets, ERCOT power demand,
15-minute crypto, liquidity-reward programs, and Kalshi vs Polymarket price gaps.

## What we learned

1. **The market is a strong forecaster.** A Kalshi price already contains the Weather Service forecasts and
   traders watching live readings. Correcting the NBM for each station's recent errors
   improves it, but the result still does not beat the market.
2. **The LLM did not beat the market.** Over 10 settlement days of day-before forecasts the market's Brier
   score was 0.098 vs the LLM's 0.106; on same-day forecasts (1,475 brackets) 0.102 vs 0.110. The research
   loop was retired after that test. Averaging several LLM answers (an ensemble) gave no gain: the samples
   were nearly identical.
3. **A machine-learning model did not either.** An ML model trained on the same inputs, stacked with the
   market price, scored the same as the market alone (the difference was inside the noise).
4. **Cheap YES contracts are overpriced (longshot bias).** On ~4,800 settled temperature brackets, YES priced
   5-10c won about 3% of the time. Buying cheap YES lost 6-7c per contract after fees, the worst of any band,
   so a risk gate now blocks it. The same bias showed up in mention markets.
5. **Taking liquidity loses almost everywhere.** A screen of ~93,000 settled markets across categories, with a
   holdout, found no taker edge that survived fees and spread. CPI, gas and power demand: the market beat the
   public nowcasts. Sports: favorites win slightly less often than priced, but the spread eats it.
6. **The only leads are on the maker side, and they are small.** Resting orders on one side of some sports
   games earned about +2c per contract historically (90% range above zero, but shrinking over the year).
   Favorite-side quoting looks negative so far. Liquidity rewards look large until you count
   the cost of being picked off and the capital they tie up (that simulation was retired 2026-10-22). The
   maker strategies run as paper simulations with the verdict rule written down in advance (about 30
   settlement days); no verdict yet.
7. **Settlement details decide everything.** The climate day runs midnight to midnight local standard time,
   so the "daily high" can be set at 1 a.m. Some stations send an afternoon report with the day so far,
   which was once cached as the final and threw the calibration off by 17-28 degrees. Fixes include the stored data,
   not just the code.
8. **Score carefully or fool yourself.** Score each bracket's first forecast per timing window, not the
   latest (late same-day forecasts look brilliant because the answer is nearly known). Fewer than ~10
   settlement days is noise, because one day's weather moves every bracket together. Prospective results
   count; reconstructions and slices found after looking do not.

## Engineering lessons

- Verify a fix through the real path (the live job or tool), not a shortcut: several bugs survived tests
  that exercised a helper instead of the job.
- A bug that skewed stored data gets the data repaired too, with a backup first.
- An agent's description of its own code can be stale; check the source, not the agent's summary.
- Every change: backup, tests, deploy at once with automatic rollback, then confirm through the live job.

## What is not public

The code, the exact gate thresholds and simulation rules, the data, and the open leads' details stay
private. Nothing here is trading advice; the project trades no real money.

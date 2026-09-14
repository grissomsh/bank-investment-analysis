# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

A股银行投资定量分析系统 (A-share bank investment analysis) — a four-layer quantitative framework for bank stock / bank ETF investing: data layer → sector temperature (ETF timing) → per-bank five-dimension cross-sectional scoring → portfolio hints. This repo is the source for a Claude Code skill; setup.sh installs the skill (SKILL.md + scripts + references) into `~/.claude/skills/bank-investment-analysis`, where Claude Code picks it up; runtime artifacts land in that install dir.s `workspace/` (env `BANK_WORKSPACE` overrides).

## Commands

Pure Python stdlib + akshare (the single external dependency). Run directly:

```bash
python3 scripts/bank_analysis.py                    # full pipeline: fetch → temperature → score → console+JSON+HTML
python3 scripts/bank_analysis.py --detail 600036    # additionally print one bank's metric detail block
python3 scripts/bank_analysis.py --no-html          # skip HTML report
python3 scripts/bank_analysis.py --report 601128    # download annual/interim report PDFs (cninfo) into reports/<fiscal-year>/
python3 scripts/bank_analysis.py --healthcheck      # probe all data sources + SQLite, exit 1 on failure
python3 scripts/bank_daily_brief.py                 # daily brief: macro + pool announcements + news + dzjy/research → workspace/brief/
python3 scripts/bank_analysis.py --stats            # local csindex archive status
bash setup.sh                                       # install into ~/.claude/skills/ (reuses ~/.etf-skill/venv when present)
python3 tests/test_scoring.py                       # offline scoring regression (103 checks, no network)
```

**On this machine**: Homebrew Python is PEP 668-externally-managed. Use an existing venv with akshare — `~/.etf-skill/venv/bin/python` (from the etf-three-factor skill) or the `venv/` inside the skill dir created by setup.sh.

## Architecture

### Pipeline (`scripts/bank_analysis.py`, `run()`)

1. Fetch 中证银行指数 (`sz399986`) daily K-line from Tencent (~320 bars) → MA250 deviation.
2. Fetch CSI official index valuation (last ~20 days only) → upsert into SQLite for long-term accumulation. Index dividend yield (近12M `股息率1`) minus 10Y treasury yield (`bond_zh_us_rate`) = 股债利差.
3. Per bank (hardcoded 42-name pool): East Money F10 main indicators (`stock_financial_analysis_indicator_em`, SECUCODE format like `600036.SH`) which uniquely carries **bank-specific fields** (NIM/NPL/provision coverage/CET1), plus valuation history since 2018 (`stock_value_em`).
4. Build sector median-PB series from constituent PB histories (equal-weight median, needs ≥50% of banks valid per day) → its own-history percentile.
5. Sector temperature = PB-percentile×50% + yield-spread×35% + momentum×15% (missing factors are dropped and weights renormalized).
6. Five-dimension scoring by cross-sectional midrank percentiles; missing items renormalize weights within dimension; missing dimensions renormalize total weight (`覆盖度` shows coverage). Gates apply hard downgrades after scoring.
7. Outputs: console table, JSON, HTML (template uses @TOKEN@ replacement — never %-formatting or .format, CSS braces/percent signs break both). Report header shows both generation time (`ts`) and data date (`data_date` = index's report trading day); JSON carries both fields top-level.

The daily brief (`bank_daily_brief.py`, cron 21:30, output `workspace/brief/`) is a qualitative material layer — it never feeds the scoring model. Interpretation methodology (社融→盈利传导、三层回答法、月度验证清单) lives in `references/bank_framework.md` §7; when analyzing a brief in-session, follow its §7.5 four-part structure (节奏/结构/主题/风险) and declare any stale sources up front.

### Model constants

All weights/thresholds/piecewise mappings live in `scripts/bank_universe.py` (`WEIGHTS`, `GATES`, `CONFIDENCE`, `PB_PCTILE_MAP`, `SPREAD_MAP`, `SECTOR_WEIGHTS`). They are theory-driven heuristics, NOT statistically calibrated. Any change requires re-running `tests/test_scoring.py`; deeper rationale and the field-semantics pitfalls are documented in `references/bank_framework.md`. Since v1.2, `WEIGHTS` includes YoY marginal factors (`nim_chg`, `npl_chg`) computed in `enrich_with_prev_year` from the full report-period DataFrame; per-bank `可信度` (high/mid/low) is an annotation only — it never alters scores or levels.

### Field semantics (verified empirically — do not "fix" back)

- `BLDKBBL` sounds like 拨贷比 but actually holds **拨备覆盖率(%)**; real 拨贷比 is `LOAN_PROVISION_RATIO`.
- Capital adequacy mapping: `HXYJBCZL`=核心一级, `FIRST_ADEQUACY_RATIO`=一级, `NEWCAPITALADER`=总资本充足率.
- Quarterly ROE/ROA are annualized by cumulative-period factor (Q1×4, H1×2, 9M×4/3) — a coarse comparability approximation, disclosed in reports/docs.
- Per-bank disclosure dates differ (some banks still on Q1 during interim season); every row keeps its 报告期 label rather than being silently mixed.

## Testing philosophy

Offline regression in `tests/test_scoring.py`: pure compute functions (percentile ranking, piecewise maps, annualization, gates, renormalization, fallback behavior) verified against hand-computed expectations. Network fetchers are never imported for these. When a model constant changes, update expectations only if the *behavioral intent* changed — investigate failures instead of loosening them.

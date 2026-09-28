# CMN-C2-259 — Enterprise Deal Scoring & Pipeline Priority Agent

> **Category**: Cat 2 (domain workflow)
> **Industry**: CMN

## Overview

Enterprise Deal Scoring & Pipeline Priority Agent reads open sales opportunities from a
configured read-only deals table and returns a ranked pipeline review: an explainable
deterministic win-probability score per deal, a rule-based priority band, an explicit factor
coverage figure, a stagnation flag, and — for deals that carry free-text notes — qualitative risks
and suggested next actions drafted by a language model.

It suits a weekly pipeline review in an organisation whose deal data lives in a plain table rather
than in a CRM with built-in scoring: a sales manager looking for stalled deals and where to spend
the week, a rep asking what to do next on their own accounts, or a sales-operations team that
wants an ad-hoc prioritisation habit written down as a rule table instead of carried in people's
heads.

The separation between the computed score and the model's reading of the notes is the point of
the design:

- **The score is deterministic and inspectable.** It is a weighted combination of phase, amount,
  recency and BANT completeness, with every weight, the phase scale, the amount ceiling and the
  recency horizon in `config/config.yaml`. Nothing about the number comes from the model.
- **Missing data is shown, not imputed.** Each deal reports coverage as *M of N* factors actually
  present, and a deal below the configured ratio is marked for manual review rather than scored
  confidently on thin input. Per-deal validation issues are listed alongside the score.
- **The model's findings sit in their own columns.** Qualitative risks and recommended actions are
  rendered as separate columns from the score, so an AI observation is never mistaken for part of
  the computed figure. Only deals whose notes are non-empty are sent, capped by `llm.max_deals`.
- **Priority is a rule table, not a model decision.** A score-and-stagnation routing table maps
  each deal to a priority band, so the ranking policy is a config change rather than a code change.

The agent does not create or clean source deal data, does not write back to a CRM, and does not
predict outcomes. It orders and annotates what the table already says, for a person to act on.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Integration

Provide:

- an AgentCore runtime;
- a deals table reachable through the configured backend. As shipped this is a SQLite database
  whose file path comes from the `DEALS_DB_PATH` secret; `src/services/deal_repository_factory.py`
  is the single seam where another `BaseDatabase` implementation is substituted;
- an LLM API key for the configured provider — with `llm.provider: anthropic` (the default) that
  is `ANTHROPIC_API_KEY`, named by `llm.api_key_secret`. `openai` and `bedrock` are also
  supported; the client is imported lazily, so only the provider actually configured has to be
  installed;
- caller authentication in front of the entry adapter.

Runtime parameters read from `config/config.yaml` (shipped defaults in parentheses):

| Key | Meaning |
|---|---|
| `database.table` (`deals`), `database.path_secret_key` (`DEALS_DB_PATH`) | Which table to read, and the secret holding the database location |
| `column_map` (`{}`) | `{canonical name: actual column}` — absorbs a table whose column names differ, without a code change |
| `scoring.weights` (phase .35 / amount .25 / recency .20 / bant .20) | Relative weight of each score factor |
| `scoring.phase_scores`, `scoring.amount_ceiling`, `scoring.recency_horizon_days` | The per-factor scales the weights are applied to |
| `stagnation_days` (`14`) | Days without an update after which a deal is flagged stagnant |
| `routing_table` (5 rules) | Score + stagnation → priority band (`P1-urgent` … `P4-low`), evaluated in order |
| `low_coverage_flag_ratio` (`0.4`) | Coverage below this marks the deal for manual review |
| `llm.provider` / `llm.model` / `llm.temperature` / `llm.api_key_secret` / `llm.max_deals` (`20`) | Which provider to build, and how many note-bearing deals to analyse per run |
| `max_retry` (`3`), `timeout_seconds` (`30`), `memory_enabled` (`false`), `hitl.enabled` (`false`) | Framework-level runtime settings |

The configuration is trusted and comes from this file only. A caller cannot supply it: the entry
adapter drops every request key except the two public filters, and the graph re-injects the
trusted configuration before the inner workflow runs.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent is built for the platform and connects to it at start-up. Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Supplied by the platform's own package registry at build time. It is not published on public PyPI, so `pip install -e .` alone does not fetch it. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The agent does not fall back to a degraded mode when the framework is missing: `src/api/server.py`
imports `framework.*` and `shared.*` at module scope and builds the graph at import time, so
without the AgentCore package the process fails to start. It also refuses to start on a missing or
malformed `config/config.yaml` — the runtime configuration is required, not optional. With the
framework present but no platform around it the server does start and answers `/health`, but it
configures no caller authentication, so `/invoke` answers **401** until `INVOKE_AUTH_TOKEN` is set
or verifying middleware is placed in front, and no secrets are provisioned, so a run that reaches
the database or the model ends in error. The test suite runs without a platform connection.

## Input

`POST /invoke` takes a JSON body with `input`, an optional `session_id`, and an optional
`input_context`. The deals are read from the database, not from the request — `input` is not
parsed as data, and a short marker such as `"run"` is enough.

Only two `input_context` keys are honoured, and they are applied as equality filters on the query:

| Key | Effect |
|---|---|
| `owner` | Restrict to one deal owner |
| `phase` | Restrict to one pipeline phase |

Everything else a caller sends is **stripped at the entry boundary** — including `today`, which
would otherwise let a caller move the clock and reorder priorities by faking recency and
stagnation, and `_config`, which would let a caller point the agent at a different table or
rewrite the scoring policy. Unknown keys are dropped rather than passed through.

Callers must be authenticated. The entry adapter uses a trust level stamped by upstream middleware
if one is present; otherwise, when `INVOKE_AUTH_TOKEN` is set on the server environment, a matching
`Authorization: Bearer` header promotes the caller to `VERIFIED_EXTERNAL` (a mismatched token is
**401**). A caller that is neither middleware-verified nor token-authenticated is rejected with
**401** — there is no anonymous path into the graph, and every node requires `VERIFIED_EXTERNAL`
regardless.

```bash
curl -X POST http://127.0.0.1:8000/invoke \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ${INVOKE_AUTH_TOKEN}" \
  -d '{
    "input": "run",
    "session_id": "demo-001",
    "input_context": {"owner": "sales-01", "phase": "proposal"}
  }'
```

The deals table is expected to carry `deal_id` (required — a row without one is skipped),
`deal_name`, `account_name`, `phase`, `amount`, `last_updated`, the four BANT fields
(`bant_budget`, `bant_authority`, `bant_need`, `bant_timeline`), `notes` and `owner`. Every column
except `deal_id` is optional; a missing one becomes a recorded coverage gap rather than an error.

## Output

The standard AgentCore envelope is returned, with the finished Markdown report in `output`. The
report is a single ranked table — priority, deal, account, score, coverage, stagnation, the
deterministic findings (including any manual-review flag and the list of missing factors), and the
two model-written columns.

Two states short-circuit to a one-line report instead:

- **No rows matched** the filters — the report says so rather than rendering an empty table.
- **Every fetched row lacked `deal_id`** — the report warns that the table may not match the
  expected schema and points at `column_map`, rather than silently returning nothing.

Priority bands come from the routing table in first-match order; with the shipped rules that is
`P1-urgent` (score ≥ 0.7 **and** stagnant), `P2-high` (score ≥ 0.7, or ≥ 0.4 and stagnant),
`P3-normal` (score ≥ 0.4), `P4-low` (below that). The report is written in Japanese.

## Security and Limitations

Every node requires verified-external trust, so an anonymous caller is stopped before any node
body runs. Personal data is handled in two passes: contact columns are dropped from each row as it
is read, before it ever enters state; then an input-gate hook scans every remaining string field —
not just `notes` — and masks residual personal data in place, so nothing raw reaches the inner
graph, the model, or the report. On the way out, the rendered report is scanned again and any
detected span is redacted. The output pass masks rather than blocks, deliberately: a report is
still useful with a redaction in it.

Known limits, recorded rather than open:

- **The score is only as good as the columns present.** Coverage is reported as *M of N* and a
  thin deal is flagged, but the agent does not refuse to score it. Treat a low-coverage row as a
  prompt to check the record, not as a ranking.
- **`phase` values are taken as given.** The phase scale is a config lookup; a value outside it
  contributes nothing and shows up as a coverage gap rather than an error.
- **A model failure fails the run.** The qualitative step does not swallow provider errors — an
  exception propagates out of the inner workflow and the invocation ends in error rather than
  returning a partially annotated report. A run over deals that have no notes never calls the
  model at all, so it needs no key.
- **Analysis is capped.** At most `llm.max_deals` note-bearing deals are analysed per run; beyond
  that, deals keep their deterministic score and priority with empty qualitative columns.
- **Stagnation is measured from `last_updated`.** A table whose update timestamp does not track
  real activity will report stagnation that does not exist, or miss it.

Output is machine-generated and assistive. The qualitative findings in particular are a reading of
free text, not a fact about the deal; any decision that matters should be made against the primary
record by a person.

## Release

- Version: 1.0.0
- Category: fixed multi-step domain workflow (Cat 2) — an outer pipeline whose main step wraps an
  inner scoring workflow
- Generation mode: `llm` — the qualitative columns only; the score, coverage, stagnation and
  priority are deterministic
- HITL: not enabled
- Persistence: none — no checkpointer is configured
- Secrets: `DEALS_DB_PATH`, `ANTHROPIC_API_KEY` (the latter named by `llm.api_key_secret`); the
  entry adapter additionally reads `INVOKE_AUTH_TOKEN` from the environment when caller-token
  authentication is in use

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection, without a database and without a language model, using
fakes and an in-process SQLite fixture. Running the agent itself needs all three.

## Documentation

`docs/02_design.md` carries the node flow, the state schema, the expected deals-table interface,
the security-layer mapping and the design decision record. `docs/03_test_spec.md` records the test
specification and results.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit, integration and boundary tests
config/       agent configuration
docs/         design and test specifications
```

See `docs/02_design.md` for the design and `docs/03_test_spec.md` for the test specification.

## Customising

1. Adjust `config/` for your own environment and policies.
2. Replace the knowledge sources and sample data with your own.
3. Review the node implementations under `src/nodes/` for domain-specific logic.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.

# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A daily bioinformatics-literature pipeline (`收集 → AI 精筛 → 归档 → 推送`) that replaces two
Hermes cron jobs and runs on GitHub Actions. Focus areas (encoded in `feed.py` queries and the
`curate.py` system prompt): RNA modification (m6A/pseudouridine/m5C), Nanopore Direct RNA
Sequencing methods/tooling, and AI applications in bioinformatics.

## Commands

```bash
pip install -e ".[dev]"          # install (editable + pytest)
pytest                           # run all tests (network-free smoke tests)
pytest tests/test_smoke.py::test_paper_key_doi_priority   # single test
ruff check                       # lint

# Manual pipeline stages (need .env loaded: set -a; source .env; set +a)
bio-2-info feed                  # PubMed + bioRxiv → candidates_<today>.json
bio-2-info curate                # LLM picks/summarizes → selected_<today>.json
bio-2-info archive --skip-ima    # selected → digest_<today>.md (no IMA upload)

# Composite jobs (what CI runs)
bio-2-info run-feed --dry-run    # feed → curate → render (no Telegram send)
bio-2-info run-archive --dry-run --skip-ima
```

There is no host Python toolchain expectation beyond `pip install -e`; **runtime deps are zero
(stdlib only)** — only `pytest` is a dev dep. Do not add pip dependencies; the whole design goal
is that CI never has to install third-party packages.

## Architecture

Four stages, each a module under `src/bio_2_info/`, wired together by `__main__.py`:

| Stage | Module | Input | Output |
|-------|--------|-------|--------|
| feed | `feed.py` | PubMed E-utilities + bioRxiv API | `candidates_<YYYYMMDD>.json` |
| curate | `curate.py` | candidates | `selected_<YYYYMMDD>.json` |
| archive | `archive.py` | selected | `digest_<YYYYMMDD>.md` + IMA upload |
| notify | `notify.py` | selected + archive summary | one combined Telegram message |

Key cross-cutting concepts a single file won't reveal:

- **Stages communicate through dated files in `--data-dir` (default `data/digests/`)**, not
  function returns. Each `cmd_*` reads `<thing>_<today>.json` by filename convention
  (`_candidates_path` / `_selected_path` in `__main__.py`). `run-feed` / `run-archive` chain the
  stages but still round-trip through these files.

- **One CI workflow, one Telegram message.** `daily.yml` (one `workflow_dispatch`, triggered by
  cron-job.org) runs `run-feed` then `run-archive` in a single job. `run-feed` only fetches +
  curates (no send); `run-archive` reads the `selected_<today>.json` from the same runner's disk,
  archives, then sends a **single combined message** (`render_feed_message(sel, trailer=
  render_archive_line(summary))` — digest body + a one-line archive status + CI footer) and records
  the ledgers. Archival is wrapped in `try/except`, so the digest is pushed even if IMA throws (the
  trailer just shows a failure note); `run-archive` then returns non-zero. The `archive` step is
  `continue-on-error` and the commit step is `if: always()`, so `pushed_ledger.json` always gets
  committed even when IMA archival fails (otherwise papers would be re-pushed the next day), while a
  trailing gate still reddens CI on archive failure.

- **Two independent cross-day dedup ledgers, both keyed by `archive.paper_key()`**
  (`doi:<doi>` preferred, else `title:<first 80 chars>`) and both committed back to the repo:
  - `archived_ledger.json` (written by `archive.py` during `run-archive`) — skips re-uploading PDFs to IMA.
  - `pushed_ledger.json` (written by `__main__._record_pushed` in `run-archive` after the combined
    send, committed by `daily.yml`) — records every paper actually pushed to Telegram. `cmd_feed`
    calls `_filter_already_pushed` to drop these from candidates **before** curation, so a paper is
    never re-curated or re-notified on a later day. Only *pushed* (selected + sent) papers are
    recorded; fetched-but-unselected papers stay eligible for future days. Recording happens only on
    a real send, not `--dry-run`.

- **`_bucket` / `priority` flow end-to-end.** `feed.py` tags each paper with `_bucket`
  (`nanopore_drs`/`rna_mod`/`core`/`ai_bioinfo`/`ai_application`); `curate.py`'s prompt turns those
  into `priority` (🥇/🥈/🥉); `notify.py` and `archive.py` group/sort output by priority. If you
  change bucket names in `feed.py`, update the curate prompt and the section maps in `notify.py`.

- **LLM backend is any OpenAI-compatible Chat Completions endpoint** (default Zhipu GLM-4.6).
  Configured via `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL`. `curate.py` is lenient about models
  that wrap JSON in ```` ```json ```` fences and backfills missing `link`/`journal`/`date`/`_bucket`
  from the original candidate so the model can't fabricate links.

- **IMA archival shells out to vendored Node scripts** under `vendor/ima/*.cjs`
  (`ima_api.cjs`, `knowledge-base/scripts/cos-upload.cjs`) via `subprocess` — Node ≥18, no npm
  deps. PDFs come from Europe PMC OA lookup (`epmc_lookup` → `download_pdf`). Anything IMA-related
  is gated behind `skip_ima` (CLI `--skip-ima` or `BIO_SKIP_IMA=1`) so the pipeline runs end-to-end
  without IMA credentials.

## Skip-IMA mode

`--skip-ima` / `BIO_SKIP_IMA=1` short-circuits PDF download + IMA upload, building only the local
digest markdown. Use this for local runs and any CI path lacking IMA secrets — it's the supported
degraded mode, not a hack.

## Conventions

- Modules are runnable directly (`python -m bio_2_info.feed`, etc.) reading stdin/argv and writing
  JSON to stdout, in addition to being invoked through the `bio-2-info` CLI.
- User-facing strings (digests, Telegram messages, the curate system prompt) are intentionally in
  Chinese; keep them that way.
- Tests are strictly network-free / no-LLM — never add tests that hit PubMed/bioRxiv/LLM/Telegram/IMA.

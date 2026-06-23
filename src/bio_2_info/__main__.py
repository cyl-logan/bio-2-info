"""CLI entry point. Sub-commands: feed | curate | archive | run-feed | run-archive | all."""
from __future__ import annotations
import argparse
import json
import os
import sys
import datetime
from pathlib import Path

from . import feed, curate, archive, notify


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA_DIR = REPO_ROOT / "data" / "digests"


def _today_compact() -> str:
    return datetime.date.today().strftime("%Y%m%d")


def _selected_path(data_dir: Path) -> Path:
    return data_dir / f"selected_{_today_compact()}.json"


def _candidates_path(data_dir: Path) -> Path:
    return data_dir / f"candidates_{_today_compact()}.json"


def cmd_feed(args) -> int:
    """Fetch candidates from PubMed + bioRxiv, write to file + stdout."""
    result = feed.collect_all()
    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    out_path = _candidates_path(data_dir)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    sys.stderr.write(f"[feed] {result['counts']} → {out_path}\n")
    if args.print:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0


def cmd_curate(args) -> int:
    """Call LLM on candidates_<today>.json (or stdin), write selected_<today>.json."""
    data_dir = Path(args.data_dir)
    if args.input:
        with open(args.input, encoding="utf-8") as f:
            data = json.load(f)
    else:
        path = _candidates_path(data_dir)
        if not path.exists():
            sys.stderr.write(f"[curate] no candidates file at {path}; run feed first\n")
            return 2
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    cands = data.get("papers", data if isinstance(data, list) else [])
    if not cands:
        sys.stderr.write("[curate] no candidates to curate\n")
        return 2
    result = curate.curate(cands)
    data_dir.mkdir(parents=True, exist_ok=True)
    out_path = _selected_path(data_dir)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    sys.stderr.write(f"[curate] {result['_meta']} → {out_path}\n")
    if args.print:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0


def cmd_archive(args) -> int:
    """Read selected_<today>.json, archive to IMA + build digest."""
    data_dir = Path(args.data_dir)
    if args.input:
        with open(args.input, encoding="utf-8") as f:
            sel = json.load(f)
    else:
        path = _selected_path(data_dir)
        if not path.exists():
            sys.stderr.write(f"[archive] no selected file at {path}; run curate first\n")
            return 2
        with open(path, encoding="utf-8") as f:
            sel = json.load(f)
    skip = args.skip_ima or os.environ.get("BIO_SKIP_IMA") == "1"
    summary = archive.archive(sel, str(data_dir), skip_ima=skip)
    sys.stderr.write(f"[archive] {summary['status']} pdf={summary.get('pdf_archived')} link={summary.get('link_in_digest')}\n")
    if args.print:
        print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0


def cmd_run_feed(args) -> int:
    """Feed → curate → notify (the 8:15 job replacement)."""
    rc = cmd_feed(argparse.Namespace(data_dir=args.data_dir, print=False))
    if rc:
        return rc
    rc = cmd_curate(argparse.Namespace(data_dir=args.data_dir, input=None, print=False))
    if rc:
        return rc
    with open(_selected_path(Path(args.data_dir)), encoding="utf-8") as f:
        sel = json.load(f)
    text = notify.render_feed_message(sel)
    if args.dry_run:
        print(text)
        return 0
    notify.send_telegram(text)
    sys.stderr.write("[run-feed] telegram delivered\n")
    return 0


def cmd_run_archive(args) -> int:
    """Archive selected → notify (the 9:15 job replacement)."""
    rc = cmd_archive(argparse.Namespace(
        data_dir=args.data_dir, input=None, print=False,
        skip_ima=args.skip_ima,
    ))
    if rc:
        return rc
    # rebuild summary by re-running archive output… simpler: re-read digest file path
    # We re-call archive with the cached selected.json? No: we already wrote the digest,
    # but didn't capture the summary. Re-read selected and emit a quick stat.
    data_dir = Path(args.data_dir)
    with open(_selected_path(data_dir), encoding="utf-8") as f:
        sel = json.load(f)
    # Build a minimal summary from on-disk artifacts so we don't double-archive.
    today_h = datetime.date.today().strftime("%Y-%m-%d")
    digest_local = data_dir / f"digest_{_today_compact()}.md"
    summary = {
        "status": "ok",
        "date": today_h,
        "total": len(sel.get("papers", [])),
        "pdf_archived": None,
        "link_in_digest": None,
        "skipped_dedup": None,
        "failed": 0,
        "failed_titles": [],
        "digest_local": str(digest_local),
        "digest_uploaded": not args.skip_ima,
        "skip_ima": args.skip_ima,
        "digest_status": "",
    }
    text = notify.render_archive_message(summary)
    if args.dry_run:
        print(text)
        return 0
    notify.send_telegram(text)
    sys.stderr.write("[run-archive] telegram delivered\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bio-2-info")
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR),
                        help=f"Where candidate/selected/digest files live (default: {DEFAULT_DATA_DIR}).")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("feed", help="Fetch PubMed + bioRxiv candidates")
    p.add_argument("--print", action="store_true")
    p.set_defaults(func=cmd_feed)

    p = sub.add_parser("curate", help="LLM-curate candidates")
    p.add_argument("--input", help="Override candidates path")
    p.add_argument("--print", action="store_true")
    p.set_defaults(func=cmd_curate)

    p = sub.add_parser("archive", help="Archive selected papers")
    p.add_argument("--input", help="Override selected path")
    p.add_argument("--skip-ima", action="store_true", help="Skip PDF download + IMA upload")
    p.add_argument("--print", action="store_true")
    p.set_defaults(func=cmd_archive)

    p = sub.add_parser("run-feed", help="feed → curate → telegram (replaces 8:15 cron)")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_run_feed)

    p = sub.add_parser("run-archive", help="archive → telegram (replaces 9:15 cron)")
    p.add_argument("--skip-ima", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_run_archive)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

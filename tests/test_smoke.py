"""Smoke tests — network-free, no LLM call."""
from __future__ import annotations
import json

from bio_2_info import notify, archive


def test_render_feed_empty():
    msg = notify.render_feed_message({"date": "2026-06-23", "papers": []})
    assert "每日生信资讯" in msg
    assert "2026-06-23" in msg


def test_render_feed_with_papers():
    sel = {
        "date": "2026-06-23",
        "papers": [
            {"title": "Test paper A", "priority": "🥇", "summary_cn": "做了X",
             "relevance_cn": "对DRS有用", "link": "https://example.com/a",
             "journal": "Nat Methods", "date": "2026-06-22", "_bucket": "nanopore_drs"},
            {"title": "Test paper B", "priority": "🥈", "summary_cn": "用LLM做Y",
             "link": "https://example.com/b"},
        ],
    }
    msg = notify.render_feed_message(sel)
    assert "🥇" in msg and "🥈" in msg
    assert "Test paper A" in msg
    assert "example.com/a" in msg


def test_render_archive_empty():
    msg = notify.render_archive_message({"status": "empty", "date": "2026-06-23"})
    assert "无新论文" in msg


def test_paper_key_doi_priority():
    assert archive.paper_key({"doi": "10.1/foo", "title": "x"}) == "doi:10.1/foo"
    assert archive.paper_key({"title": "Some Long  Title"}) == "title:some long title"


def test_sanitize_filename():
    assert archive.sanitize_filename("a/b:c?d", "pdf") == "a b c d.pdf"
    long = "x" * 200
    assert len(archive.sanitize_filename(long, "pdf")) <= 124


def test_archive_empty_returns_status():
    out = archive.archive({"papers": []}, "/tmp/bio_2_info_test")
    assert out["status"] == "empty"


def test_archive_skip_ima_builds_digest(tmp_path):
    sel = {
        "date": "2026-06-23",
        "papers": [{
            "title": "Test paper", "priority": "🥇",
            "summary_cn": "做了X", "relevance_cn": "DRS",
            "link": "https://example.com/a", "doi": "10.99/abc",
            "journal": "Nat Methods", "date": "2026-06-22",
            "_bucket": "nanopore_drs",
        }],
        "summary_zh": "测试摘要",
    }
    out = archive.archive(sel, str(tmp_path), skip_ima=True)
    assert out["status"] == "ok"
    assert out["skip_ima"] is True
    digest = (tmp_path / out["digest_local"]).read_text(encoding="utf-8") \
        if not out["digest_local"].startswith("/") \
        else open(out["digest_local"], encoding="utf-8").read()
    assert "Test paper" in digest
    assert "DRS" in digest
    # ledger written
    ledger = json.loads((tmp_path / "archived_ledger.json").read_text())
    assert "doi:10.99/abc" in ledger

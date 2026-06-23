"""Smoke tests — network-free, no LLM call."""
from __future__ import annotations
import json

from bio_2_info import notify, archive, curate
from bio_2_info import __main__ as cli


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


def test_filter_already_pushed():
    papers = [
        {"doi": "10.1/a", "title": "A"},
        {"doi": "10.2/b", "title": "B"},
        {"title": "No DOI paper"},
    ]
    ledger = {archive.paper_key({"doi": "10.1/a", "title": "A"}): {"date": "2026-06-22"}}
    out = cli._filter_already_pushed(papers, ledger)
    keys = {archive.paper_key(p) for p in out}
    assert archive.paper_key({"doi": "10.1/a"}) not in keys
    assert archive.paper_key({"doi": "10.2/b"}) in keys
    assert len(out) == 2


def test_record_pushed_is_idempotent(tmp_path):
    papers = [{"doi": "10.9/x", "title": "X", "link": "https://example.com/x"}]
    added = cli._record_pushed(papers, tmp_path)
    assert added == 1
    ledger = json.loads((tmp_path / "pushed_ledger.json").read_text(encoding="utf-8"))
    assert archive.paper_key(papers[0]) in ledger
    # Re-recording the same paper adds nothing new.
    assert cli._record_pushed(papers, tmp_path) == 0


def test_record_then_filter_excludes(tmp_path):
    papers = [{"doi": "10.5/dup", "title": "Dup"}, {"doi": "10.6/new", "title": "New"}]
    cli._record_pushed([papers[0]], tmp_path)
    ledger = archive.load_ledger(str(cli._pushed_ledger_path(tmp_path)))
    out = cli._filter_already_pushed(papers, ledger)
    assert len(out) == 1
    assert out[0]["doi"] == "10.6/new"


def test_append_site_data_merges_and_dedups(tmp_path):
    path = tmp_path / "papers.json"
    p1 = {"title": "A", "doi": "10.1/a", "priority": "🥇", "summary_cn": "做了A",
          "relevance_cn": "DRS", "_bucket": "nanopore_drs", "journal": "Nat Methods",
          "date": "2026-06-22", "link": "https://example.com/a", "source": "PubMed"}
    total = cli._append_site_data([p1], path=path, pushed_date="2026-06-23")
    assert total == 1
    rec = json.loads(path.read_text(encoding="utf-8"))["papers"][0]
    assert rec["key"] == archive.paper_key(p1)
    assert rec["summary_cn"] == "做了A" and rec["bucket"] == "nanopore_drs"
    assert rec["pushed_date"] == "2026-06-23"
    # Re-appending same paper later keeps earliest pushed_date, no duplication.
    total2 = cli._append_site_data([p1], path=path, pushed_date="2026-06-25")
    assert total2 == 1
    assert json.loads(path.read_text(encoding="utf-8"))["papers"][0]["pushed_date"] == "2026-06-23"


def test_curate_thinking_knob(monkeypatch):
    """LLM_THINKING env toggles the provider-specific thinking field in the payload."""
    captured = {}

    def fake_post(url, headers, payload, timeout=90):
        captured["payload"] = payload
        return {"choices": [{"message": {"content": '{"papers": [], "no_core": true}'}}]}

    monkeypatch.setattr(curate, "_post_json", fake_post)
    monkeypatch.setenv("LLM_API_KEY", "x")

    monkeypatch.delenv("LLM_THINKING", raising=False)
    curate.curate([{"title": "A", "_bucket": "core"}])
    assert "thinking" not in captured["payload"]

    monkeypatch.setenv("LLM_THINKING", "disabled")
    curate.curate([{"title": "A", "_bucket": "core"}])
    assert captured["payload"]["thinking"] == {"type": "disabled"}


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

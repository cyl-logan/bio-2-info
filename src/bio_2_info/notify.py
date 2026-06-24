"""Telegram notifier (stdlib urllib, like ssq-checker)."""
from __future__ import annotations
import json
import os
import urllib.request
import urllib.error


TG_API = "https://api.telegram.org"

# 标注消息来源：由 GitHub Actions CI 自动推送。
CI_FOOTER = "🤖 _由 GitHub Actions CI 自动推送_"


class NotifyError(RuntimeError):
    pass


def send_telegram(text: str, *, token: str | None = None,
                  chat_id: str | None = None,
                  parse_mode: str = "Markdown",
                  disable_web_page_preview: bool = True) -> dict:
    token = token or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise NotifyError("missing TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")

    url = f"{TG_API}/bot{token}/sendMessage"

    def _attempt(pm: str) -> dict:
        payload = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": disable_web_page_preview,
        }
        if pm:
            payload["parse_mode"] = pm
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=body,
                                      headers={"Content-Type": "application/json"},
                                      method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())

    try:
        data = _attempt(parse_mode)
    except urllib.error.HTTPError as e:
        # Telegram 400s when Markdown entities are malformed (e.g. a stray * / _ / [
        # in a paper title). Retry once as plain text so one bad char can't drop the
        # whole digest — losing the message would also lose the pushed_ledger record.
        if not parse_mode:
            raise NotifyError(f"HTTP {e.code}: {e.read()[:300]!r}") from e
        try:
            data = _attempt("")
        except urllib.error.HTTPError as e2:
            raise NotifyError(f"HTTP {e2.code}: {e2.read()[:300]!r}") from e2
    if not data.get("ok"):
        raise NotifyError(f"telegram api not ok: {data}")
    return data


# ---------- message rendering ----------
def render_feed_message(selected: dict, max_chars: int = 3800,
                        trailer: str = "") -> str:
    """Render the curated paper list as a Telegram-friendly Markdown message.

    `trailer` is an optional one-line status (e.g. archive result) placed between
    the digest body and the CI footer; it is reserved in the length budget so the
    whole message stays within `max_chars`.
    """
    date = selected.get("date", "")
    papers = selected.get("papers", [])
    tail = (f"\n\n{trailer}" if trailer else "") + f"\n\n{CI_FOOTER}"

    def _fit(body: str) -> str:
        """Reserve tail in the budget, truncate body, hard-clamp to max_chars."""
        budget = max_chars - len(tail)
        if len(body) > budget:
            note = "\n\n…(已截断，详见知识库 digest)"
            cut = budget - len(note)
            body = (body[:cut].rstrip() + note) if cut > 0 else ""
        result = f"{body}{tail}"
        return result if len(result) <= max_chars else result[:max_chars]

    if not papers:
        return _fit(f"📚 每日生信资讯 · {date}\n\n_今日无合格论文_")

    by_prio: dict[str, list[dict]] = {"🥇": [], "🥈": [], "🥉": []}
    for p in papers:
        pr = p.get("priority") or "🥈"
        by_prio.setdefault(pr, []).append(p)

    sec_titles = {
        "🥇": "🥇 核心方法 (RNA mod / DRS)",
        "🥈": "🥈 AI 方法与应用",
        "🥉": "🥉 值得一看",
    }

    lines = [f"📚 *每日生信资讯* · {date}", ""]
    if selected.get("no_core"):
        lines.append("_今日无核心新方法论文_")
        lines.append("")
    for pr in ("🥇", "🥈", "🥉"):
        items = by_prio.get(pr, [])
        if not items:
            continue
        lines.append(f"## {sec_titles[pr]}")
        for p in items:
            title = (p.get("title") or "").replace("*", "").replace("_", " ")
            summary = p.get("summary_cn", "")
            relevance = p.get("relevance_cn", "")
            journal = p.get("journal", "")
            date_p = p.get("date", "")
            link = p.get("link", "")
            lines.append(f"*{title}*")
            if summary:
                lines.append(f"🔹 {summary}")
            if relevance:
                lines.append(f"🔸 与你相关：{relevance}")
            if link:
                lines.append(f"🔗 [链接]({link})")
            meta = " · ".join(x for x in [journal, str(date_p)] if x)
            if meta:
                lines.append(f"_{meta}_")
            lines.append("")
        lines.append("")
    return _fit("\n".join(lines).rstrip())


def render_archive_line(summary: dict) -> str:
    """One-line archive status, used as the trailer in the combined feed message."""
    status = summary.get("status")
    if status == "empty":
        return "📥 _今日无新论文可归档_"
    if status == "error":
        return "⚠️ _归档失败（IMA 异常），未入知识库_"
    if summary.get("skip_ima"):
        return "📥 _本次跳过 IMA 上传，仅生成本地 digest_"
    total = summary.get("total", 0)
    pdf = summary.get("pdf_archived")
    link = summary.get("link_in_digest")
    skipped = summary.get("skipped_dedup")
    failed = summary.get("failed")
    detail = []
    if pdf is not None:
        detail.append(f"PDF {pdf}")
    if link is not None:
        detail.append(f"链接 {link}")
    if skipped:
        detail.append(f"去重 {skipped}")
    if failed:
        detail.append(f"失败 {failed}")
    suffix = f"（{' / '.join(detail)}）" if detail else ""
    # archive() may upload paper PDFs fine yet fail the summary-digest upload;
    # don't claim 入知识库 in that case.
    if not summary.get("digest_uploaded") and "失败" in (summary.get("digest_status") or ""):
        return f"⚠️ _已归档 {total} 篇{suffix}，但 digest 上传知识库失败_"
    return f"📥 _已归档 {total} 篇{suffix}入知识库「每日生信资讯」_"

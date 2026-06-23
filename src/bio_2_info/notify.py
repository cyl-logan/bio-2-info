"""Telegram notifier (stdlib urllib, like ssq-checker)."""
from __future__ import annotations
import json
import os
import urllib.request
import urllib.error


TG_API = "https://api.telegram.org"


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
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": disable_web_page_preview,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body,
                                  headers={"Content-Type": "application/json"},
                                  method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise NotifyError(f"HTTP {e.code}: {e.read()[:300]!r}") from e
    if not data.get("ok"):
        raise NotifyError(f"telegram api not ok: {data}")
    return data


# ---------- message rendering ----------
def render_feed_message(selected: dict, max_chars: int = 3800) -> str:
    """Render the curated paper list as a Telegram-friendly Markdown message."""
    date = selected.get("date", "")
    papers = selected.get("papers", [])
    if not papers:
        return f"📚 每日生信资讯 · {date}\n\n_今日无合格论文_"

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
    text = "\n".join(lines).rstrip()
    if len(text) > max_chars:
        text = text[:max_chars - 100].rstrip() + "\n\n…(已截断，详见知识库 digest)"
    return text


def render_archive_message(summary: dict) -> str:
    status = summary.get("status")
    date = summary.get("date", "")
    if status == "empty":
        return f"📥 *生信资讯归档* · {date}\n\n今日无新论文可归档。"
    total = summary.get("total", 0)
    pdf = summary.get("pdf_archived", 0)
    link = summary.get("link_in_digest", 0)
    failed = summary.get("failed", 0)
    skipped = summary.get("skipped_dedup", 0)
    lines = [
        f"📥 *生信资讯归档完成* · {date}",
        "",
        f"共 *{total}* 篇 → 📄 PDF {pdf} / 🔗 链接 {link}"
        + (f" / ♻️ 去重跳过 {skipped}" if skipped else "")
        + (f" / ⚠️ 失败 {failed}" if failed else ""),
    ]
    if failed and summary.get("failed_titles"):
        lines.append("")
        lines.append("⚠️ 失败标题（可能非OA或抓取异常）:")
        for t in summary["failed_titles"][:5]:
            lines.append(f"- {t[:120]}")
    if summary.get("digest_status"):
        lines.append("")
        lines.append(f"_{summary['digest_status']}_")
    if summary.get("digest_uploaded"):
        lines.append("")
        lines.append("📝 当日带摘要的 digest 已存入知识库「每日生信资讯」。")
    elif summary.get("skip_ima"):
        lines.append("")
        lines.append("_（本次跳过 IMA 上传，仅生成本地 digest）_")
    return "\n".join(lines)

"""Archive selected papers into IMA knowledge base + build digest markdown.

Adapted from ~/.hermes/scripts/research_archive.py — calls vendored node
helpers under vendor/ima/ instead of the in-tree Hermes skill path.
"""
from __future__ import annotations
import os
import sys
import json
import time
import subprocess
import tempfile
import datetime
import urllib.request
import urllib.parse
import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
VENDOR_IMA = REPO_ROOT / "vendor" / "ima"
IMA_API = VENDOR_IMA / "ima_api.cjs"
COS_UPLOAD = VENDOR_IMA / "knowledge-base" / "scripts" / "cos-upload.cjs"
NODE = os.environ.get("NODE_BIN", "node")
KB_NAME = os.environ.get("IMA_KB_NAME", "每日生信资讯")
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"


def log(*a) -> None:
    sys.stderr.write(" ".join(str(x) for x in a) + "\n")


# ---------------- IMA API helper ----------------
def _ima(api_path: str, body: dict) -> dict:
    proc = subprocess.run(
        [NODE, str(IMA_API), api_path, json.dumps(body, ensure_ascii=False)],
        capture_output=True, text=True, timeout=120,
    )
    out = (proc.stdout or "").strip()
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        raise RuntimeError(f"ima_api {api_path} failed: {err or out}")
    try:
        resp = json.loads(out)
    except Exception:
        raise RuntimeError(f"ima_api {api_path} bad json: {out[:300]}") from None
    if resp.get("code") != 0:
        raise RuntimeError(f"ima_api {api_path} code={resp.get('code')} msg={resp.get('msg')}")
    return resp.get("data", {})


def resolve_kb_id(name: str = KB_NAME) -> str:
    data = _ima("openapi/wiki/v1/search_knowledge_base", {"query": name, "cursor": "", "limit": 20})
    for item in data.get("info_list", []):
        if item.get("kb_name") == name or item.get("name") == name:
            return item.get("kb_id") or item.get("id")
    raise RuntimeError(f"KB not found: {name}")


# ---------------- Europe PMC: resolve OA PDF ----------------
def epmc_lookup(paper: dict) -> tuple[bool, str | None, str]:
    doi = (paper.get("doi") or "").strip()
    title = (paper.get("title") or "").strip()
    query = f'DOI:"{doi}"' if doi else title
    url = ("https://www.ebi.ac.uk/europepmc/webservices/rest/search?"
           + urllib.parse.urlencode({"query": query, "format": "json",
                                     "pageSize": 3, "resultType": "core"}))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        d = json.loads(urllib.request.urlopen(req, timeout=30).read())
    except Exception as e:
        log(f"[epmc err] {e}")
        return (False, None, paper.get("link", ""))
    results = d.get("resultList", {}).get("result", [])
    chosen = None
    for r in results:
        if doi and (r.get("doi", "").lower() == doi.lower()):
            chosen = r
            break
    if chosen is None and results:
        chosen = results[0]
    if not chosen:
        return (False, None, paper.get("link", ""))
    is_oa = chosen.get("isOpenAccess") == "Y"
    pmcid = chosen.get("pmcid")
    return (is_oa and bool(pmcid), pmcid, paper.get("link", ""))


def download_pdf(pmcid: str, dest: str) -> bool:
    url = f"https://europepmc.org/articles/{pmcid}?pdf=render"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=90) as r:
            data = r.read()
    except Exception as e:
        log(f"[pdf dl err] {pmcid}: {e}")
        return False
    if not data[:5].startswith(b"%PDF"):
        log(f"[pdf invalid] {pmcid}: not a PDF (got {data[:20]!r})")
        return False
    with open(dest, "wb") as f:
        f.write(data)
    return True


# ---------------- IMA file upload ----------------
def sanitize_filename(name: str, ext: str, maxlen: int = 120) -> str:
    name = re.sub(r"[\\/:*?\"<>|\n\r\t]", " ", name).strip()
    name = re.sub(r"\s+", " ", name)
    if len(name) > maxlen:
        name = name[:maxlen].rstrip()
    return f"{name}.{ext}"


def upload_file(kb_id: str, filepath: str, title: str,
                media_type: int, content_type: str, file_ext: str) -> str:
    size = os.path.getsize(filepath)
    fname = os.path.basename(filepath)
    cm = _ima("openapi/wiki/v1/create_media", {
        "file_name": fname, "file_size": size, "content_type": content_type,
        "knowledge_base_id": kb_id, "file_ext": file_ext,
    })
    media_id = cm.get("media_id")
    cred = cm.get("cos_credential", {})
    cmd = [
        NODE, str(COS_UPLOAD),
        "--file", filepath,
        "--secret-id", cred.get("secret_id", ""),
        "--secret-key", cred.get("secret_key", ""),
        "--token", cred.get("token", ""),
        "--bucket", cred.get("bucket_name", ""),
        "--region", cred.get("region", ""),
        "--cos-key", cred.get("cos_key", ""),
        "--content-type", content_type,
        "--start-time", str(cred.get("start_time", "")),
        "--expired-time", str(cred.get("expired_time", "")),
        "--timeout", "300000",
    ]
    up = subprocess.run(cmd, capture_output=True, text=True, timeout=360)
    if up.returncode != 0:
        raise RuntimeError(f"cos upload failed: {(up.stderr or up.stdout).strip()[:300]}")
    _ima("openapi/wiki/v1/add_knowledge", {
        "media_type": media_type, "media_id": media_id, "title": title,
        "knowledge_base_id": kb_id,
        "file_info": {"cos_key": cred.get("cos_key"), "file_size": size, "file_name": fname},
    })
    return media_id


def name_is_repeated(kb_id: str, fname: str, media_type: int) -> bool:
    try:
        data = _ima("openapi/wiki/v1/check_repeated_names", {
            "params": [{"name": fname, "media_type": media_type}],
            "knowledge_base_id": kb_id,
        })
        for r in data.get("results", []):
            if r.get("name") == fname:
                return bool(r.get("is_repeated"))
    except Exception as e:
        log(f"[check_repeated err] {fname}: {e}")
    return False


# ---------------- ledger ----------------
def paper_key(p: dict) -> str:
    doi = (p.get("doi") or "").strip().lower()
    if doi:
        return f"doi:{doi}"
    title = (p.get("title") or "").strip().lower()
    title = re.sub(r"\s+", " ", title)
    return f"title:{title[:80]}"


def load_ledger(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_ledger(ledger: dict, path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(ledger, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


# ---------------- main archive flow ----------------
def archive(selected: dict, data_dir: str, *, skip_ima: bool = False) -> dict:
    """Archive a curated selection. Returns summary dict for the notifier.

    selected: {"date": "YYYY-MM-DD", "papers": [...], ...} (output of curate.curate)
    data_dir: where to write digest_*.md and archived_ledger.json
    skip_ima: if True (e.g. for local dry-run or when IMA creds missing in CI),
              skip PDF download + IMA upload; only build the digest markdown.
    """
    today = datetime.date.today()
    today_compact = today.strftime("%Y%m%d")
    today_h = today.strftime("%Y-%m-%d")
    papers = selected.get("papers", [])
    if not papers:
        return {"status": "empty", "date": today_h, "msg": "名单为空，无可归档论文"}

    os.makedirs(data_dir, exist_ok=True)
    ledger_path = os.path.join(data_dir, "archived_ledger.json")
    ledger = load_ledger(ledger_path)

    kb_id = None
    tmpdir = tempfile.mkdtemp(prefix="bio_archive_")
    if not skip_ima:
        kb_id = resolve_kb_id()

    pdf_ok, link_in_digest, skipped, failed = [], [], [], []
    digest_lines = [
        f"# 每日生信资讯 · {today_h}", "",
        f"> 自动归档摘要 · 共 {len(papers)} 篇", "",
    ]
    if selected.get("summary_zh"):
        digest_lines += [f"_今日概览_: {selected['summary_zh']}", ""]

    for i, p in enumerate(papers, 1):
        title = (p.get("title") or "").strip()
        link = (p.get("link") or "").strip()
        journal = p.get("journal", "")
        date = p.get("date", "")
        bucket = p.get("_bucket") or p.get("bucket") or ""
        priority = p.get("priority", "")
        summary = (p.get("summary_cn") or "").strip()
        relevance = (p.get("relevance_cn") or "").strip()
        doi = (p.get("doi") or "").strip()
        key = paper_key(p)
        archive_link = f"https://doi.org/{doi}" if doi else link

        digest_lines.append(f"## {i}. {priority} {title}".rstrip())
        if summary:
            digest_lines.append(f"- **摘要**：{summary}")
        if relevance:
            digest_lines.append(f"- **相关性**：{relevance}")
        meta = " · ".join(x for x in [journal, str(date), bucket] if x)
        if meta:
            digest_lines.append(f"- _{meta}_")

        prev = ledger.get(key)
        if prev:
            skipped.append(title)
            tag = "PDF原文" if prev.get("pdf") else "链接"
            digest_lines.append(f"- ♻️ 此前已归档（{tag}，{prev.get('date','')}），本次不重复入库")
            digest_lines.append(f"- 🔗 [{archive_link}]({archive_link})")
            digest_lines.append("")
            continue

        archived_pdf = False
        if not skip_ima:
            try:
                is_oa, pmcid, _ = epmc_lookup(p)
                if is_oa and pmcid:
                    dest = os.path.join(tmpdir, sanitize_filename(title or pmcid, "pdf"))
                    if download_pdf(pmcid, dest):
                        fname = os.path.basename(dest)
                        if name_is_repeated(kb_id, fname, 1):
                            digest_lines.append(f"- 📄 原文 PDF 已在库中（OA, {pmcid}），跳过重复上传")
                            skipped.append(title)
                            archived_pdf = True
                        else:
                            try:
                                upload_file(kb_id, dest, fname, 1, "application/pdf", "pdf")
                                pdf_ok.append(title)
                                digest_lines.append(f"- 📄 已存原文 PDF（OA, {pmcid}）")
                                archived_pdf = True
                            except Exception as e:
                                log(f"[upload err] {title[:40]}: {e}")
                                failed.append(title)
            except Exception as e:
                log(f"[paper err] {title[:40]}: {e}")
                failed.append(title)

        if archived_pdf:
            digest_lines.append(f"- 🔗 [{archive_link}]({archive_link})")
            ledger[key] = {"date": today_h, "pdf": True, "title": title}
        else:
            digest_lines.append(f"- 🔗 链接（无OA全文）：[{archive_link}]({archive_link})")
            link_in_digest.append(title)
            ledger[key] = {"date": today_h, "pdf": False, "title": title}
        digest_lines.append("")
        time.sleep(0.3)

    # digest md
    digest_text = "\n".join(digest_lines)
    digest_fname = f"每日生信资讯_{today_h}.md"
    digest_path = os.path.join(data_dir, f"digest_{today_compact}.md")
    with open(digest_path, "w", encoding="utf-8") as f:
        f.write(digest_text)

    digest_uploaded = False
    digest_status = ""
    if not skip_ima:
        digest_upload_path = os.path.join(tmpdir, digest_fname)
        with open(digest_upload_path, "w", encoding="utf-8") as f:
            f.write(digest_text)
        try:
            if name_is_repeated(kb_id, digest_fname, 7):
                digest_status = "当日 digest 已在库中，跳过重复上传"
                log(f"[digest] {digest_status}")
            else:
                upload_file(kb_id, digest_upload_path, digest_fname, 7, "text/markdown", "md")
                digest_uploaded = True
        except Exception as e:
            log(f"[digest upload err] {e}")
            digest_status = f"digest 上传失败: {e}"

    save_ledger(ledger, ledger_path)

    return {
        "status": "ok",
        "date": today_h,
        "total": len(papers),
        "pdf_archived": len(pdf_ok),
        "link_in_digest": len(link_in_digest),
        "skipped_dedup": len(skipped),
        "failed": len(failed),
        "failed_titles": failed,
        "digest_local": digest_path,
        "digest_uploaded": digest_uploaded,
        "digest_status": digest_status,
        "skip_ima": skip_ima,
    }


if __name__ == "__main__":
    # Read selected JSON from stdin or first arg; write archive summary to stdout.
    if len(sys.argv) > 1 and sys.argv[1] not in ("-",):
        with open(sys.argv[1], encoding="utf-8") as f:
            sel = json.load(f)
    else:
        sel = json.load(sys.stdin)
    data_dir = os.environ.get("BIO_DATA_DIR", str(REPO_ROOT / "data" / "digests"))
    skip = os.environ.get("BIO_SKIP_IMA") == "1"
    out = archive(sel, data_dir, skip_ima=skip)
    print(json.dumps(out, ensure_ascii=False, indent=1))

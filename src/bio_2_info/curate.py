"""Curate candidates: call Zhipu GLM (or any OpenAI-compatible LLM) to pick + summarize.

Replaces the AI-in-the-loop step of the Hermes cron prompt.
Pure stdlib HTTP (urllib) so CI doesn't need extra deps.
"""
from __future__ import annotations
import json
import os
import sys
import urllib.request
import urllib.error
import datetime
from typing import Any


# Default points at Zhipu's standard endpoint. Override via env if needed.
DEFAULT_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
DEFAULT_MODEL = "glm-4.6"

SYSTEM_PROMPT = """你是 Logan 的每日生信文献策展专家。Logan 做生物信息研究，核心方向是 RNA 修饰（m6A/pseudouridine/m5C 等表观转录组）和 Nanopore Direct RNA Sequencing (DRS)，关注检测方法、basecalling、modification calling、信号处理、工具/pipeline，以及 AI 在生信/生物中的应用。

候选论文的 _bucket 字段含义：
- nanopore_drs / rna_mod / core = 核心方向（RNA修饰、DRS方法工具）
- ai_bioinfo = AI方法学本身（foundation model、transformer、深度学习架构）
- ai_application = AI 应用案例（别人怎么用ML/AI/AlphaFold/LLM/扩散模型去解决具体生物学问题）

【筛选与排序规则】
🥇 第一优先级（必推）: RNA mod / Nanopore DRS 的方法、工具、算法、benchmark、DRS 技术（如 dorado basecalling 评测、信号模拟器、新检测软件、modification calling pipeline）。
🥈 第二优先级（必推，每天尽量保证 2-3 条）: AI 在生信/生物的应用。两类都要覆盖：(a) AI方法学（ai_bioinfo：foundation model、序列/语言模型、新架构）；(b) AI应用案例（ai_application：别人用AI工具实际解决生物学问题的范例）。至少保证有 1 条是 ai_application 类的落地应用。优先顶刊（Nature Methods/Nat Biotech/Genome Biology/NAR/Bioinformatics/Nat Commun 等）。
🥉 第三优先级（降级，仅"很好的"才推）: m6A/RNA 修饰的疾病机制论文（如 METTL3 调控某癌症）。默认不推，只有发在顶刊（Nature/Cell/Science 及其子刊、Mol Cell、NAR 等）或方法创新突出的才选 1-2 条。

排序：输出按 🥇→🥈→🥉 排列，方法向在最上面。宁缺毋滥，每天精选约 6-8 条；某优先级当天没有合格的就跳过不凑数。如果核心（🥇）当天为空，明确写一句"今日无核心新方法论文"，再用🥈补上。注意 ai_application 池子偏临床/泛医学的条目要剔除（如纯影像诊断、食品检测等离生信远的），只保留分子/组学/序列层面、对生信工作者有借鉴价值的 AI 应用。

【输出要求】
- 必须返回严格 JSON（无 markdown 围栏、无解释、不要别的内容）。
- 顶层结构: {"papers":[...],"no_core":bool,"summary_zh":"全天概述一句话(可选)"}
- papers 每项字段：
    - title: 英文原标题（直接抄候选）
    - link: 候选 JSON 的 link 字段（不可改写、不可编造）
    - doi: 候选 JSON 的 doi（没有就空字符串）
    - journal: 候选 JSON 的 journal
    - date: 候选 JSON 的 date
    - _bucket: 候选 JSON 的 _bucket（保留原值）
    - priority: 字符串 "🥇" / "🥈" / "🥉"
    - summary_cn: 一句话中文总结（说清做了什么+方法亮点，<=80 汉字）
    - relevance_cn: 一句话说明对 RNA mod/DRS/AI 方向的价值（<=60 汉字）
- no_core: 若核心(🥇)当天为空则置 true。
"""


class CurationError(RuntimeError):
    pass


def _post_json(url: str, headers: dict, payload: dict, timeout: int = 180) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json",
        **headers,
    }, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        raise CurationError(f"HTTP {e.code}: {e.read()[:500]!r}") from e
    try:
        return json.loads(raw)
    except Exception as e:
        raise CurationError(f"bad json from LLM: {raw[:300]!r}") from e


def _slim_candidate(p: dict) -> dict:
    """Shrink each candidate so prompt fits comfortably in a single call."""
    keep = ("title", "link", "doi", "journal", "date", "_bucket",
            "category", "authors", "source")
    out = {k: p.get(k, "") for k in keep if p.get(k, "") != ""}
    # bioRxiv has abstract; truncate hard to keep prompt small
    if p.get("abstract"):
        out["abstract"] = p["abstract"][:600]
    return out


def curate(
    candidates: list[dict],
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    auth_style: str = "bearer",
    extra_body: dict | None = None,
) -> dict:
    """Call the LLM to pick + summarize. Returns dict with 'papers' list."""
    api_key = api_key or os.environ.get("LLM_API_KEY") or os.environ.get("GLM_API_KEY")
    if not api_key:
        raise CurationError("missing LLM_API_KEY / GLM_API_KEY")
    base_url = (base_url or os.environ.get("LLM_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    model = model or os.environ.get("LLM_MODEL") or DEFAULT_MODEL

    slim = [_slim_candidate(p) for p in candidates]
    user_msg = (
        f"今天日期: {datetime.date.today().isoformat()}\n"
        f"候选总数: {len(slim)}\n\n"
        f"候选论文 JSON 数组（每条已精简）：\n"
        f"```json\n{json.dumps(slim, ensure_ascii=False)}\n```\n\n"
        f"请按规则精筛 6-8 条并返回严格 JSON。"
    )

    if auth_style == "x-api-key":
        headers = {"x-api-key": api_key}
    else:
        headers = {"Authorization": f"Bearer {api_key}"}

    payload: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0.3,
        "max_tokens": 4096,
        "response_format": {"type": "json_object"},
    }
    if extra_body:
        payload.update(extra_body)

    # Reasoning models (e.g. Zhipu GLM-4.x flash) spend the whole max_tokens
    # budget on hidden reasoning and return an empty `content`. Opt in to
    # disabling/enabling thinking via LLM_THINKING (e.g. "disabled"); leaving it
    # unset keeps the request portable for non-reasoning OpenAI-compatible APIs.
    thinking = os.environ.get("LLM_THINKING")
    if thinking:
        payload["thinking"] = {"type": thinking}

    url = f"{base_url}/chat/completions"
    resp = _post_json(url, headers, payload)

    try:
        content = resp["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise CurationError(f"unexpected LLM response shape: {json.dumps(resp)[:500]}") from e

    # Some providers wrap json in ```json fences even with response_format. Be lenient.
    content = content.strip()
    if content.startswith("```"):
        content = content.strip("`")
        if content.lstrip().lower().startswith("json"):
            content = content.split("\n", 1)[1] if "\n" in content else content
    try:
        parsed = json.loads(content)
    except Exception as e:
        raise CurationError(f"LLM returned non-JSON: {content[:500]!r}") from e

    papers = parsed.get("papers") or []
    # Backfill required fields if the model missed them by looking up the candidate.
    by_doi = {(p.get("doi") or "").lower(): p for p in candidates if p.get("doi")}
    by_title = {(p.get("title") or "").lower(): p for p in candidates}
    for p in papers:
        doi = (p.get("doi") or "").lower()
        title = (p.get("title") or "").lower()
        src = by_doi.get(doi) or by_title.get(title) or {}
        for field in ("link", "journal", "date", "_bucket"):
            if not p.get(field) and src.get(field):
                p[field] = src.get(field)

    return {
        "date": datetime.date.today().isoformat(),
        "papers": papers,
        "no_core": bool(parsed.get("no_core")),
        "summary_zh": parsed.get("summary_zh", ""),
        "_meta": {
            "model": model,
            "base_url": base_url,
            "candidates_in": len(slim),
            "papers_out": len(papers),
        },
    }


if __name__ == "__main__":
    # Read candidates JSON from stdin or first arg path; emit curated JSON.
    if len(sys.argv) > 1 and sys.argv[1] not in ("-",):
        with open(sys.argv[1], encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = json.load(sys.stdin)
    cands = data.get("papers", data if isinstance(data, list) else [])
    result = curate(cands)
    print(json.dumps(result, ensure_ascii=False, indent=1))

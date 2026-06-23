"""Fetch candidate papers from PubMed + bioRxiv.

Adapted from ~/.hermes/scripts/research_feed.py. Pure stdlib.
"""
from __future__ import annotations
import urllib.request
import urllib.parse
import json
import time
import sys
import datetime
from dataclasses import dataclass, asdict, field
from typing import Any


# ---------------- PubMed (PRIMARY: official journals) ----------------
def _eutils(endpoint: str, params: dict) -> dict:
    base = f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/{endpoint}"
    url = base + "?" + urllib.parse.urlencode(params)
    for attempt in range(3):
        try:
            return json.loads(urllib.request.urlopen(url, timeout=30).read())
        except Exception as e:
            if attempt == 2:
                sys.stderr.write(f"[pubmed err] {endpoint}: {e}\n")
                return {}
            time.sleep(1.5)
    return {}


def _pubmed_search(term: str, days: int, retmax: int = 40) -> list[str]:
    r = _eutils(
        "esearch.fcgi",
        {
            "db": "pubmed",
            "term": term,
            "retmax": retmax,
            "retmode": "json",
            "datetype": "pdat",
            "reldate": days,
            "sort": "date",
        },
    )
    return r.get("esearchresult", {}).get("idlist", [])


def _pubmed_summaries(ids: list[str]) -> list[dict]:
    if not ids:
        return []
    out: list[dict] = []
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        s = _eutils("esummary.fcgi", {"db": "pubmed", "id": ",".join(chunk), "retmode": "json"})
        res = s.get("result", {})
        for uid in chunk:
            doc = res.get(uid)
            if not doc:
                continue
            doi = ""
            for aid in doc.get("articleids", []):
                if aid.get("idtype") == "doi":
                    doi = aid.get("value", "")
            out.append({
                "source": "PubMed",
                "title": (doc.get("title", "") or "").strip(),
                "journal": doc.get("fulljournalname", doc.get("source", "")),
                "date": doc.get("pubdate", doc.get("epubdate", "")),
                "pmid": uid,
                "doi": doi,
                "link": f"https://pubmed.ncbi.nlm.nih.gov/{uid}/",
                "authors": ", ".join(a.get("name", "") for a in doc.get("authors", [])[:4]),
            })
        time.sleep(0.35)
    return out


PUBMED_QUERIES = {
    "nanopore_drs": '(nanopore[Title/Abstract] OR "direct RNA"[Title/Abstract] OR dorado[Title/Abstract] OR remora[Title/Abstract]) AND ("RNA modification"[Title/Abstract] OR epitranscriptom*[Title/Abstract] OR m6A[Title/Abstract] OR basecall*[Title/Abstract] OR "modification detection"[Title/Abstract])',
    "rna_mod": '("m6A"[Title/Abstract] OR pseudouridine[Title/Abstract] OR "m5C"[Title/Abstract] OR "2\'-O-methyl"[Title/Abstract] OR epitranscriptom*[Title/Abstract] OR "A-to-I"[Title/Abstract] OR "RNA editing"[Title/Abstract]) AND (sequencing[Title/Abstract] OR detection[Title/Abstract] OR method*[Title/Abstract])',
    "ai_bioinfo": '("deep learning"[Title/Abstract] OR "foundation model"[Title/Abstract] OR transformer[Title/Abstract] OR "language model"[Title/Abstract]) AND (genomics[Title/Abstract] OR transcriptom*[Title/Abstract] OR "RNA-seq"[Title/Abstract] OR nanopore[Title/Abstract] OR sequencing[Title/Abstract])',
    "ai_application": '("machine learning"[Title/Abstract] OR "deep learning"[Title/Abstract] OR "artificial intelligence"[Title/Abstract] OR AlphaFold[Title/Abstract] OR "large language model"[Title/Abstract] OR "generative model"[Title/Abstract] OR "diffusion model"[Title/Abstract]) AND ("protein design"[Title/Abstract] OR "drug discovery"[Title/Abstract] OR "variant effect"[Title/Abstract] OR "single-cell"[Title/Abstract] OR "gene expression"[Title/Abstract] OR "functional prediction"[Title/Abstract] OR "regulatory element"[Title/Abstract] OR "RNA structure"[Title/Abstract] OR transcriptom*[Title/Abstract] OR genomic*[Title/Abstract] OR "sequence model"[Title/Abstract] OR epigenom*[Title/Abstract])',
}


def collect_pubmed() -> list[dict]:
    seen: dict[str, dict] = {}
    for name, q in PUBMED_QUERIES.items():
        if name == "ai_application":
            days, retmax = 4, 25
        elif name == "ai_bioinfo":
            days, retmax = 7, 40
        else:
            days, retmax = 10, 40
        ids = _pubmed_search(q, days=days, retmax=retmax)
        for rec in _pubmed_summaries(ids):
            key = rec["doi"] or rec["title"].lower()
            rec["_bucket"] = name
            if key and key not in seen:
                seen[key] = rec
        time.sleep(0.3)
    return list(seen.values())


# ---------------- bioRxiv (SECONDARY: preprints) ----------------
CORE_KW = [
    "nanopore", "direct rna", "m6a", "m5c", "pseudouridine", "rna modification",
    "epitranscriptom", "basecall", "modification calling", "oxford nanopore",
    "dorado", "remora", "n6-methyl", "2'-o-methyl", "inosine", "rna editing",
    "a-to-i",
]
AI_KW = [
    "deep learning", "foundation model", "transformer", "neural network",
    "language model", "self-supervised",
]
AI_CATS = {
    "bioinformatics", "genomics", "molecular biology",
    "systems biology", "synthetic biology",
}


def collect_biorxiv() -> list[dict]:
    today = datetime.date.today()
    start = today - datetime.timedelta(days=3)
    base = f"https://api.biorxiv.org/details/biorxiv/{start}/{today}"
    papers: list[dict] = []
    cursor = 0
    while True:
        try:
            data = json.loads(urllib.request.urlopen(f"{base}/{cursor}", timeout=30).read())
        except Exception as e:
            sys.stderr.write(f"[biorxiv err] {e}\n")
            break
        coll = data.get("collection", [])
        if not coll:
            break
        papers.extend(coll)
        total = int(data["messages"][0].get("total", 0))
        cursor += len(coll)
        if cursor >= total or cursor > 1500:
            break
        time.sleep(0.3)

    out: list[dict] = []
    for p in papers:
        txt = (p.get("title", "") + " " + p.get("abstract", "")).lower()
        core_hits = [k for k in CORE_KW if k in txt]
        if core_hits:
            bucket = "core"
        else:
            ai_hits = [k for k in AI_KW if k in txt]
            if ai_hits and p.get("category", "") in AI_CATS:
                bucket = "ai_bioinfo"
            else:
                continue
        out.append({
            "source": "bioRxiv",
            "title": p.get("title", "").strip(),
            "journal": "bioRxiv (preprint)",
            "date": p.get("date", ""),
            "doi": p.get("doi", ""),
            "link": f"https://www.biorxiv.org/content/{p.get('doi', '')}",
            "category": p.get("category", ""),
            "authors": p.get("authors", "")[:120],
            "abstract": p.get("abstract", "")[:1500],
            "_bucket": bucket,
        })
    return out


def collect_all() -> dict:
    pubmed = collect_pubmed()
    biorxiv = collect_biorxiv()
    seen: set[str] = set()
    merged: list[dict] = []
    for rec in pubmed + biorxiv:
        key = (rec.get("doi") or "").lower() or rec["title"].lower()[:60]
        if key in seen:
            continue
        seen.add(key)
        merged.append(rec)
    return {
        "generated": datetime.datetime.now().isoformat(timespec="seconds"),
        "counts": {
            "pubmed": len(pubmed),
            "biorxiv": len(biorxiv),
            "total_after_dedup": len(merged),
        },
        "papers": merged,
    }


if __name__ == "__main__":
    print(json.dumps(collect_all(), ensure_ascii=False, indent=1))

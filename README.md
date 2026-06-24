# bio_2_info

每日生信文献的「收集 → AI 精筛 → 归档 → 推送」流水线，迁离 Hermes cron，跑在 GitHub Actions 上，免除本地关机即丢任务的痛点。

## 流程

```
PubMed + bioRxiv         LLM (Zhipu GLM 4.6 等)        IMA「每日生信资讯」KB         Telegram「研究资讯」频道
─────────────────  ───►  ─────────────────────  ───►  ─────────────────────  ───►  ───────────────────────
   feed.py                 curate.py                    archive.py                    notify.py
   候选 JSON               精筛+中文总结 JSON             下载OA PDF + 上传digest        渲染Markdown并发送
```

对应 Hermes 上原来的两个 cron job，现已合并为单个 `daily.yml` 工作流（一次 `workflow_dispatch`
触发，由 cron-job.org 打点）：feed 与 archive 在同一个 job 内串行，`selected_<today>.json`
直接经 runner 磁盘传递，无需 git 中转。

| 原 cron               | 替代命令                  | 在 daily.yml 中的步骤 |
|-----------------------|--------------------------|----------------------|
| 8:15 feed_research    | `bio-2-info run-feed`    | 第 1 步：抓取 + 精筛（不发 Telegram） |
| 9:15 research_archive | `bio-2-info run-archive` | 第 2 步：归档 IMA + 推**一条合并消息**（摘要+归档状态，continue-on-error） |

## 快速开始

```bash
git clone <repo>
cd bio_2_info
cp .env.example .env  # 然后填好 LLM_API_KEY / TELEGRAM_BOT_TOKEN
pip install -e ".[dev]"

# 加载 .env 并跑全流程（本地手动验证）
set -a; source .env; set +a
bio-2-info feed              # 1) 抓候选
bio-2-info curate            # 2) LLM 精筛
bio-2-info archive --skip-ima  # 3) 只生成 digest 不传 IMA
bio-2-info run-feed --dry-run   # 整套联调（不真的发 Telegram）
```

## 配置（.env）

| 变量 | 说明 |
|------|------|
| `LLM_API_KEY` | OpenAI-compatible LLM 的 key（默认 Zhipu GLM；也可换 DeepSeek/Kimi 等） |
| `LLM_BASE_URL` | 默认 `https://open.bigmodel.cn/api/paas/v4` |
| `LLM_MODEL` | 默认 `glm-4.6` |
| `TELEGRAM_BOT_TOKEN` | 与 ssq-checker 共用的 bot token |
| `TELEGRAM_CHAT_ID` | 默认 `-1004407117408`（「研究资讯」频道） |
| `IMA_CLIENT_ID` | IMA OpenAPI client id |
| `IMA_API_KEY` | IMA OpenAPI api key |
| `IMA_KB_NAME` | 默认 `每日生信资讯` |
| `BIO_SKIP_IMA` | 设为 `1` 跳过 IMA 上传（仅生成 digest） |

## GitHub Actions

仓库 Secrets 需要这几个：
`LLM_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `IMA_CLIENT_ID`, `IMA_API_KEY`。

workflow 在 `.github/workflows/` 下，分别跑 run-feed 和 run-archive，输出件存进 `data/digests/`（用 `actions/upload-artifact`）。

## 依赖

- **运行时**：Python ≥ 3.10，纯 stdlib（urllib/json/argparse），无 pip 依赖。
- **IMA 上传**：Node ≥ 18（用 vendored `vendor/ima/*.cjs`，无 npm 依赖）。
- **测试**：`pytest`（`pip install -e ".[dev]"`）。

## 设计要点

- **零 runtime Python deps**：跟 ssq-checker 一样，避免 CI 装包失败。
- **LLM 后端可换**：默认 Zhipu，但只要 OpenAI-compatible Chat Completions 都能用。
- **跨日去重（两本账）**：均按 DOI（无则标题）记录、commit 回仓库供后续 run 复用：
  - `archived_ledger.json`：已归档到 IMA 的论文，避免重复传 PDF。
  - `pushed_ledger.json`：已推送到 Telegram 的论文。`feed` 抓取后会先剔除已推送的再交给 LLM 精筛，
    所以同一篇不会跨天重复推送；只记录"真正精选并发出"的，当天没选中的以后仍可被选。`--dry-run` 不记录。
- **Skip-IMA 模式**：CI 失败/无凭证时仍能跑通流程，digest 留在 artifact 里手动补传。
- **vendor IMA 工具**：脱离 `~/.hermes/skills/ima-skill/`，仓库自给自足。

#!/usr/bin/env python3
"""
Quant Ideas Daily Operator - Stage A implementation

- Collects research + market signals from RSS/GitHub
- Deduplicates + scores
- Generates markdown digest + structured JSON
- Stores export drafts for Notion/Feishu
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import textwrap
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Singapore")
USER_AGENT = "Mozilla/5.0 (OpenClaw Quant Ideas Operator)"


@dataclass
class Item:
    title: str
    kind: str
    source: str
    published_at: str
    link: str
    summary: str
    line: str
    raw_score: int = 1
    score: int = 1
    topic: str = ""


RESEARCH_KEYWORDS = [
    "microstructure",
    "order book",
    "order flow",
    "alpha",
    "feature",
    "signal",
    "statistical arbitrage",
    "execution",
    "transaction cost",
    "reinforcement learning",
    "imitation learning",
    "online learning",
    "time series",
    "transformer",
    "crypto",
    "perpetual",
    "benchmark",
    "quant",
]

MARKET_KEYWORDS = [
    "liquidity",
    "inflation",
    "rate",
    "yield",
    "futures",
    "options",
    "volatility",
    "exchange",
    "listing",
    "fee",
    "macro",
    "policy",
    "fx",
    "crypto",
    "index",
    "bond",
]

RESEARCH_TOPICS = {
    "Microstructure / Order Flow": ["microstructure", "order book", "order flow", "fill", "execution", "latency"],
    "Alpha / Features / Signals": ["alpha", "factor", "feature", "signal", "label", "prediction"],
    "ML / RL / Imitation Learning": ["machine learning", "reinforcement", "imitation", "transformer", "neural", "online learning"],
    "Crypto / Market Structure": ["crypto", "perpetual", "on-chain", "exchange", "market structure"],
    "Tools / Repos / Research Infrastructure": ["github", "tool", "benchmark", "framework", "backtest", "infra", "simulator"],
}

MARKET_TOPICS = {
    "Macro / Liquidity": ["macro", "liquidity", "inflation", "rate", "yield", "fed", "ecb", "boj"],
    "Equities / Futures / FX / Crypto": ["equities", "stocks", "futures", "fx", "crypto", "bitcoin", "ethereum", "index"],
    "Exchange / Venue Updates": ["exchange", "listing", "fee", "contract", "venue", "announcement"],
    "Theme / Catalyst / Event-driven": ["theme", "catalyst", "event", "earnings", "policy", "geopolitical"],
}


def now_sg() -> datetime:
    return datetime.now(tz=TZ)


def ensure_dirs(base: Path) -> None:
    for rel in [
        "reports/daily",
        "reports/json",
        "reports/github",
        "cache",
        "logs",
        "prompts",
        "state",
        "export/notion",
        "export/feishu",
        "scripts",
    ]:
        (base / rel).mkdir(parents=True, exist_ok=True)


def read_url(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def cache_fetch(base: Path, source_name: str, url: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", source_name.lower()).strip("-")
    cache_path = base / "cache" / f"{slug}.xml"
    text = read_url(url)
    cache_path.write_text(text, encoding="utf-8")
    return text


def parse_rss_datetime(value: str | None) -> datetime:
    if not value:
        return datetime.now(tz=timezone.utc)
    value = value.strip()
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        pass
    for fmt in [
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d %H:%M:%S",
    ]:
        try:
            dt = datetime.strptime(value, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            continue
    return datetime.now(tz=timezone.utc)


def normalize_text(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def parse_rss_items(xml_text: str, source: str, kind: str, line: str) -> list[Item]:
    root = ET.fromstring(xml_text)
    channel = root.find("channel")
    if channel is None:
        # Atom fallback
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        items = []
        for entry in root.findall("atom:entry", ns):
            title = normalize_text(entry.findtext("atom:title", default="", namespaces=ns))
            link_el = entry.find("atom:link", ns)
            link = link_el.attrib.get("href", "") if link_el is not None else ""
            summary = normalize_text(entry.findtext("atom:summary", default="", namespaces=ns))
            published_raw = entry.findtext("atom:updated", default="", namespaces=ns)
            published = parse_rss_datetime(published_raw).astimezone(TZ)
            if title and link:
                items.append(
                    Item(
                        title=title,
                        kind=kind,
                        source=source,
                        published_at=published.isoformat(),
                        link=link,
                        summary=summary,
                        line=line,
                    )
                )
        return items

    out: list[Item] = []
    for item in channel.findall("item"):
        title = normalize_text(item.findtext("title", default=""))
        link = normalize_text(item.findtext("link", default=""))
        summary = normalize_text(item.findtext("description", default=""))
        published_raw = item.findtext("pubDate") or item.findtext("published") or item.findtext("dc:date")
        published = parse_rss_datetime(published_raw).astimezone(TZ)
        if title and link:
            out.append(
                Item(
                    title=title,
                    kind=kind,
                    source=source,
                    published_at=published.isoformat(),
                    link=link,
                    summary=summary,
                    line=line,
                )
            )
    return out


def fetch_github_items() -> list[Item]:
    queries = [
        "quant trading order book language:Python pushed:>=2026-03-01",
        "market microstructure simulator language:Python pushed:>=2026-03-01",
        "reinforcement learning trading language:Python pushed:>=2026-03-01",
        "crypto market making language:Python pushed:>=2026-03-01",
    ]
    items: list[Item] = []
    for q in queries:
        url = (
            "https://api.github.com/search/repositories?q="
            + urllib.parse.quote(q)
            + "&sort=updated&order=desc&per_page=6"
        )
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="ignore"))
        for repo in payload.get("items", []):
            summary = normalize_text(repo.get("description") or "")
            stars = repo.get("stargazers_count", 0)
            summary = f"{summary} (⭐ {stars})"
            published = parse_rss_datetime(repo.get("pushed_at")).astimezone(TZ)
            items.append(
                Item(
                    title=repo.get("full_name", ""),
                    kind="GitHub",
                    source="GitHub Search API",
                    published_at=published.isoformat(),
                    link=repo.get("html_url", ""),
                    summary=summary,
                    line="research",
                )
            )
    return items


def dedup(items: list[Item]) -> list[Item]:
    seen: set[str] = set()
    out: list[Item] = []
    for it in items:
        key = re.sub(r"[^a-z0-9]+", "", it.title.lower())[:120]
        if not key:
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def score_item(item: Item, now: datetime) -> int:
    text = f"{item.title} {item.summary}".lower()
    score = 1

    # Relevance scoring
    score += sum(1 for k in RESEARCH_KEYWORDS if k in text) // 3
    score += sum(1 for k in MARKET_KEYWORDS if k in text) // 4

    if item.kind == "论文":
        score += 1
    if item.kind == "GitHub":
        score += 1

    try:
        published = datetime.fromisoformat(item.published_at)
        age_hours = (now - published).total_seconds() / 3600
        if age_hours <= 48:
            score += 1
        elif age_hours <= 120:
            score += 0
        else:
            score -= 1
    except Exception:
        pass

    if any(k in text for k in ["order book", "microstructure", "execution", "alpha", "reinforcement", "liquidity", "fee"]):
        score += 1

    return max(1, min(5, score))


def pick_topic(text: str, mapping: dict[str, list[str]], default_topic: str) -> str:
    t = text.lower()
    for topic, kws in mapping.items():
        if any(k in t for k in kws):
            return topic
    return default_topic


def clean_summary(summary: str, max_len: int = 280) -> str:
    summary = normalize_text(summary)
    if not summary:
        return "原文摘要较短，建议查看原文获取完整细节。"
    if len(summary) > max_len:
        return summary[: max_len - 1] + "…"
    return summary


def to_short_date(iso_text: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_text)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return iso_text[:10]


def implication_for_market(item: Item) -> str:
    t = f"{item.title} {item.summary}".lower()
    if any(k in t for k in ["liquidity", "rate", "yield", "inflation", "macro"]):
        return "关注流动性与利率预期变化对跨资产相关性与波动率的传导。"
    if any(k in t for k in ["fee", "listing", "exchange", "contract", "venue"]):
        return "关注交易成本与可交易标的变化对成交质量和策略容量的影响。"
    if any(k in t for k in ["crypto", "bitcoin", "ethereum", "perpetual"]):
        return "关注加密现货-永续基差、资金费率与风险偏好联动。"
    return "可作为事件驱动观察点，评估对成交活跃度与风格轮动的影响。"


def inspiration(item: Item) -> dict[str, str]:
    t = f"{item.title} {item.summary}".lower()
    feature = "盘口不平衡、价量冲击与成交后短期反转因子"
    label = "5-30分钟方向标签 + 成交质量标签（滑点/冲击）"
    model = "轻量时序模型（TCN/Transformer）与在线更新线性基线"
    data = "逐笔成交、L2订单簿、交易所公告与资金费率"
    execution = "分时段动态参与率 + 冲击成本约束"
    event = "宏观流动性与交易所规则变化驱动的波动率再定价"
    watch = "价差、深度、撤单率、资金费率、跨市场相关性"

    if "reinforcement" in t or "imitation" in t:
        model = "行为克隆 + 离线RL 的执行策略组合"
    if "order book" in t or "microstructure" in t:
        feature = "多档盘口斜率、队列位置变化、被动成交概率特征"
    if "crypto" in t or "perpetual" in t:
        data = "交易所资金费率、未平仓量、链上稳定币流入"
        event = "现货/永续/期货三市场联动的微结构变化"
    if "github" in item.kind.lower() or "simulator" in t:
        execution = "先在仿真环境做冲击敏感性实验，再灰度实盘"

    return {
        "feature": feature,
        "label": label,
        "model": model,
        "data": data,
        "execution": execution,
        "event": event,
        "watch": watch,
    }


def filter_recent_items(items: list[Item], now: datetime, max_age_hours: int = 48) -> list[Item]:
    recent: list[Item] = []
    for item in items:
        try:
            published = datetime.fromisoformat(item.published_at)
        except Exception:
            recent.append(item)
            continue
        age = now - published
        if timedelta(hours=-6) <= age <= timedelta(hours=max_age_hours):
            recent.append(item)
    return recent


def build_readme_teaser(focus_items: list[Item]) -> str:
    text = " ".join(item.title for item in focus_items[:3]).lower()
    tags: list[str] = []
    if "slippage-at-risk" in text or "liquidity risk" in text:
        tags.append("SaR前瞻量化流动性风险")
    if "algoxpert" in text or "overfitting" in text:
        tags.append("反过拟合研究框架")
    if "dex" in text or "dynamic fees" in text:
        tags.append("DEX动态费率博弈")
    if "order book" in text or "microstructure" in text:
        tags.append("微结构信号新线索")
    if not tags and focus_items:
        title = focus_items[0].title
        short = title if len(title) <= 28 else title[:28] + "…"
        tags.append(short)
    return " + ".join(tags[:2]) + "：今天最值得盯的量化线索"


def update_readme(base: Path, dt_file: str, focus_items: list[Item]) -> Path:
    reports_dir = base / "reports" / "github"
    report_files = sorted(reports_dir.glob("*.md"), reverse=True)
    teaser = build_readme_teaser(focus_items)
    lines = [
        "# Quant Ideas",
        "",
        "Daily Quant Ideas Digest repo mirror. New reports are generated under `reports/github/`.",
        "",
        "## Latest Report",
        f"- [{dt_file}｜{teaser}](reports/github/{dt_file}.md)",
        "",
        "## Recent Reports",
    ]
    seen = set()
    for path in report_files[:10]:
        stem = path.stem
        if stem in seen:
            continue
        seen.add(stem)
        if stem == dt_file:
            continue
        lines.append(f"- [{stem}｜Daily Quant Ideas Digest](reports/github/{path.name})")
    if len(lines) == 7:
        lines.append("- 暂无历史报告")
    lines.append("")
    readme_path = base / "README.md"
    readme_path.write_text("\n".join(lines), encoding="utf-8")
    return readme_path


def build_markdown(
    now: datetime,
    sources_covered: str,
    focus_items: list[Item],
    research_items: list[Item],
    market_items: list[Item],
    backup_items: list[Item],
    github_status: str,
    notion_status: str,
    feishu_status: str,
    failure_reason: str,
) -> str:
    lines: list[str] = []
    lines.append("Daily Quant Ideas Digest")
    lines.append("")
    lines.append("## Metadata")
    lines.append(f"- Generated At: {now.strftime('%Y-%m-%d %H:%M')}")
    lines.append("- Focus: Quant Ideas + Markets News")
    lines.append(f"- Sources Covered: {sources_covered}")
    lines.append("")
    lines.append("## 一、今日最值得关注（最多 7 条）")

    if not focus_items:
        lines.append("- 今日暂无达到 4/5 的高质量条目，建议先看备选阅读并补充信息源。")

    for item in focus_items[:7]:
        ins = inspiration(item)
        core = clean_summary(item.summary, max_len=320)
        reason = "与量化研究主线高度相关，可直接形成可验证实验或市场观察假设。"
        if item.kind == "新闻":
            reason = "可能改变波动率、流动性或交易成本，对短中期研究假设有直接影响。"
        lines += [
            f"- 标题：{item.title}",
            f"- 类型：{item.kind}",
            f"- 来源：{item.source}",
            f"- 发布时间：{to_short_date(item.published_at)}",
            f"- 链接：{item.link}",
            f"- 评分：{item.score}/5",
            f"- 核心摘要：{core}",
            f"- 为什么值得关注：{reason}",
            "- 对我的直接启发：",
            f"  - 候选特征：{ins['feature']}",
            f"  - 候选标签：{ins['label']}",
            f"  - 候选模型：{ins['model']}",
            f"  - 候选数据源：{ins['data']}",
            f"  - 候选执行优化思路：{ins['execution']}",
            f"  - 候选事件驱动研究方向：{ins['event']}",
            f"  - 候选市场观察清单：{ins['watch']}",
            "",
        ]

    lines.append("## 二、Research Line")
    for topic in RESEARCH_TOPICS.keys():
        lines.append(f"### {topic}")
        bucket = [x for x in research_items if x.topic == topic][:6]
        if not bucket:
            lines.append("- 暂无高相关更新")
        else:
            for item in bucket:
                lines.append(f"- {item.title}｜{clean_summary(item.summary, 70)}｜{item.link}")
        lines.append("")

    lines.append("## 三、Markets Line")
    for topic in MARKET_TOPICS.keys():
        lines.append(f"### {topic}")
        bucket = [x for x in market_items if x.topic == topic][:6]
        if not bucket:
            lines.append("- 暂无高相关更新")
        else:
            for item in bucket:
                lines.append(f"- {item.title}｜{clean_summary(item.summary, 75)}｜{item.link}｜潜在交易含义：{implication_for_market(item)}")
        lines.append("")

    lines.append("## 四、备选阅读（评分 3/5）")
    if not backup_items:
        lines.append("- 暂无")
    else:
        for item in backup_items[:12]:
            lines.append(f"- {item.title}｜有参考价值但与当前主线相关性中等。｜{item.link}")
    lines.append("")

    lines.append("## 五、今日结论")
    lines.append("1. 今日最重要的 3 个新想法")
    top3 = focus_items[:3]
    if not top3:
        lines.append("- 暂无 4/5 以上信号，先补足信息源质量。")
    else:
        for idx, item in enumerate(top3, 1):
            lines.append(f"- 想法{idx}：围绕《{item.title}》提炼可验证研究假设，优先做小样本快速回测。")

    lines.append("2. 未来 7 天最值得验证的 3 个实验方向")
    lines += [
        "- 方向1：构建盘口斜率 + 成交冲击的短周期 alpha，验证在高波动时段的稳定性。",
        "- 方向2：将交易所规则/费用变化转成事件标签，检验对成交质量和容量的影响。",
        "- 方向3：在加密市场加入资金费率与未平仓量共振特征，做跨市场联动预测。",
    ]

    lines.append("3. 今日最值得持续跟踪的 3 个市场主题")
    lines += [
        "- 主题1：全球流动性与利率预期对风险资产相关性的再定价。",
        "- 主题2：交易所费用、上新与合约规则变化带来的微结构冲击。",
        "- 主题3：加密现货-永续-期货链路中的波动率与流动性迁移。",
    ]
    lines.append("")

    lines.append("## 六、运行与同步状态")
    lines.append(f"- GitHub：{github_status}")
    lines.append(f"- Notion：{notion_status}")
    lines.append(f"- 飞书：{feishu_status}")
    lines.append(f"- 失败原因与重试建议：{failure_reason}")
    lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def collect_items(base: Path) -> tuple[list[Item], list[str], list[str]]:
    feed_specs = [
        {"name": "arXiv q-fin", "url": "https://export.arxiv.org/rss/q-fin", "kind": "论文", "line": "research"},
        {"name": "arXiv stat.ML", "url": "https://export.arxiv.org/rss/stat.ML", "kind": "论文", "line": "research"},
        {"name": "arXiv cs.LG", "url": "https://export.arxiv.org/rss/cs.LG", "kind": "论文", "line": "research"},
        {"name": "WSJ Markets RSS", "url": "https://feeds.a.dj.com/rss/RSSMarketsMain.xml", "kind": "新闻", "line": "markets"},
        {"name": "Investing.com RSS", "url": "https://www.investing.com/rss/news.rss", "kind": "新闻", "line": "markets"},
        {"name": "Cointelegraph Markets", "url": "https://cointelegraph.com/rss/tag/markets", "kind": "新闻", "line": "markets"},
        {"name": "Binance Announcements", "url": "https://www.binance.com/en/support/announcement/rss", "kind": "新闻", "line": "markets"},
        {"name": "The Block RSS", "url": "https://www.theblock.co/rss.xml", "kind": "新闻", "line": "markets"},
    ]

    items: list[Item] = []
    source_names: list[str] = []
    source_errors: list[str] = []

    for spec in feed_specs:
        try:
            xml_text = cache_fetch(base, spec["name"], spec["url"])
            parsed = parse_rss_items(xml_text, spec["name"], spec["kind"], spec["line"])
            if not parsed:
                source_errors.append(f"{spec['name']}: parsed 0 items")
                continue
            items.extend(parsed)
            source_names.append(spec["name"])
        except Exception as e:
            source_errors.append(f"{spec['name']}: {e}")

    try:
        gh_items = fetch_github_items()
        if gh_items:
            items.extend(gh_items)
            source_names.append("GitHub Search API")
        else:
            source_errors.append("GitHub Search API: returned 0 items")
    except Exception as e:
        source_errors.append(f"GitHub Search API: {e}")

    return items, source_names, source_errors


def run(base: Path, run_time: datetime, github_status: str, notion_status: str, feishu_status: str, failure_reason: str) -> dict[str, Any]:
    ensure_dirs(base)

    raw_items, source_names, source_errors = collect_items(base)
    raw_items = filter_recent_items(raw_items, run_time, max_age_hours=48)
    all_items = dedup(raw_items)

    for item in all_items:
        item.score = score_item(item, run_time)
        text = f"{item.title} {item.summary}"
        if item.line == "research":
            item.topic = pick_topic(text, RESEARCH_TOPICS, "Tools / Repos / Research Infrastructure")
        else:
            item.topic = pick_topic(text, MARKET_TOPICS, "Theme / Catalyst / Event-driven")

    all_items.sort(key=lambda x: (x.score, x.published_at), reverse=True)

    focus_items = [x for x in all_items if x.score >= 4][:7]
    backup_items = [x for x in all_items if x.score == 3][:12]

    research_items = [x for x in all_items if x.line == "research" and x.score >= 3]
    market_items = [x for x in all_items if x.line == "markets" and x.score >= 3]

    dt_file = run_time.strftime("%Y-%m-%d - %H%M")
    md_path = base / "reports" / "daily" / f"{dt_file}.md"
    github_md_path = base / "reports" / "github" / f"{dt_file}.md"
    json_path = base / "reports" / "json" / f"{dt_file}.json"

    sources_covered = "arXiv / RSS / Blogs / GitHub / WeChat Articles / Markets / Tools"
    markdown = build_markdown(
        now=run_time,
        sources_covered=sources_covered,
        focus_items=focus_items,
        research_items=research_items,
        market_items=market_items,
        backup_items=backup_items,
        github_status=github_status,
        notion_status=notion_status,
        feishu_status=feishu_status,
        failure_reason=failure_reason,
    )

    payload = {
        "generated_at": run_time.isoformat(),
        "timezone": "Asia/Singapore",
        "sources": source_names,
        "source_errors": source_errors,
        "counts": {
            "raw": len(raw_items),
            "deduped": len(all_items),
            "focus": len(focus_items),
            "backup": len(backup_items),
        },
        "focus_items": [asdict(x) for x in focus_items],
        "research_items": [asdict(x) for x in research_items],
        "market_items": [asdict(x) for x in market_items],
        "backup_items": [asdict(x) for x in backup_items],
        "sync_status": {
            "github": github_status,
            "notion": notion_status,
            "feishu": feishu_status,
            "failure_reason": failure_reason,
        },
    }

    md_path.write_text(markdown, encoding="utf-8")
    github_md_path.write_text(markdown, encoding="utf-8")
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    # Exports for downstream sync
    (base / "export" / "notion" / f"{dt_file}.md").write_text(markdown, encoding="utf-8")
    (base / "export" / "feishu" / f"{dt_file}.md").write_text(markdown, encoding="utf-8")
    readme_path = update_readme(base, dt_file, focus_items)

    state = {
        "last_run_at": run_time.isoformat(),
        "last_markdown": str(md_path),
        "last_json": str(json_path),
        "fingerprint": hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
        "item_count": len(all_items),
    }
    (base / "state" / "last_run.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "markdown_path": str(md_path),
        "github_markdown_path": str(github_md_path),
        "readme_path": str(readme_path),
        "json_path": str(json_path),
        "focus_count": len(focus_items),
        "dedup_count": len(all_items),
        "raw_count": len(raw_items),
        "source_errors": source_errors,
        "dt_file": dt_file,
    }


def write_log(base: Path, dt_tag: str, content: str) -> Path:
    path = base / "logs" / f"run-{dt_tag}.log"
    path.write_text(content, encoding="utf-8")
    return path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=".", help="project root")
    parser.add_argument("--github-status", default="待更新")
    parser.add_argument("--notion-status", default="待更新")
    parser.add_argument("--feishu-status", default="待更新")
    parser.add_argument("--failure-reason", default="待更新")
    args = parser.parse_args()

    base = Path(args.base).expanduser().resolve()
    run_time = now_sg()
    dt_tag = run_time.strftime("%Y%m%d-%H%M")

    result = run(
        base=base,
        run_time=run_time,
        github_status=args.github_status,
        notion_status=args.notion_status,
        feishu_status=args.feishu_status,
        failure_reason=args.failure_reason,
    )

    log_text = textwrap.dedent(
        f"""
        [quant_ideas_pipeline]
        generated_at={run_time.isoformat()}
        base={base}
        raw_count={result['raw_count']}
        dedup_count={result['dedup_count']}
        focus_count={result['focus_count']}
        source_error_count={len(result['source_errors'])}
        markdown_path={result['markdown_path']}
        github_markdown_path={result['github_markdown_path']}
        readme_path={result['readme_path']}
        json_path={result['json_path']}
        """
    ).strip() + "\n"
    log_path = write_log(base, dt_tag, log_text)

    print(json.dumps({"ok": True, "result": result, "log_path": str(log_path)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

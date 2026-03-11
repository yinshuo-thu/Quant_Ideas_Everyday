#!/usr/bin/env python3
"""
Render-aware Notion sync for Quant Ideas digest.

Requires env:
- NOTION_TOKEN (or NOTION_API_KEY)
- NOTION_QUANT_IDEAS_PAGE_ID (page id of OpenClaw/Quant Ideas parent)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Singapore")
NOTION_VERSION = "2022-06-28"
MAX_TEXT = 1800
MAX_CHILDREN_BATCH = 100


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def notion_request(method: str, path: str, token: str, payload: dict | None = None) -> dict:
    url = f"https://api.notion.com/v1/{path.lstrip('/')}"
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Notion-Version": NOTION_VERSION,
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8", errors="ignore"))


def chunk_text(text: str, n: int = MAX_TEXT) -> list[str]:
    text = text.strip()
    if not text:
        return []
    parts = []
    while text:
        if len(text) <= n:
            parts.append(text)
            break
        cut = text.rfind(" ", 0, n)
        if cut < n // 2:
            cut = n
        parts.append(text[:cut].strip())
        text = text[cut:].strip()
    return [p for p in parts if p]


_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)|(https?://\S+)")


def rich_text_from_text(text: str, *, bold: bool = False, color: str = "default") -> list[dict]:
    text = text.strip()
    if not text:
        return []
    out: list[dict] = []
    pos = 0
    for m in _LINK_RE.finditer(text):
        if m.start() > pos:
            raw = text[pos : m.start()]
            if raw:
                out.append({"type": "text", "text": {"content": raw}})
        if m.group(1) and m.group(2):
            out.append(
                {
                    "type": "text",
                    "text": {"content": m.group(1), "link": {"url": m.group(2)}},
                }
            )
        else:
            url = m.group(3)
            out.append(
                {
                    "type": "text",
                    "text": {"content": url, "link": {"url": url}},
                }
            )
        pos = m.end()
    if pos < len(text):
        out.append({"type": "text", "text": {"content": text[pos:]}})

    normalized: list[dict] = []
    for part in out:
        content = part["text"]["content"]
        link = part["text"].get("link")
        for chunk in chunk_text(content, MAX_TEXT):
            entry = {
                "type": "text",
                "text": {"content": chunk},
                "annotations": {"bold": bold, "italic": False, "strikethrough": False, "underline": False, "code": False, "color": color},
            }
            if link:
                entry["text"]["link"] = link
            normalized.append(entry)
    return normalized


def make_text_blocks(block_type: str, text: str, *, bold: bool = False, color: str = "default") -> list[dict]:
    blocks = []
    for chunk in chunk_text(text):
        blocks.append(
            {
                "object": "block",
                "type": block_type,
                block_type: {"rich_text": rich_text_from_text(chunk, bold=bold, color=color)},
            }
        )
    return blocks


def make_spacer_block() -> dict:
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": [{"type": "text", "text": {"content": "\u00A0"}}]},
    }


def make_list_block(block_type: str, text: str) -> dict:
    chunks = chunk_text(text)
    content = chunks[0] if chunks else text.strip()
    return {
        "object": "block",
        "type": block_type,
        block_type: {"rich_text": rich_text_from_text(content), "children": []},
    }


def build_blocks(markdown_text: str) -> list[dict]:
    blocks: list[dict] = []
    paragraph_buf: list[str] = []
    list_stack: list[tuple[int, dict]] = []
    first_content_written = False

    def reset_list_stack() -> None:
        nonlocal list_stack
        list_stack = []

    def flush_paragraph() -> None:
        nonlocal paragraph_buf, blocks, first_content_written
        if not paragraph_buf:
            return
        text = " ".join(x.strip() for x in paragraph_buf if x.strip())
        if not first_content_written and text == "Daily Quant Ideas Digest":
            blocks.extend(make_text_blocks("heading_1", text))
        else:
            blocks.extend(make_text_blocks("paragraph", text))
        first_content_written = True
        paragraph_buf = []

    for raw_line in markdown_text.splitlines():
        line = raw_line.rstrip("\n")
        stripped = line.strip()

        if not stripped:
            flush_paragraph()
            reset_list_stack()
            continue

        if stripped == "<!-- SPACER -->":
            flush_paragraph()
            reset_list_stack()
            blocks.append(make_spacer_block())
            continue

        bold_only = re.match(r"^\*\*(.+)\*\*$", stripped)
        if bold_only:
            flush_paragraph()
            reset_list_stack()
            blocks.extend(make_text_blocks("paragraph", bold_only.group(1).strip(), bold=True, color="red"))
            first_content_written = True
            continue

        if stripped.startswith("### "):
            flush_paragraph()
            reset_list_stack()
            blocks.extend(make_text_blocks("heading_3", stripped[4:].strip()))
            first_content_written = True
            continue
        if stripped.startswith("## "):
            flush_paragraph()
            reset_list_stack()
            blocks.extend(make_text_blocks("heading_2", stripped[3:].strip()))
            first_content_written = True
            continue
        if stripped.startswith("# "):
            flush_paragraph()
            reset_list_stack()
            blocks.extend(make_text_blocks("heading_1", stripped[2:].strip()))
            first_content_written = True
            continue

        list_match = re.match(r"^(\s*)(-\s+|\d+\.\s+)(.+)$", line)
        if list_match:
            flush_paragraph()
            indent_spaces = len(list_match.group(1).replace("\t", "    "))
            level = indent_spaces // 2
            marker = list_match.group(2)
            content = list_match.group(3).strip()
            block_type = "numbered_list_item" if re.match(r"^\d+\.\s+$", marker) else "bulleted_list_item"
            block = make_list_block(block_type, content)

            while list_stack and list_stack[-1][0] >= level:
                list_stack.pop()

            if list_stack:
                parent = list_stack[-1][1]
                parent[parent["type"]].setdefault("children", []).append(block)
            else:
                blocks.append(block)
            list_stack.append((level, block))
            first_content_written = True
            continue

        reset_list_stack()
        paragraph_buf.append(stripped)

    flush_paragraph()
    return blocks


def batches(items: list[dict], size: int) -> Iterable[list[dict]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def create_page(token: str, parent_page_id: str, title: str) -> dict:
    payload = {
        "parent": {"page_id": parent_page_id},
        "properties": {
            "title": {
                "title": [
                    {
                        "type": "text",
                        "text": {"content": title},
                    }
                ]
            }
        },
    }
    return notion_request("POST", "pages", token, payload)


def append_children(token: str, block_id: str, children: list[dict]) -> None:
    for batch in batches(children, MAX_CHILDREN_BATCH):
        notion_request("PATCH", f"blocks/{block_id}/children", token, {"children": batch})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=".")
    parser.add_argument("--markdown", required=True)
    parser.add_argument("--title", required=True)
    args = parser.parse_args()

    base = Path(args.base).expanduser().resolve()
    load_env_file(base / "state" / "notion.env")
    token = os.getenv("NOTION_TOKEN") or os.getenv("NOTION_API_KEY")
    parent_page_id = os.getenv("NOTION_QUANT_IDEAS_PAGE_ID")

    result = {
        "ok": False,
        "timestamp": datetime.now(tz=TZ).isoformat(),
        "title": args.title,
        "reason": "",
        "page_id": None,
        "url": None,
        "block_count": 0,
    }

    if not token:
        result["reason"] = "missing NOTION_TOKEN / NOTION_API_KEY"
    elif not parent_page_id:
        result["reason"] = "missing NOTION_QUANT_IDEAS_PAGE_ID (OpenClaw/Quant Ideas parent page id)"
    else:
        md_text = Path(args.markdown).read_text(encoding="utf-8")
        blocks = build_blocks(md_text)
        result["block_count"] = len(blocks)
        try:
            page = create_page(token, parent_page_id, args.title)
            page_id = page.get("id")
            if not page_id:
                raise RuntimeError(f"page create returned no id: {page}")
            append_children(token, page_id, blocks)
            result["ok"] = True
            result["page_id"] = page_id
            result["url"] = page.get("url")
            result["reason"] = "success"
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="ignore")
            result["reason"] = f"HTTP {e.code}: {body[:500]}"
        except Exception as e:
            result["reason"] = str(e)

    log_path = base / "logs" / f"notion-sync-{datetime.now(tz=TZ).strftime('%Y%m%d-%H%M%S')}.json"
    log_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"result": result, "log_path": str(log_path)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

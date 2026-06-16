"""
discussion_universe.py — 讨论热度股票池加载与解析

当前来源：
- ApeWisdom 多个 Reddit 热门板块（过去 24 小时）
"""

from __future__ import annotations

from datetime import datetime, timezone
import html
import json
import os
import re


BASE = os.path.dirname(__file__)
DISCUSSION_UNIVERSE_PATH = os.path.join(BASE, 'discussion_universe.json')


def _strip_tags(text: str) -> str:
    clean = re.sub(r'<[^>]+>', ' ', text)
    clean = html.unescape(clean)
    return re.sub(r'\s+', ' ', clean).strip()


def normalize_code(symbol: str) -> str:
    code = str(symbol).strip().upper()
    return code if code.startswith('US.') else f'US.{code}'


def parse_apewisdom_html(page_html: str) -> list[dict]:
    tbody = re.search(r'<tbody>(.*?)</tbody>', page_html, re.S)
    if not tbody:
        return []

    rows = re.findall(r'<tr>(.*?)</tr>', tbody.group(1), re.S)
    items: list[dict] = []
    for row in rows:
        rank_matches = re.findall(r'<td class="td-right"[^>]*data-sort="([^"]*)"', row)
        metric_matches = re.findall(r'<td class="td-center rh-sm" data-sort="([^"]*)"', row)
        symbol_match = re.search(r'<span class="badge badge-company">([A-Z0-9.\-]+)</span>', row)
        company_match = re.search(r'<div class="company-name">(.*?)</div>', row, re.S)
        href_match = re.search(r'<a href="(/stocks/[^"]+/)"', row)
        if not rank_matches or len(metric_matches) < 2 or symbol_match is None:
            continue

        try:
            rank = int(float(rank_matches[0]))
        except ValueError:
            continue

        symbol = symbol_match.group(1).strip().upper()
        company = _strip_tags(company_match.group(1)) if company_match else symbol

        def _num(value: str) -> float | None:
            value = str(value).strip()
            if value == '':
                return None
            try:
                return float(value)
            except ValueError:
                return None

        mentions = _num(metric_matches[0])
        mentions_change_24h = _num(metric_matches[1])
        upvotes = _num(rank_matches[-1])

        items.append({
            'rank': rank,
            'symbol': symbol,
            'code': normalize_code(symbol),
            'company': company,
            'mentions': int(round(mentions or 0)),
            'mentions_change_24h_pct': None if mentions_change_24h is None else round(mentions_change_24h, 2),
            'upvotes': int(round(upvotes or 0)),
            'asset_url': f"https://apewisdom.io{href_match.group(1)}" if href_match else '',
        })

    return sorted(items, key=lambda item: item['rank'])


def load_discussion_universe(path: str = DISCUSSION_UNIVERSE_PATH) -> dict:
    if not os.path.exists(path):
        return {
            'generated_at': None,
            'source': {},
            'items': [],
        }
    with open(path) as f:
        payload = json.load(f)
    if not isinstance(payload, dict):
        return {'generated_at': None, 'source': {}, 'items': []}
    payload.setdefault('generated_at', None)
    payload.setdefault('source', {})
    payload.setdefault('items', [])
    return payload


def save_discussion_universe(payload: dict, path: str = DISCUSSION_UNIVERSE_PATH):
    with open(path, 'w') as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


def discussion_codes(payload: dict | None = None, limit: int | None = None) -> list[str]:
    data = payload or load_discussion_universe()
    items = list(data.get('items', []))
    if limit is not None:
        items = items[:limit]
    return [normalize_code(item['symbol']) for item in items if item.get('symbol')]


def build_watch_universe(
    base_codes: list[str],
    payload: dict | None = None,
    extra_limit: int = 0,
) -> list[str]:
    base = [normalize_code(code) for code in base_codes]
    if extra_limit <= 0:
        return list(dict.fromkeys(base))

    extras: list[str] = []
    seen = set(base)
    for code in discussion_codes(payload, limit=200):
        if code in seen:
            continue
        extras.append(code)
        seen.add(code)
        if len(extras) >= extra_limit:
            break
    return list(dict.fromkeys(base + extras))


def discussion_rank(code: str, payload: dict | None = None) -> int | None:
    """返回股票在讨论热度榜单中的排名（1-based），不在榜单则返回 None。"""
    data = payload or load_discussion_universe()
    target = normalize_code(code)
    for item in data.get('items', []):
        if normalize_code(item.get('symbol', '')) == target:
            return int(item.get('rank', 9999))
    return None


def discussion_alloc_modifier(code: str, payload: dict | None = None) -> tuple[float, str]:
    """
    根据讨论热度排名返回 alloc_mult 调整系数及说明。

    逻辑：
      rank  1-10  → 0.75：讨论极热（crowded trade），散户追高风险高，降权
      rank 11-50  → 1.15：有关注但未过热，社区动量加成，适度加权
      rank 51-200 → 1.00：在榜但不显著，不调整
      不在榜单    → 1.00：无数据，不调整
    """
    rank = discussion_rank(code, payload)
    if rank is None:
        return 1.00, ''
    if rank <= 10:
        return 0.75, f'热榜#{rank}(过热降权)'
    if rank <= 50:
        return 1.15, f'热榜#{rank}(动量加成)'
    return 1.00, f'热榜#{rank}'


def fresh_metadata() -> dict:
    return {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'source': {
            'name': 'ApeWisdom',
            'channel': 'reddit/social',
            'urls': [
                'https://apewisdom.io/wallstreetbets/',
                'https://apewisdom.io/wallstreetbets/?page=2',
                'https://apewisdom.io/stocks/',
            ],
        },
    }

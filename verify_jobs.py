"""Batch verification pipeline for Ningbo makeup/photography jobs.

The verifier only uses public search results and ordinary HTTP GET requests.
It never submits forms, bypasses CAPTCHAs, or attempts to evade login/risk
controls.  It updates verification fields in 招聘/jobs.md and appends an
auditable record to data/verification-log.json.

Usage:
    python verify_jobs.py --apply
    python verify_jobs.py --apply --max-jobs 5
    python verify_jobs.py --self-test
"""
from __future__ import annotations

import argparse
import concurrent.futures
import html
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
JOBS_MD = ROOT / "招聘" / "jobs.md"
LOG_FILE = ROOT / "data" / "verification-log.json"
COOLDOWN_DAYS = 7
MAX_WORKERS = 6
HTTP_TIMEOUT = 10
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
)

ALLOWED_STATUSES = {"verified", "located", "weak_verified", "pending"}
STALE_WORDS = ("已失效", "职位已失效", "已结束", "招聘结束", "已关闭", "已下线")
BLOCK_WORDS = ("安全验证", "滑块", "验证码", "登录后", "登录查看", "访问验证", "安全中心")

PLATFORM_RULES = (
    ("boss", "BOSS直聘", ("zhipin.com",)),
    ("58", "58同城", ("58.com",)),
    ("liepin", "猎聘", ("liepin.com",)),
    ("51job", "前程无忧", ("51job.com",)),
    ("7192", "全影人才网", ("7192.com",)),
    ("quanzhi", "全职网", ("quanzhi.com",)),
    ("gongzuo365", "工作365", ("gongzuo365.com",)),
    ("zhaopin", "智联招聘", ("zhaopin.com",)),
)


def out(value: str) -> None:
    print(value, flush=True)


def clean_text(value: str) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def norm(value: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", clean_text(value).lower())


def company_variants(value: str) -> list[str]:
    value = re.sub(r"【待核验线索】", "", value).strip()
    value = re.sub(r"（[^）]*）|\([^)]*\)", "", value).strip()
    variants = [value]
    # Legal suffixes and common location prefixes are useful for search,
    # while the full company name remains the primary matching key.
    short = re.sub(r"(有限责任公司|有限公司|公司|工作室|摄影店|摄影馆|婚纱店)$", "", value).strip()
    if short and short not in variants:
        variants.append(short)
    return [norm(x) for x in variants if norm(x)]


def role_parts(value: str) -> list[str]:
    value = re.sub(r"（[^）]*）|\([^)]*\)", "", value)
    parts = re.split(r"[/、,，+及和]|\s+", value)
    return [norm(x) for x in parts if len(norm(x)) >= 2]


def region_parts(value: str) -> list[str]:
    value = clean_text(value)
    parts = re.findall(r"鄞州|海曙|江北|镇海|北仑|奉化|慈溪|余姚|宁海|象山|宁波", value)
    return [norm(x) for x in parts]


def numbers(value: str) -> list[float]:
    text = clean_text(value).lower().replace(",", "")
    result: list[float] = []
    for raw in re.findall(r"\d+(?:\.\d+)?\s*(?:万|千|k|Ｋ)?", text):
        n = float(re.sub(r"[^0-9.]", "", raw))
        if "万" in raw:
            n *= 10000
        elif "千" in raw:
            n *= 1000
        elif "k" in raw or "Ｋ" in raw:
            n *= 1000
        result.append(n)
    return result


def salary_match(expected: str, candidate: str) -> bool:
    a, b = numbers(expected), numbers(candidate)
    if not a or not b:
        return False
    lo_a, hi_a = (min(a), max(a)) if len(a) > 1 else (a[0], a[0])
    lo_b, hi_b = (min(b), max(b)) if len(b) > 1 else (b[0], b[0])
    return max(lo_a, lo_b) <= min(hi_a, hi_b) or abs(sum(a) / len(a) - sum(b) / len(b)) <= 2500


def platform_for(url: str) -> tuple[str, str]:
    low = (url or "").lower()
    for key, label, domains in PLATFORM_RULES:
        if any(domain in low for domain in domains):
            return key, label
    return "search", "搜索引擎"


def safe_url(url: str) -> bool:
    try:
        p = urllib.parse.urlparse(url)
        return p.scheme in {"http", "https"} and bool(p.netloc) and ".gov.cn" not in p.netloc.lower()
    except Exception:
        return False


def fetch(url: str) -> tuple[int, str, str, str]:
    """Return status, final URL, text and an error classification."""
    if not safe_url(url):
        return 0, url, "", "unsafe_url"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9"})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as response:
            body = response.read(250000).decode("utf-8", errors="replace")
            final = response.geturl()
            return response.status, final, body, ""
    except Exception as exc:
        return 0, url, "", type(exc).__name__


def ddg_search(query: str, limit: int = 8) -> list[dict]:
    url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query)
    status, final, page, error = fetch(url)
    if not page:
        return []
    results: list[dict] = []
    seen: set[str] = set()
    for match in re.finditer(r'<a(?=[^>]*class="[^"]*result__a)[^>]*href="([^"]+)"[^>]*>(.*?)</a>', page, re.S):
        href = html.unescape(match.group(1))
        title = clean_text(match.group(2))
        if href.startswith("//"):
            href = "https:" + href
        parsed = urllib.parse.urlparse(href)
        params = urllib.parse.parse_qs(parsed.query)
        if "uddg" in params:
            href = params["uddg"][0]
        if not safe_url(href) or href in seen or not title:
            continue
        # A result snippet is normally nearby in the DDG HTML.
        tail = page[match.end():match.end() + 1800]
        sm = re.search(r'class="result__snippet"[^>]*>(.*?)</', tail, re.S)
        snippet = clean_text(sm.group(1)) if sm else ""
        results.append({"title": title, "url": href, "snippet": snippet, "query": query, "search_url": url})
        seen.add(href)
        if len(results) >= limit:
            break
    return results


def baidu_search(query: str, limit: int = 8) -> list[dict]:
    """Parse ordinary Baidu result HTML; no clicking/interstitial bypass."""
    url = "https://www.baidu.com/s?wd=" + urllib.parse.quote(query) + "&rn=30"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9"})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as response:
            page = response.read(400000).decode("utf-8", errors="replace")
    except Exception:
        return []
    results: list[dict] = []
    seen: set[str] = set()
    anchors = list(re.finditer(r'data-url="([^"]+)"', page))
    for index, match in enumerate(anchors):
        href = html.unescape(match.group(1))
        if not safe_url(href) or href in seen:
            continue
        parsed = urllib.parse.urlparse(href)
        if ".gov.cn" in parsed.netloc.lower() or any(x in parsed.netloc.lower() for x in ("baike", "zhidao", "map.baidu")):
            continue
        end = anchors[index + 1].start() if index + 1 < len(anchors) else min(len(page), match.end() + 6000)
        block = page[match.start():end]
        tm = re.search(r'<h3[^>]*>.*?<a[^>]*>(.*?)</a>\s*</h3>', block, re.S)
        if not tm:
            continue
        title = clean_text(tm.group(1))
        sm = (re.search(r'<span[^>]*class="[^"]*(?:content-right|co-type-copy|C-abstract)[^"]*"[^>]*>(.*?)</span>', block, re.S)
              or re.search(r'<div[^>]*class="[^"]*(?:c-abstract|c-span-len)[^"]*"[^>]*>(.*?)</div>', block, re.S))
        snippet = clean_text(sm.group(1)) if sm else ""
        results.append({"title": title, "url": href, "snippet": snippet, "query": query, "search_url": url})
        seen.add(href)
        if len(results) >= limit:
            break
    return results


def bing_search(query: str, limit: int = 8) -> list[dict]:
    url = "https://cn.bing.com/search?q=" + urllib.parse.quote(query)
    status, final, page, error = fetch(url)
    if not page:
        return []
    results: list[dict] = []
    markers = list(re.finditer(r'<li[^>]*class="[^"]*b_algo[^"]*"[^>]*>', page, re.S))
    for index, marker in enumerate(markers):
        end = markers[index + 1].start() if index + 1 < len(markers) else len(page)
        block = page[marker.start():end]
        am = re.search(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not am:
            continue
        href, title = html.unescape(am.group(1)), clean_text(am.group(2))
        sm = re.search(r'<p>(.*?)</p>', block, re.S)
        if safe_url(href):
            results.append({"title": title, "url": href, "snippet": clean_text(sm.group(1)) if sm else "", "query": query, "search_url": url})
        if len(results) >= limit:
            break
    return results


def search(query: str) -> list[dict]:
    # Keep the same broad fallback order as the existing updater.  Each
    # function only reads public result HTML and returns [] on blocking.
    for fn in (baidu_search, ddg_search, bing_search):
        results = fn(query)
        if results:
            return results
    return []


def boss_detail_candidates(result: dict) -> list[dict]:
    """Extract public /job_detail/ hrefs from a BOSS search page only.

    This is ordinary page parsing.  If BOSS returns a login/security page, no
    bypass is attempted and the original search result remains the evidence.
    """
    key, label = platform_for(result["url"])
    if key != "boss" or "/zhaopin/" not in result["url"]:
        return []
    status, final, page, error = fetch(result["url"])
    if not page or any(word in clean_text(page[:30000]) for word in BLOCK_WORDS):
        return []
    found: list[dict] = []
    seen = set()
    for match in re.finditer(r"(?:https?://(?:www\.)?zhipin\.com)?(/job_detail/[A-Za-z0-9_-]+\.html)", page):
        href = "https://www.zhipin.com" + match.group(1)
        if href in seen:
            continue
        context = clean_text(page[max(0, match.start() - 1000):match.end() + 1000])
        found.append({**result, "url": href, "title": context[:500], "snippet": context, "platform": label, "from_list": True})
        seen.add(href)
    return found[:10]


def extract_job_detail_url(result: dict) -> str:
    url = result.get("url", "")
    if "/job_detail/" in url:
        return url
    for text in (result.get("title", ""), result.get("snippet", ""), result.get("url", "")):
        m = re.search(r"https?://(?:www\.)?zhipin\.com/job_detail/[A-Za-z0-9_-]+\.html", text)
        if m:
            return m.group(0)
    return ""


def parse_entries(text: str) -> list[dict]:
    starts = list(re.finditer(r"(?m)^###\s+(\d+)\.\s+.*$", text))
    entries: list[dict] = []
    for i, match in enumerate(starts):
        end = starts[i + 1].start() if i + 1 < len(starts) else len(text)
        block = text[match.start():end]
        heading = match.group(0).strip()
        body = heading.split("\n", 1)[0]
        body = re.sub(r"^###\s+\d+\.\s+", "", body)
        parts = re.split(r"\s+[—-]\s+", body, maxsplit=1)
        company = parts[0].strip()
        position = parts[1].strip() if len(parts) > 1 else ""
        fields = {}
        for fm in re.finditer(r"(?m)^-\s*.+?\*\*(.+?)\*\*\s*[：:]\s*(.+)$", block):
            fields[fm.group(1).strip()] = fm.group(2).strip()
        links = re.findall(r"(?m)^-\s*🔗\s*\[[^]]+\]\((https?://[^)]+)\)", block)
        status = fields.get("verification_status", "").strip().lower()
        has_marker = "待核验" in block or "无直链" in block
        needs = status == "pending" or not links or has_marker
        entries.append({
            "number": int(match.group(1)), "block": block, "company": company, "position": position,
            "fields": fields, "links": links, "needs": needs,
        })
    return entries


def query_variants(entry: dict) -> list[str]:
    company = re.sub(r"【待核验线索】", "", entry["company"]).strip()
    short = re.sub(r"（[^）]*）|\([^)]*\)", "", company).strip()
    short = re.sub(r"(有限责任公司|有限公司|公司|工作室|摄影店|摄影馆|婚纱店)$", "", short).strip() or short
    position = entry["position"] or "化妆师"
    region = entry["fields"].get("区域", "宁波")
    # Keep both legal/full-name and short-name queries.  Platform-specific
    # queries are intentionally separate: a single OR query often causes a
    # search engine to return the platform home page rather than a job card.
    return [
        f'"{company}" "{position}" 宁波 招聘',
        f'"{short}" "{position}" 宁波 招聘',
        f'"{short}" 化妆师 招聘',
        f'宁波 {region} "{short}" "{position}"',
        f'site:zhipin.com/zhaopin/ "{short}" "{position}"',
        f'site:58.com "{short}" "{position}" 宁波',
        f'site:liepin.com/job/ "{short}" "{position}" 宁波',
        f'site:51job.com/ningbo "{short}" "{position}"',
        f'site:7192.com "{short}" "{position}" 宁波',
        f'site:gongzuo365.com "{short}" "{position}"',
    ]


def score_candidate(entry: dict, candidate: dict, detail: dict | None) -> dict:
    company_text = norm(entry["company"] + " " + re.sub(r"【待核验线索】", "", entry["company"]))
    company_text_variants = company_variants(entry["company"])
    role_text = norm(entry["position"])
    searchable = norm(" ".join([candidate.get("title", ""), candidate.get("snippet", ""), candidate.get("url", "")]))

    company_match = any(v and v in searchable for v in company_text_variants)
    company_score = 40 if company_match and company_text and norm(entry["company"]) in searchable else 30 if company_match else 0
    if not company_score:
        # A conservative token fallback prevents a company-only hit from
        # becoming a successful match.
        significant = [x for x in company_text_variants[-1:] if len(x) >= 3]
        company_score = 18 if significant and any(x[:4] in searchable for x in significant) else 0

    parts = role_parts(entry["position"] or "化妆师")
    role_hits = sum(1 for p in parts if p in searchable)
    title_score = 25 if role_text and role_text in searchable else 20 if parts and role_hits == len(parts) else 15 if role_hits else 0

    regions = region_parts(entry["fields"].get("区域", "宁波")) or ["宁波"]
    location_match = any(r in searchable for r in regions)
    location_score = 15 if len(regions) > 1 and location_match else 12 if location_match else 0

    salary = entry["fields"].get("薪资", "")
    salary_ok = salary_match(salary, candidate.get("title", "") + " " + candidate.get("snippet", ""))
    salary_score = 10 if salary_ok else 0

    exp_text = entry["fields"].get("学历要求", "")
    exp_tokens = re.findall(r"\d+[-到]?\d*年|学历不限|大专|本科|中专", exp_text)
    exp_match = bool(exp_tokens and any(norm(x) in searchable for x in exp_tokens))
    exp_score = 5 if exp_match else 0

    page_exists = bool(detail and detail.get("status", 0) in range(200, 400))
    body = detail.get("body", "") if detail else ""
    blocked = any(w in clean_text(body[:50000]) for w in BLOCK_WORDS)
    stale = any(w in clean_text(body[:50000]) for w in STALE_WORDS)
    current_score = 5 if page_exists and not stale else 0
    score = company_score + title_score + location_score + salary_score + exp_score + current_score
    detail_match = bool(detail and company_match and title_score >= 15 and not blocked and not stale)
    source_key, source_label = platform_for(candidate.get("url", ""))
    direct_url = extract_job_detail_url(candidate)

    if company_score >= 30 and title_score >= 15 and score >= 80:
        if detail_match:
            status = "verified"
        elif source_key == "boss" or blocked or direct_url:
            status = "located"
        else:
            status = "weak_verified"
    elif company_score >= 30 and title_score >= 15 and score >= 60:
        status = "weak_verified"
    else:
        status = "pending"

    error = ""
    if status == "pending":
        error = "未找到同时匹配公司和岗位的当前招聘记录"
    elif blocked:
        error = "详情页触发登录/安全验证，未尝试绕过；保留公开搜索证据"
    elif stale:
        error = "候选页面显示岗位可能已失效"

    return {
        "status": status, "source": source_label, "source_key": source_key,
        "verification_url": candidate.get("search_url") or candidate.get("url", ""),
        "direct_url": direct_url,
        "score": score,
        "evidence": {
            "company_match": company_score >= 30,
            "title_match": title_score >= 15,
            "location_match": location_match,
            "salary_match": salary_ok,
            "experience_education_match": exp_match,
            "current_page": page_exists and not stale,
            "score": score,
        },
        "error": error,
        "candidate": {k: candidate.get(k, "") for k in ("title", "url", "snippet", "query")},
        "detail": {"status": detail.get("status", 0) if detail else 0, "final_url": detail.get("final_url", "") if detail else "", "blocked": blocked, "stale": stale},
    }


def verify_entry(entry: dict) -> dict:
    queries = query_variants(entry)
    raw: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, len(queries))) as pool:
        futures = [pool.submit(search, q) for q in queries]
        for q, future in zip(queries, futures):
            try:
                raw.extend(future.result())
            except Exception:
                pass

    # De-duplicate result URLs, then optionally parse BOSS list pages for
    # public job_detail hrefs.  A list/search page is never treated as a
    # direct application URL.
    unique: dict[str, dict] = {}
    for item in raw:
        unique.setdefault(item.get("url", ""), item)
    boss_lists = [x for x in unique.values() if platform_for(x.get("url", ""))[0] == "boss"][:4]
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        for extracted in pool.map(boss_detail_candidates, boss_lists):
            for item in extracted:
                unique.setdefault(item["url"], item)

    scored: list[dict] = []
    for candidate in unique.values():
        if not candidate.get("url"):
            continue
        detail = None
        url = candidate.get("url", "")
        if "/job_detail/" in url or re.search(r"/(?:jobdetail|job)/", url):
            st, final, body, error = fetch(url)
            detail = {"status": st, "final_url": final, "body": body, "error": error}
        scored.append(score_candidate(entry, candidate, detail))
    scored.sort(key=lambda x: (x["score"], x["evidence"]["company_match"], x["evidence"]["title_match"]), reverse=True)

    best = scored[0] if scored else {
        "status": "pending", "source": "", "verification_url": "", "direct_url": "", "score": 0,
        "evidence": {"company_match": False, "title_match": False, "location_match": False, "salary_match": False, "experience_education_match": False, "current_page": False, "score": 0},
        "error": "搜索渠道没有返回可用候选结果", "candidate": {}, "detail": {},
    }
    # A search result alone cannot be called verified.  located is reserved
    # for a strong public listing, especially BOSS list evidence.
    best["queries"] = queries
    best["candidates_found"] = len(scored)
    best["all_candidates"] = scored[:10]
    return best


def due(entry: dict, today: date) -> bool:
    # Pending/no-direct jobs are deliberately retried on every run.  The
    # seven-day expiry applies to already located/weak/verified records.
    if entry["needs"] and (not entry["links"] or entry["fields"].get("verification_status", "").lower() == "pending"):
        return True
    value = entry["fields"].get("verified_at", "")
    try:
        verified = date.fromisoformat(value[:10])
    except Exception:
        return True
    return today - verified >= timedelta(days=COOLDOWN_DAYS)


VERIFICATION_KEYS = {
    "verification_status", "verification_source", "verification_url", "verification_score",
    "verified_at", "verification_evidence", "verification_error", "direct_url",
}


def upsert_verification_fields(block: str, result: dict, now: str) -> str:
    lines = block.splitlines()
    lines = [line for line in lines if not any(f"**{key}**" in line for key in VERIFICATION_KEYS)]
    fields = [
        f'- 🔎 **verification_status**：{result["status"]}',
        f'- 🧭 **verification_source**：{result.get("source", "") or "未找到"}',
        f'- 🌐 **verification_url**：{result.get("verification_url", "") or ""}',
        f'- 📊 **verification_score**：{result.get("score", 0)}',
        f'- 📅 **verified_at**：{now}',
        f'- 🧾 **verification_evidence**：{json.dumps(result.get("evidence", {}), ensure_ascii=False, separators=(",", ":"))}',
        f'- ⚠️ **verification_error**：{result.get("error", "") or ""}',
    ]
    if result.get("direct_url"):
        fields.insert(3, f'- 🔗 **direct_url**：{result["direct_url"]}')
    # Keep fields at the end of the card so existing human-readable fields
    # and links remain untouched.
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines + fields) + "\n\n"


def append_log(records: list[dict]) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    old: list[dict] = []
    if LOG_FILE.exists():
        try:
            value = json.loads(LOG_FILE.read_text(encoding="utf-8"))
            old = value if isinstance(value, list) else []
        except Exception:
            old = []
    LOG_FILE.write_text(json.dumps(old + records, ensure_ascii=False, indent=2), encoding="utf-8")


def apply_results(text: str, entries: list[dict], results: dict[int, dict], now: str) -> str:
    output: list[str] = []
    cursor = 0
    for entry in entries:
        start = text.find(entry["block"], cursor)
        if start < 0:
            continue
        output.append(text[cursor:start])
        if entry["number"] in results:
            output.append(upsert_verification_fields(entry["block"], results[entry["number"]], now))
        else:
            output.append(entry["block"])
        cursor = start + len(entry["block"])
    output.append(text[cursor:])
    return "".join(output)


def self_test() -> int:
    sample = """### 1. 【待核验线索】宁波示例摄影 — 化妆师\n- 📍 **区域**：鄞州区\n- 💰 **薪资**：月薪8000-12000\n"""
    entries = parse_entries(sample)
    assert len(entries) == 1 and entries[0]["needs"]
    assert len(query_variants(entries[0])) >= 5
    candidate = {"title": "宁波示例摄影 化妆师 8000-12000", "snippet": "鄞州区 3-5年", "url": "https://www.zhipin.com/zhaopin/demo", "search_url": "https://www.zhipin.com/zhaopin/demo"}
    scored = score_candidate(entries[0], candidate, {"status": 200, "final_url": candidate["url"], "body": "宁波示例摄影 化妆师 鄞州区 8000-12000", "error": ""})
    assert scored["evidence"]["company_match"] and scored["evidence"]["title_match"]
    assert scored["score"] >= 60
    assert scored["score"] <= 100
    boss = {"url": "https://www.zhipin.com/job_detail/abc123.html", "title": "", "snippet": ""}
    assert extract_job_detail_url(boss).endswith("/job_detail/abc123.html")
    out("SELF_TEST_OK")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="write statuses and verification log")
    parser.add_argument("--max-jobs", type=int, default=0)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    if not JOBS_MD.exists():
        out("VERIFY_FAIL: 招聘/jobs.md 不存在")
        return 1
    text = JOBS_MD.read_text(encoding="utf-8")
    entries = parse_entries(text)
    today = date.today()
    targets = [x for x in entries if x["needs"] and due(x, today)]
    if args.max_jobs:
        targets = targets[:args.max_jobs]
    out(f"VERIFY_TARGETS: {len(targets)} / {len(entries)}")
    now = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
    results: dict[int, dict] = {}
    logs: list[dict] = []
    counts = {"verified": 0, "located": 0, "weak_verified": 0, "pending": 0}
    for index, entry in enumerate(targets, 1):
        out(f"[{index}/{len(targets)}] #{entry['number']} {entry['company']} — {entry['position']}")
        result = verify_entry(entry)
        results[entry["number"]] = result
        counts[result["status"]] = counts.get(result["status"], 0) + 1
        logs.append({
            "job_id": entry["number"], "company": entry["company"], "position": entry["position"],
            "searched_at": now, "search_queries": result.get("queries", []),
            "candidates": result.get("all_candidates", []), "match_score": result.get("score", 0),
            "final_status": result["status"], "verification_source": result.get("source", ""),
            "verification_url": result.get("verification_url", ""), "direct_url": result.get("direct_url", ""),
            "evidence": result.get("evidence", {}), "error": result.get("error", ""),
        })
        out(f"    -> {result['status']} score={result.get('score', 0)} candidates={result.get('candidates_found', 0)}")
    if args.apply and results:
        JOBS_MD.write_text(apply_results(text, entries, results, now), encoding="utf-8")
        append_log(logs)
        out(f"WROTE: {JOBS_MD}")
        out(f"WROTE: {LOG_FILE}")
    elif not args.apply:
        out("DRY_RUN: 未写入岗位和日志，使用 --apply 才会保存")
    out("VERIFY_SUMMARY: " + json.dumps(counts, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Pre-deploy quality gate for the Ningbo makeup jobs site."""
from pathlib import Path
import re
import sys

ROOT = Path(__file__).parent
JOBS = ROOT / "招聘" / "jobs.md"
INDEX = ROOT / "site" / "index.html"

FORBIDDEN = (
    "heiguang.com", "baidu.com/link", "weixin.sogou.com/link",
    "zhaopin.com/zhaopin/", "msearch.51job.com/jobs/",
    "www.58.com/pp", "b.dianzhangzhipin.com/store",
    "b.dianzhangzhipin.com/storer", "https://zhipin.com/", "https://www.zhipin.com/",
)
ALLOWED = re.compile(
    r"https?://(?:m\.58\.com/(?:nb|yuyao)/[^\s)]+x\.shtml|"
    r"(?:www\.)?58\.com/[^\s)]+x\.shtml|"
    r"(?:www\.)?quanzhi\.com/job/[^\s)]+|"
    r"(?:www\.)?zhaopin\.com/jobdetail/[^\s)]+|"
    r"(?:www\.)?liepin\.com/job/[^\s)]+|"
    r"jobs\.51job\.com/[^\s)]+|"
    r"(?:b\.)?dianzhangzhipin\.com/job/[^\s)]+|"
    r"(?:m\.)?7192\.com/[^\s)]+/ningbo/\d+\.html|"
    r"hr\.7192\.com/[^\s)]+/ningbo/\d+\.html|"
    r"job\.nhzj\.com/job/\d+|(?:www\.)?xsmpw\.com/job/\d+\.html|"
    r"(?:www\.)?nbjdrc\.com/job[^\s)]*|"
    r"(?:www\.)?nbmanp\.com/[^\s)]+|"
    r"(?:(?:www|m)\.)?gongzuo365\.com/zhaopin/[^\s)]+|"
    r"q\.yingjiesheng\.com/jobdetail/[^\s)]+|"
    r"(?:www\.)?zjlsrc\.com/job\d+\.shtml|"
    r"(?:www\.)?yupao\.com/[^\s)]+|"
    r"yuyao\.58supin\.com/[^\s)]+|nb\.58supin\.com/[^\s)]+|"
    r"(?:www\.)?zhidianmijin\.com\.cn/jobs/[^\s)]+)"
)


def fail(msg: str) -> None:
    print(f"QUALITY_GATE_FAIL: {msg}")
    sys.exit(1)


def main() -> None:
    if not JOBS.exists():
        fail("招聘/jobs.md 不存在")
    text = JOBS.read_text(encoding="utf-8")
    entries = re.findall(r"^###\s+\d+\.", text, re.M)
    if len(entries) < 30:
        fail(f"岗位只有 {len(entries)} 条，疑似截断")
    stated = re.search(r"本期收录\s*\*\*(\d+)", text)
    if stated and int(stated.group(1)) != len(entries):
        fail(f"头部声明 {stated.group(1)} 条，实际 {len(entries)} 条")

    # Only Markdown 🔗 links are application/direct links.  Verification
    # evidence fields may legitimately point to a BOSS search/detail page and
    # must not silently become application links.
    urls = re.findall(r"(?m)^-\s*🔗\s*\[[^]]+\]\((https?://[^)\s]+)\)", text)
    evidence_urls = re.findall(r"(?m)^-\s*(?:🌐\s*)?(?:🔗\s*)?\*\*(?:verification_url|direct_url)\*\*\s*[：:]\s*(https?://\S+)", text)
    seen = set()
    for url in urls:
        if any(bad in url.lower() for bad in FORBIDDEN):
            fail(f"发现禁止链接: {url}")
        if url in seen:
            fail(f"发现重复链接: {url}")
        seen.add(url)
        if not ALLOWED.fullmatch(url.rstrip(".,")):
            fail(f"链接不符合岗位直链规则: {url}")
    for url in evidence_urls:
        if ".gov.cn" in url.lower() or "heiguang.com" in url.lower():
            fail(f"核验字段发现禁止链接: {url}")

    statuses = {"verified", "located", "weak_verified", "pending"}
    status_lines = re.findall(r"(?m)^-\s*🔎\s*\*\*verification_status\*\*\s*[：:]\s*(\S+)", text)
    for value in status_lines:
        if value not in statuses:
            fail(f"未知核验状态: {value}")
    score_lines = re.findall(r"(?m)^-\s*📊\s*\*\*verification_score\*\*\s*[：:]\s*(\d+)", text)
    if any(int(x) > 100 for x in score_lines):
        fail("核验分数超过100")
    state_lines = [x.strip() for x in re.findall(
        r"(?m)^-\s*🧩\s*\*\*verification_state\*\*\s*[：:]([^\r\n]*)", text
    )]
    if any(x not in {"", "active", "stale", "possibly_closed"} for x in state_lines):
        fail("未知核验状态标记")

    log = ROOT / "data" / "verification-log.json"
    if log.exists():
        try:
            value = __import__("json").loads(log.read_text(encoding="utf-8"))
            if not isinstance(value, list):
                fail("verification-log.json 必须是数组")
        except Exception as exc:
            fail(f"verification-log.json 无法解析: {exc}")

    if not INDEX.exists():
        fail("site/index.html 不存在")
    html = INDEX.read_text(encoding="utf-8")
    cards = len(re.findall(r'class=["\'][^"\']*job-item', html))
    if cards != len(entries):
        fail(f"页面岗位卡片 {cards} 条，与数据 {len(entries)} 条不一致")
    print(f"QUALITY_GATE_OK: {len(entries)} jobs, {len(urls)} direct links, {cards} cards")


if __name__ == "__main__":
    main()

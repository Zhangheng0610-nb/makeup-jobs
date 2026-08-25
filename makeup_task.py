"""
Makeup Jobs Updater — 宁波化妆师招聘聚合站自动更新
Scheduled via Windows Task Scheduler (odd days, before 9:00 valley price).
Usage: python makeup_task.py makeup     # 主任务（奇数日门禁 + 互斥锁）
       python makeup_task.py seed       # 种子搜索：跑 SEED_QUERIES 写 seed_results.json（无模型）
       python makeup_task.py seedsearch "查询词" [max_results]
"""
import json, os, sys, subprocess, ctypes, re, html as html_mod, urllib.parse, urllib.request, http.cookiejar, time
from datetime import date
from pathlib import Path

import anthropic

# ── Stdout encoding ─────────────────────────────────────────────
# Windows 重定向 stdout 时默认 GBK，⚠️/emoji 会抛 UnicodeEncodeError 直接崩
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

# ── Config ────────────────────────────────────────────────────
# Cloud (GitHub Actions) provides env vars; local Windows falls back to settings.json
_api_key = os.environ.get('DEEPSEEK_API_KEY') or os.environ.get('ANTHROPIC_AUTH_TOKEN', '')
_base_url = os.environ.get('ANTHROPIC_BASE_URL', '')
_model = os.environ.get('ANTHROPIC_MODEL', '')
if not _api_key or not _base_url:
    SETTINGS_PATH = os.path.join(os.path.expanduser('~'), '.claude', 'settings.json')
    with open(SETTINGS_PATH) as f:
        settings = json.load(f)
    env = settings.get('env', {})
    _api_key = _api_key or env.get('ANTHROPIC_AUTH_TOKEN', '')
    _base_url = _base_url or env.get('ANTHROPIC_BASE_URL', '')
    _model = _model or env.get('ANTHROPIC_MODEL', 'deepseek-v4-flash')

API_KEY = _api_key
BASE_URL = _base_url
MODEL = _model
EFFORT = 'xhigh'

BASE_DIR = Path(os.environ.get('MAKEUP_BASE_DIR', r'C:\Users\张衡\Desktop\宁波化妆师招聘'))

# ── 手动更新冷却（防连点重复消耗）──────────────────────────────
COOLDOWN_MINUTES = 30  # 距上次更新完成不足30分钟的再次触发直接跳过
COOLDOWN_FILE = BASE_DIR / '.makeup_cooldown'

def cooldown_remaining() -> int | None:
    """距上次完成还差几分钟？在冷却窗口内返回剩余分钟，否则 None。"""
    try:
        last = float(COOLDOWN_FILE.read_text(encoding='utf-8').strip())
    except (OSError, ValueError):
        return None
    remain = COOLDOWN_MINUTES - (time.time() - last) / 60
    return max(1, int(remain)) if remain > 0 else None
JOBS_MD = BASE_DIR / '招聘' / 'jobs.md'
SITE_DIR = BASE_DIR / 'site'

TODAY = date.today()
TODAY_STR = TODAY.strftime('%Y-%m-%d')
WEEKDAYS_CN = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
TODAY_WD = WEEKDAYS_CN[TODAY.weekday()]

client = anthropic.Anthropic(api_key=API_KEY, base_url=BASE_URL)

# ── Single-instance lock ───────────────────────────────────────
_mutex_handles = {}

def acquire_lock(mode: str) -> bool:
    """Try to take the mutex for this mode. Returns True if acquired."""
    if sys.platform.startswith('linux'):
        return True  # GitHub Actions: single-job concurrency guard handles parallelism
    try:
        name = 'Local\\MakeupJobsAutoTask_' + mode
        handle = ctypes.windll.kernel32.CreateMutexW(None, False, name)
        if not handle:
            print(f"[LOCK] CreateMutex failed — proceeding without lock")
            return True
        _mutex_handles[mode] = handle
        if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            return False  # another instance already holds the mutex
        return True
    except Exception as e:
        print(f"[LOCK] error: {e} — proceeding without lock")
        return True

def release_lock(mode: str):
    """Release the mutex handle for this mode."""
    handle = _mutex_handles.pop(mode, None)
    if handle:
        try:
            ctypes.windll.kernel32.CloseHandle(handle)
        except Exception:
            pass

# ── Tools ─────────────────────────────────────────────────────

# 低价值噪声域名（照抄文博版已验证清单）
_NOISE_NL = (
    'zdic.net', 'hanyuguoxue.com', 'hgcha.com', 'chagushici.com', 'iciba.com',
    'dict.cn', 'dictionary.cambridge.org', 'wiki.mbalib.com', 'mbalib.com',
    'baike.sogou.com', 'baike.baidu.com', 'zhidao.baidu.com', 'xueshushe.cn',
    'dili360.com', 'maigoo.com', 'mafengwo.cn', 'samsung.com.cn', 'samsung.cn',
    'account.samsung.cn', 'britishairways.com', 'viking.com', 'egyptair.com',
    'italia.it', '9game.cn', 'csls.cdb.com.cn', 'hanyu.baidu.com',
)
# 化妆培训学校/加盟广告会严重污染招聘结果，标题含这些词一律丢弃
_NOISE_TITLE = ('拼音', '词典', '字典', '怎么读', '翻译', '部首', '笔顺', '组词', '造句', '音标', '释义',
                '培训', '速成', '招生', '加盟', '学员', '学费')


def _is_noise(item) -> bool:
    t = item.get('title', '') or ''
    u = item.get('url', '') or ''
    if any(k in t for k in _NOISE_TITLE):
        return True
    try:
        nl = urllib.parse.urlparse(u).netloc.lower()
    except Exception:
        nl = ''
    if any(b in nl for b in _NOISE_NL):
        return True
    if 'zhihu.com/topic' in u or 'zhihu.com/question' in u:
        return True
    return False


def web_search(query: str, max_results: int = 8) -> str:
    """Baidu first, then Sogou-WeChat (catches 公众号 recruitment posts), bing last.
    GitHub Actions (Linux, foreign IP): baidu/sogou block datacenter IPs; DuckDuckGo HTML
    works reliably from abroad, bing as fallback."""
    if sys.platform.startswith('linux'):
        ddg = _ddg_search(query, max_results)
        if ddg:
            return json.dumps(ddg, ensure_ascii=False, indent=2)
        return _bing_search(query, max_results)
    baidu = _baidu_search(query, max_results)
    sogou = _sogou_search(query, max_results)
    merged = [r for r in baidu if not _is_noise(r)]
    titles = {r['title'] for r in merged}
    for r in sogou:
        if r['title'] not in titles and not _is_noise(r):
            merged.append(r)
            titles.add(r['title'])
    if merged:
        return json.dumps(merged[:max_results + 4], ensure_ascii=False, indent=2)
    return _bing_search(query, max_results)

def _baidu_search(query: str, max_results: int) -> list:
    """Search Baidu. Prefer data-url (real link); skip encyclopedia cards (nourl)."""
    try:
        url = 'https://www.baidu.com/s?wd=' + urllib.parse.quote(query) + '&rn=' + str(min(max_results * 3, 30))
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36',
            'Accept-Language': 'zh-CN,zh;q=0.9',
        })
        page = urllib.request.urlopen(req, timeout=20).read().decode('utf-8', errors='replace')
        results = []
        seen = set()
        anchors = list(re.finditer(r'data-url="([^"]+)"', page))
        for i, du in enumerate(anchors):
            raw = html_mod.unescape(du.group(1))
            if 'nourl' in raw or raw in seen:
                continue
            try:
                nl = urllib.parse.urlparse(raw).netloc.lower()
            except Exception:
                nl = ''
            # 过滤政府域名(审查风险) + 百科/地图/问答噪声
            if '.gov.cn' in nl or any(b in nl for b in ('baike.baidu', 'baike.sogou', 'map.baidu', 'zhidao.baidu')):
                continue
            seg_end = anchors[i + 1].start() if i + 1 < len(anchors) else min(du.start() + 6000, len(page))
            seg = page[du.start():seg_end]
            hm = re.search(r'<h3[^>]*>.*?<a[^>]*>(.*?)</a>\s*</h3>', seg, re.S)
            if not hm:
                continue
            title = html_mod.unescape(re.sub(r'<[^>]+>', '', hm.group(1))).strip()
            if not title:
                continue
            sm = (re.search(r'<span[^>]*class="[^"]*(?:content-right|co-type-copy|C-abstract)[^"]*"[^>]*>(.*?)</span>', seg, re.S)
                  or re.search(r'<div[^>]*class="[^"]*(?:c-abstract|c-span-len)[^"]*"[^>]*>(.*?)</div>', seg, re.S))
            snippet = html_mod.unescape(re.sub(r'<[^>]+>', '', sm.group(1))).strip() if sm else ''
            results.append({'title': title, 'url': raw, 'snippet': snippet[:500]})
            seen.add(raw)
            if len(results) >= max_results:
                break
        # 数据不足时用 h3 跳转链接兜底
        if len(results) < max_results:
            for tm in re.finditer(r'<h3[^>]*>.*?<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', page, re.S):
                u = tm.group(1)
                if not u.startswith('http') or u in seen:
                    continue
                try:
                    ul = urllib.parse.urlparse(u).netloc.lower()
                except Exception:
                    ul = ''
                if '.gov.cn' in ul:
                    continue
                seen.add(u)
                t = html_mod.unescape(re.sub(r'<[^>]+>', '', tm.group(2))).strip()
                results.append({'title': t, 'url': u, 'snippet': ''})
                if len(results) >= max_results:
                    break
        return results
    except Exception:
        return []

def _ddg_search(query: str, max_results: int) -> list:
    """DuckDuckGo HTML search — reliable from datacenter IPs (GitHub Actions)."""
    try:
        url = 'https://html.duckduckgo.com/html/?q=' + urllib.parse.quote(query)
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36',
        })
        page = urllib.request.urlopen(req, timeout=20).read().decode('utf-8', errors='replace')
        results = []
        seen = set()
        for m in re.finditer(r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', page, re.S):
            href = html_mod.unescape(m.group(1))
            title = html_mod.unescape(re.sub(r'<[^>]+>', '', m.group(2))).strip()
            if not title or href in seen:
                continue
            # DDG redirect param carries the real URL
            if 'uddg=' in href:
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
                real = qs.get('uddg', [href])[0]
                href = real
            try:
                nl = urllib.parse.urlparse(href).netloc.lower()
            except Exception:
                nl = ''
            if '.gov.cn' in nl or any(b in nl for b in ('baike.baidu', 'baike.sogou', 'zhidao.baidu', 'map.baidu')):
                continue
            seen.add(href)
            # snippet from following result__snippet block
            sm = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', page[m.end():m.end() + 4000], re.S)
            snippet = html_mod.unescape(re.sub(r'<[^>]+>', '', sm.group(1))).strip() if sm else ''
            results.append({'title': title, 'url': href, 'snippet': snippet[:500]})
            if len(results) >= max_results:
                break
        return results
    except Exception:
        return []

def _bing_search(query: str, max_results: int) -> str:
    """Bing fallback (original scraper)."""
    try:
        url = 'https://www.bing.com/search?q=' + urllib.parse.quote(query) + '&count=' + str(max_results)
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        })
        page = urllib.request.urlopen(req, timeout=20).read().decode('utf-8', errors='replace')
        results = []
        for m in re.finditer(r'<li class="b_algo[^"]*".*?</li>', page, re.S):
            block = m.group(0)
            tm = re.search(r'<h2[^>]*>.*?<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
            if not tm:
                continue
            result_url = tm.group(1)
            netloc = ''
            try:
                netloc = urllib.parse.urlparse(result_url).netloc.lower()
            except Exception:
                pass
            if any(b in netloc for b in ('baike.baidu', 'baike.sogou', 'map.baidu', 'zhidao.baidu')) or '.gov.cn' in netloc:
                continue
            title = html_mod.unescape(re.sub(r'<[^>]+>', '', tm.group(2))).strip()
            sm = re.search(r'<p[^>]*>(.*?)</p>', block, re.S)
            snippet = html_mod.unescape(re.sub(r'<[^>]+>', '', sm.group(1))).strip() if sm else ''
            item = {'title': title, 'url': result_url, 'snippet': snippet[:500]}
            if _is_noise(item):
                continue
            results.append(item)
            if len(results) >= max_results:
                break
        return json.dumps(results, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({'error': str(e)})

_sogou_opener = None

def _sogou_search(query: str, max_results: int = 8) -> list:
    """Sogou WeChat article search. Catches 公众号 recruitment roundups.
    Body sits behind a captcha, so only title/snippet are usable."""
    global _sogou_opener
    try:
        if _sogou_opener is None:
            cj = http.cookiejar.CookieJar()
            _sogou_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
            _sogou_opener.addheaders = [
                ('User-Agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36'),
                ('Accept-Language', 'zh-CN,zh;q=0.9'),
                ('Referer', 'https://weixin.sogou.com/'),
            ]
            _sogou_opener.open('https://weixin.sogou.com/', timeout=15)
        url = 'https://weixin.sogou.com/weixin?type=2&query=' + urllib.parse.quote(query)
        page = _sogou_opener.open(url, timeout=20).read().decode('utf-8', errors='replace')
        if '请输入验证码' in page:
            return []
        results = []
        for m in re.finditer(r'<div class="txt-box">(.*?)(?=<div class="txt-box">|<div class="s-p"|<div class="result")', page, re.S):
            blk = m.group(1)
            tm = re.search(r'<h3>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', blk, re.S)
            if not tm:
                continue
            href = tm.group(1)
            title = html_mod.unescape(re.sub(r'<[^>]+>', '', tm.group(2))).strip()
            if not title:
                continue
            txt = re.sub(r'<script.*?</script>', '', blk, flags=re.S)
            txt = re.sub(r'<[^>]+>', '', txt)
            txt = re.sub(r'\s+', ' ', txt).strip()
            url_out = 'https://weixin.sogou.com' + href if href.startswith('/link?') else href
            results.append({'title': title, 'url': url_out, 'snippet': txt[:280],
                            'source': 'sogou_wx'})
            if len(results) >= max_results:
                break
        return results
    except Exception:
        return []

def write_file(path: str, content: str) -> str:
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding='utf-8')
        return f"OK: wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"ERROR: {e}"

def run_bash(command: str) -> str:
    try:
        result = subprocess.run(command, shell=True, capture_output=True, text=True,
            encoding='utf-8', errors='replace',  # SSH/git 输出含 GBK 字节（中文路径）时不再抛 UnicodeDecodeError
            timeout=180, cwd=str(SITE_DIR))
        out = (result.stdout or '').strip() or (result.stderr or '').strip()
        return out[:3000] if len(out) > 3000 else out
    except subprocess.TimeoutExpired:
        return "ERROR: command timed out"
    except Exception as e:
        return f"ERROR: {e}"

def read_file(path: str) -> str:
    try:
        return Path(path).read_text(encoding='utf-8')[:40000]
    except Exception as e:
        return f"ERROR: {e}"

def list_dir(path: str) -> str:
    try:
        files = sorted(Path(path).glob('*.md'), reverse=True)
        return json.dumps([f.name for f in files], ensure_ascii=False)
    except Exception as e:
        return f"ERROR: {e}"

TOOLS = [
    {"name": "web_search", "description": "搜索网页获取实时信息",
     "input_schema": {"type": "object", "properties": {
         "query": {"type": "string"}, "max_results": {"type": "integer"}}, "required": ["query"]}},
    {"name": "write_file", "description": "写入文件",
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "run_bash", "description": "执行shell命令",
     "input_schema": {"type": "object", "properties": {
         "command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "读取文件内容",
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string"}}, "required": ["path"]}},
    {"name": "list_dir", "description": "列出目录下的md文件",
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string"}}, "required": ["path"]}},
]

TOOL_MAP = {
    "web_search": web_search, "write_file": write_file,
    "run_bash": run_bash, "read_file": read_file, "list_dir": list_dir,
}

# ── Prompts ────────────────────────────────────────────────────

def get_prompt(mode):
    if mode == 'makeup':
        cur_month = TODAY.strftime('%Y年%m月')
        return f"""你是宁波化妆师招聘信息AI编辑。今天是{TODAY_STR}（{TODAY_WD}）。

## 任务：采集宁波本地化妆/摄影岗位，更新 {JOBS_MD}

### 执行步骤
1. 先用 read_file 读取原 {JOBS_MD}，记住已收录的机构/岗位，避免重复；已收录且仍有效的岗位保留并刷新，本次新搜到的岗位加 🆕。
2. **多渠道并行搜索**（常规渠道 + 公众号汇总，缺一不可）：
   【常规渠道·搜索词清单】（每个词都用 web_search，可加"2026"年份词）
   - 宁波 化妆师 招聘
   - 宁波 影楼 招聘 化妆师
   - 宁波 婚纱摄影 化妆师 招聘
   - 宁波 儿童摄影 化妆师 招聘
   - 宁波 婚礼跟妆 招聘
   - 宁波 高端影楼 化妆师 招聘
   - 宁波 化妆助理 招聘
   - 宁波 造型师 招聘
   - 宁波 彩妆师 招聘
   - 宁波 摄影师 招聘 / 宁波 摄影助理 招聘
   - 具体机构名+招聘（如 金夫人 宁波 招聘、韩国艺匠 宁波 化妆师、龙摄影 宁波 招聘、大象映画 宁波 化妆师）
   【公众号汇总·补充线索】web_search 返回中 **source=sogou_wx 的条目就是公众号文章**，化妆/影楼招聘类公众号会发招聘汇总帖，标题形如「化妆师招聘信息汇总」「影楼招聘」——**只作线索**：从标题/摘要提取机构名后用「机构名+招聘」验证、拿到真实链接才能收录；公众号正文网页端读不到（撞验证码），**不要用 run_bash 去 curl 公众号文章**
   - 化妆师 招聘 汇总
   - 影楼 招聘 汇总
   - 婚礼化妆师 招聘 公众号
   【禁用词】培训/招生/加盟/速成/网红 词根已验证质量差，禁止使用
3. **过滤规则（硬性）**：
   - 只收宁波本地岗位（宁波各区县均可；余姚/慈溪/宁海/象山标注为"宁波周边"）；宁波以外一律不收
   - 岗位范围：化妆师/彩妆师/造型师/化妆助理 为主，摄影师/摄影助理 附带（求职者可兼）；其余岗位不收
   - URL 含 gov.cn 的结果一律跳过，不读取不收录
   - 不爬站：禁止 run_bash curl 58/BOSS直聘/智联/前程无忧 或调用其 API，只用 web_search 结果
   - 化妆培训学校"招学员/招加盟/招学员模特"类广告一律不收录；只收真招聘
   - 某词返回大量培训/百科/旅游噪声 → 立即换词，不在无效结果上反复搜
   - **链接质量（硬性）**：🔗 必须直指具体岗位帖。合格：58岗位帖（m.58.com/nb/meirongjianshen/或zpwentiyingshi等分类/...x.shtml）、全职招聘网岗位（quanzhi.com/job/...）、智联 jobdetail（zhaopin.com/jobdetail/...）、前程无忧岗位直达（jobs.51job.com/...）、店长直聘岗位帖（dianzhangzhipin.com/job/...）、全影人才网岗位页（hr.7192.com/分类/ningbo/数字.html 或 m.7192.com/分类/ningbo/数字.html，带数字ID）、行业站岗位页（job.nhzj.com/job/、xsmpw.com/job/、nbjdrc.com/job...）。**禁止**：搜索页/列表页（zhaopin.com/zhaopin/、msearch.51job.com/jobs/、www.58.com/pp...、hr.7192.com/yinglou/、hr.7192.com/ertong/）、公司主页（b.dianzhangzhipin.com/storer|store/）、百度/搜狗中转链接（baidu.com/link、weixin.sogou.com/link，会过期或被风控）、BOSS直聘（zhipin.com 登录墙，用户打不开）、**黑光人才网移动端 mhr.heiguang.com（用户实测打不开，禁止挂）**——黑光岗位只有 mhr 链接时：不挂链接，改写 📝 指路「黑光人才网 App 搜索'公司名'直达岗位」
4. **字段填写规范（硬性，构建器靠这些字段计算"达标"）**：
   - 💰 **薪资**：必须写原始数字文本，格式如「底薪5000+提成30%」「月薪8000-12000」「底薪4000+每单提成200」「面议」。数字要真实，禁止编造
   - 🏷️ **档位**：高端/中端/低端 三选一。判定：客单价3000元以上或知名品牌连锁影楼（金夫人/韩国艺匠/巴黎春天/龙摄影等）= 高端；客单价1000-3000 = 中端；1000以下或不可得 = 低端
   - 💰 **客单价**：招聘信息里明确写了才填（如"客单价5000"），没写就省略该行
   - 📍 **区域**：鄞州区/海曙区/江北区/镇海区/北仑区/奉化区/慈溪/余姚/宁海/象山/全宁波 等
   - 📅 **发布日期**：源页面有日期才填，格式 YYYY-MM-DD；没有就省略
   - 📝 **指路**（可选字段）：**没有具体岗位帖但岗位真实且值得收录时**才写——写明在哪个平台搜什么关键词（如「58同城搜索'蓝佳美甲 化妆师'查看原帖」），让求职者能自己找到；有合格链接就不用写
   - **达标由构建器按薪资自动计算**（底薪+提成估算合计 ≥8000元/月），你只负责如实填薪资文本，**不要在标题里标✅**
5. **质量门槛与写文件时机（硬性）**：
   - 先按**扩大搜索矩阵**搜 18-25 个查询：区县（鄞州/海曙/江北/镇海/北仑/奉化/慈溪/余姚/宁海/象山）× 平台（58/智联/前程无忧/全职网/店长直聘/全影/黑光/象山明聘/宁波招聘网）× 岗位（化妆师/彩妆/造型/跟妆/儿童/助理/摄影师）。**每次更新必须换新关键词、扩大覆盖面**（可加"2026年8月""最新""新店""高薪""诚聘"等变化词），禁止只复用上次的查询；搜到新公司/新平台继续深挖该公司其他岗位
   - **时效验证（硬性）**：优先收录近 2-3 个月（2026-06 后）仍有发布记录的岗位；发布日期 3 个月以上且无近期更新迹象的 → 不收录或备注「时效待确认」；高薪岗位必须有搜索结果佐证近期仍在招
   - 够 10 条就动笔；**第15轮前必须 write_file 写入 {JOBS_MD}**（文件头部标注"本期收录N条"），剩余轮次补搜替换低质条目
   - 目标 30-40 条，实际搜到多少写多少，不足据实写，宁缺毋滥，**禁止编造岗位和链接**
   - 🆕 只标本次新收录的；保留旧岗位不标
6. 构建发布：先检测运行环境——run_bash 执行 `python -c "import os;print(1 if os.environ.get('MAKEUP_BASE_DIR') else 0)"`：
   - **输出 1 = 云端（GitHub Actions）**：只把 {JOBS_MD} 写好即可，**禁止执行任何 git/build 命令**（Actions 会自动构建部署并同步仓库），做完就结束
   - **输出 0 = 本地**：依次执行 `cd "{SITE_DIR}" && python build.py` → `cd "{SITE_DIR}" && git add -A && git commit -m "Update makeup jobs {TODAY_STR}" && git push origin main`

### 文件格式（{JOBS_MD}）
```
# 💄 宁波化妆师招聘聚合 | {TODAY_STR}更新
> 本期收录 **N** 条。高端影楼/公司优先展示。数据来源：58同城、BOSS直聘、智联、前程无忧、影楼官网及公众号推文。
## 🏆 高端
### 1. 🆕 机构名 — 岗位名
- 🎓 **学历要求**：xxx
- 📍 **区域**：海曙区
- 💰 **薪资**：底薪5000+提成30%
- 🏷️ **档位**：高端
- 💰 **客单价**：5000元/单
- 📅 **发布日期**：2026-08-24
- 🔗 [来源平台](真实URL)
- 📝 **指路**：无直链时才写，格式如「58同城搜索'xx公司'查看原帖」
## 🥈 中端
...
## ⚙️ 低端
...
*本栏目由 AI 采集编撰 | {TODAY_STR} | 岗位真实性与薪资请以原始发布为准*
```
分节图标固定：🏆 高端 / 🥈 中端 / ⚙️ 低端。构建器会按档位字段重新分组，分节顺序无所谓。

### 合规约束
- 所有链接必须来自 web_search 结果，禁止编造；**禁止 baidu.com/link、weixin.sogou.com/link 中转链接**（会过期），禁止搜索页/公司页冒充岗位帖
- **虚假内容识别（硬性）**：行业外公司（户外拓展/生物科技/农业/建筑等）挂"万元招化妆师"且描述含糊 = 刷岗/挂羊头风险，不收录；招聘中出现"交费培训/报名费/押金/贷款"类内容 = 招转培风险，收录时在💡备注加 ⚠️ 警示；薪资显著高于同岗 2 倍以上且岗位描述含糊 = 可疑不收录
- **旧链接抽查**：每次更新时对上一期 ≥10 条链接做复查（URL 格式白名单校验；搜索结果快照确认岗位仍存在），失效的转 📝 指路或删除
- 跳过敏感内容，换关键词继续，不收录
- 宁缺毋滥：宁可少写，不注水不硬凑"""

    return ""

# ── Agent Loop ─────────────────────────────────────────────────

def run(mode, manual=False):
    # Date gate: makeup runs on odd days only (wenbo jobs takes even days);
    # manual trigger (一键更新.bat) bypasses the date gate
    if mode == 'makeup' and not manual and TODAY.day % 2 == 0:
        print(f"SKIP: today is {TODAY.day}th, makeup jobs runs on odd days only")
        return

    # Single-instance guard: skip if a same-mode task is already running
    if not acquire_lock(mode):
        print(f"SKIP: another {mode} task is already running (lock held)")
        return

    # Cooldown guard: repeated triggers within 30 min of last completion are skipped
    rem = cooldown_remaining()
    if rem:
        print(f"SKIP: 距上次更新完成不足 {COOLDOWN_MINUTES} 分钟（还需约 {rem} 分钟），本次触发已跳过")
        return

    try:
        _run_loop(mode)
        # record completion time only if the loop finished without exception
        COOLDOWN_FILE.write_text(str(time.time()), encoding='utf-8')
    finally:
        release_lock(mode)

def _run_loop(mode):
    system_prompt = get_prompt(mode)
    if not system_prompt:
        print(f"Unknown mode: {mode}")
        sys.exit(1)

    mode_labels = {'makeup': '化妆师招聘'}
    label = mode_labels.get(mode, mode)

    messages = [{"role": "user", "content": f"请执行{label}任务。按系统提示的步骤完成。先搜索，再读取参考文件，然后撰写、构建并发布。"}]

    for turn in range(55):
        print(f"\n{'='*50} [{label}] Turn {turn+1}/55")

        resp = client.messages.create(
            model=MODEL, max_tokens=8000,
            system=system_prompt, tools=TOOLS, messages=messages)

        assistant_content = []
        tool_calls = []

        for block in resp.content:
            if block.type == 'text':
                text = block.text
                if text.strip():
                    print(f"[TEXT] {text[:250]}{'...' if len(text)>250 else ''}")
                assistant_content.append(block)
            elif block.type == 'tool_use':
                print(f"[TOOL] {block.name}: {json.dumps(block.input, ensure_ascii=False)[:200]}")
                tool_calls.append(block)
                assistant_content.append(block)  # must echo tool_use back, else tool_result has no matching tool_use
            elif block.type == 'thinking':
                assistant_content.append(block)

        messages.append({"role": "assistant", "content": assistant_content})

        if not tool_calls:
            print(f"\n✅ {label}任务完成")
            break

        tool_results = []
        for tc in tool_calls:
            fn = TOOL_MAP.get(tc.name)
            try:
                result = fn(**tc.input) if fn else f"Unknown: {tc.name}"
                print(f"[RESULT] {tc.name}: {str(result)[:150]}")
            except Exception as e:
                result = f"ERROR: {e}"
            tool_results.append({"type": "tool_result", "tool_use_id": tc.id, "content": str(result)})

        messages.append({"role": "user", "content": tool_results})

        if turn >= 34:
            print(f"\n⚠️ {label}到达最大轮次")
            break

    print(f"\n{'='*50}")

# ── Seed search (no model) ─────────────────────────────────────

SEED_QUERIES = [
    # 岗位类型 × 平台
    '宁波 化妆师 招聘 2026',
    '宁波 影楼 招聘 化妆师 2026',
    '宁波 婚纱摄影 化妆师 招聘 2026',
    '宁波 儿童摄影 化妆师 招聘 2026',
    '宁波 婚礼跟妆 化妆师 招聘 2026',
    '宁波 高端影楼 化妆师 招聘 2026',
    '宁波 化妆助理 招聘 2026',
    '宁波 造型师 招聘 2026',
    '宁波 彩妆师 招聘 2026',
    '宁波 摄影师 招聘 2026',
    '宁波 摄影助理 招聘 2026',
    '宁波 化妆师 58同城 招聘',
    '宁波 化妆师 智联招聘 岗位',
    '宁波 化妆师 前程无忧 招聘',
    '宁波 化妆师 全职招聘网',
    '宁波 化妆师 店长直聘',
    '宁波 化妆师 全影人才网',
    '宁波 化妆师 黑光人才网',
    # 区县矩阵
    '鄞州 化妆师 招聘',
    '海曙 化妆师 招聘',
    '江北 化妆师 招聘',
    '镇海 化妆师 招聘',
    '北仑 化妆师 招聘',
    '奉化 化妆师 招聘',
    '慈溪 化妆师 招聘',
    '余姚 化妆师 招聘',
    '宁海 化妆师 招聘',
    '象山 化妆师 招聘',
    # 场景/类型变化词
    '宁波 新店 化妆师 招聘 8月',
    '宁波 写真馆 化妆师 招聘 2026',
    '宁波 汉服 化妆师 招聘',
    '宁波 主播 化妆师 招聘',
    '宁波 新娘跟妆 招聘 2026',
    '宁波 影楼 助理 招聘 2026',
    '宁波 摄影工作室 化妆师 招聘',
    '化妆师 招聘 汇总 公众号',
    '影楼 招聘 汇总 宁波',
]

def seed_search():
    """Run SEED_QUERIES through web_search, dump all results (with query tag) to
    招聘/seed_results.json, flushing after each query so partial results survive."""
    out = BASE_DIR / '招聘' / 'seed_results.json'
    out.parent.mkdir(parents=True, exist_ok=True)
    all_items = []
    for i, q in enumerate(SEED_QUERIES, 1):
        print(f"[SEED {i}/{len(SEED_QUERIES)}] {q}", flush=True)
        try:
            res = web_search(q, 8)
            items = json.loads(res) if isinstance(res, str) else res
            if isinstance(items, list):
                for it in items:
                    it['query'] = q
                all_items.extend(items)
                print(f"  +{len(items)} items", flush=True)
            else:
                print(f"  non-list: {str(items)[:100]}", flush=True)
        except Exception as e:
            print(f"  error: {e}", flush=True)
        out.write_text(json.dumps(all_items, ensure_ascii=False, indent=1), encoding='utf-8')
        time.sleep(4)  # 防百度连续搜索限流
    print(f"DONE: {len(all_items)} results → {out}", flush=True)

if __name__ == '__main__':
    mode = sys.argv[1] if len(sys.argv) > 1 else 'makeup'
    if mode == 'seed':
        seed_search()
    elif mode == 'seedsearch':
        q = sys.argv[2]
        n = int(sys.argv[3]) if len(sys.argv) > 3 else 8
        print(web_search(q, n))
    else:
        manual = len(sys.argv) > 2 and sys.argv[2] == 'manual'
        run(mode, manual=manual)

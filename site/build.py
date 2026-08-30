"""
宁波化妆师招聘聚合 — build.py
读取 ../招聘/jobs.md → 生成 index.html + robots.txt + sitemap.xml

数据流: jobs.md → parse → 达标估算(底薪+提成合计≥8000) → 排序 → 分档渲染 → index.html
用法: python build.py （在 site/ 目录下执行）
"""
import re
import html as html_mod
import os
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
MD_FILE = BASE_DIR / '招聘' / 'jobs.md'
OUT_DIR = Path(__file__).parent

DOMAIN = 'makeup.zhangheng666.top'
GH_REPO = 'https://github.com/Zhangheng0610-nb/makeup-jobs'
QUALIFY_THRESHOLD = 8000  # 达标 = 底薪+提成估算合计 ≥ 8000 元/月

REGIONS = ('鄞州', '海曙', '江北', '镇海', '北仑', '奉化', '慈溪', '余姚', '宁海', '象山', '舟山')
TIER_ORDER = [('high', '🏆', '高端'), ('mid', '🥈', '中端'), ('low', '⚙️', '低端')]
TIER_KEY = {'高': 'high', '中': 'mid', '低': 'low', 'end': 'mid', '端': 'low'}

# ── 数值/达标估算 ─────────────────────────────────────────────

def to_number(x) -> float | None:
    """'5000'→5000, '8K'→8000, '1.2万'→12000"""
    m = re.search(r'(\d+(?:\.\d+)?)\s*([Kk万]?)', str(x))
    if not m:
        return None
    v = float(m.group(1))
    suf = m.group(2)
    if suf == '万':
        return v * 10000
    if suf in ('K', 'k'):
        return v * 1000
    return v

def estimate(salary: str, price: str = '') -> int | None:
    """从薪资文本估算月入（确定性规则，保守口径）：
    - 区间薪资取中值；单值月薪取单值；'月入过万'→10000
    - 有底薪：提成% × 客单价 × 4单/月（无客单价按底薪×30%）；每单提成×4单/月
    - 无任何数字 → None（不达标，卡片照常显示原文）
    """
    s = (salary or '').strip()
    if not re.search(r'\d', s):
        return None
    # 剔除客单价子句，避免"客单价5000-8000"被误当月薪
    s_main = re.sub(r'[（(][^）)]*客单价[^）)]*[）)]', '', s)
    s_main = re.sub(r'客单价[^，。；;、]*', '', s_main)

    base = None
    m_base = re.search(r'底薪\s*(\d+(?:\.\d+)?)\s*[Kk万]?', s_main)
    if m_base:
        base = to_number(m_base.group(1))

    est = None
    m_range = re.search(r'(?:月薪|薪资|综合|工资)[：:（(]?\s*(\d+(?:\.\d+)?)\s*[Kk万]?\s*[-~至]\s*(\d+(?:\.\d+)?)\s*[Kk万]?', s_main)
    m_single = re.search(r'(?:月薪|月入|综合|工资)[：:（(]?\s*(\d+(?:\.\d+)?)\s*[Kk万]?(?:元)?', s_main)
    if m_range:
        lo = to_number(m_range.group(1))
        hi = to_number(m_range.group(2))
        est = int(round((lo + hi) / 2)) if lo and hi else (lo or hi)
    elif m_single:
        est = to_number(m_single.group(1))
    elif '月入过万' in s_main:
        est = 10000
    else:
        m_gen = re.search(r'(\d+(?:\.\d+)?)\s*[Kk万]?\s*[-~至]\s*(\d+(?:\.\d+)?)\s*[Kk万]?(?:元)?', s_main)
        if m_gen:
            est = to_number(m_gen.group(1))

    if base is not None:
        comm = 0
        m_rate = re.search(r'提成\s*(\d+(?:\.\d+)?)\s*%', s_main)
        if m_rate:
            rate = float(m_rate.group(1)) / 100
            p = to_number(price) if re.search(r'\d', str(price or '')) else 0
            comm = p * rate * 4 if p else base * 0.3  # 客单价×提成%×4单/月；无客单价按底薪30%保守估
        else:
            m_per = re.search(r'提成\s*(\d+(?:\.\d+)?)\s*[Kk万]?\s*元?\s*[/／]\s*单', s_main)
            if m_per:
                comm = to_number(m_per.group(1)) * 4
            else:
                m_per2 = re.search(r'每单提成\s*(\d+(?:\.\d+)?)\s*[Kk万]?\s*元?', s_main)
                if m_per2:
                    comm = to_number(m_per2.group(1)) * 4
        est = est or int(round(base + comm))
    return int(round(est)) if est is not None else None

def fmt_money(v: int | None) -> str:
    if v is None:
        return ''
    if v >= 10000:
        w = v / 10000
        return f"{int(w)}万" if w == int(w) else f"{w:.1f}万"
    return f"{v}元"

# ── 解析 ──────────────────────────────────────────────────────

def md_inline(text: str) -> str:
    """行内 markdown：**bold** → <strong>（先转义防注入）"""
    t = html_mod.escape(text)
    t = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', t)
    return t

def parse_jobs(md_file: Path) -> dict:
    text = md_file.read_text(encoding='utf-8')
    lines = text.splitlines()

    update_date = date.today()
    m_date = re.search(r'# .+?\|\s*(\d{4})[年-](\d{1,2})[月-](\d{1,2})(?:日)?', text)
    if m_date:
        update_date = date(int(m_date.group(1)), int(m_date.group(2)), int(m_date.group(3)))

    summary = []
    items = []
    cur = None
    for ln in lines:
        s = ln.strip()
        if s.startswith('> '):
            summary.append(s[2:].strip())
            continue
        m_item = re.match(r'###\s+(\d+)\.\s+(?:🆕\s*)?(?:✅\s*)?(.+?)\s*(?:[—\-]\s*(.+))?$', s)
        if m_item:
            cur = {
                'number': int(m_item.group(1)),
                'is_new': '🆕' in s[:20],
                'company': m_item.group(2).strip(),
                'position': (m_item.group(3) or '').strip(),
                'salary': '', 'tier': '', 'price': '', 'region': '',
                'publish_date': '', 'education': '', 'location': '', 'deadline': '',
                'note': '', 'guide': '', 'links': [],
                'verification_status': '', 'verification_source': '',
                'verification_url': '', 'direct_url': '', 'verification_score': '',
                'verified_at': '', 'verification_evidence': '', 'verification_error': '',
            }
            items.append(cur)
            continue
        m_field = re.match(r'-\s*.+?\*\*(.+?)\*\*\s*[：:]\s*(.+)', s)
        if cur and m_field:
            name, val = m_field.group(1), m_field.group(2).strip()
            if '客单价' in name:
                cur['price'] = val
            elif '薪资' in name or '薪' in name:
                cur['salary'] = val
            elif '档位' in name:
                cur['tier'] = val
            elif '区域' in name:
                cur['region'] = val
            elif '发布' in name:
                cur['publish_date'] = val
            elif '学历' in name:
                cur['education'] = val
            elif '地点' in name:
                cur['location'] = val
            elif '截止' in name:
                cur['deadline'] = val
            elif '指路' in name:
                cur['guide'] = val
            elif name == 'verification_status':
                cur['verification_status'] = val.lower()
            elif name == 'verification_source':
                cur['verification_source'] = val
            elif name == 'verification_url':
                cur['verification_url'] = val
            elif name == 'direct_url':
                cur['direct_url'] = val
            elif name == 'verification_score':
                cur['verification_score'] = val
            elif name == 'verified_at':
                cur['verified_at'] = val
            elif name == 'verification_evidence':
                cur['verification_evidence'] = val
            elif name == 'verification_error':
                cur['verification_error'] = val
            continue
        m_link = re.match(r'-\s*🔗\s*\[(.+?)\]\((.+?)\)', s)
        if cur and m_link:
            cur['links'].append((m_link.group(1).strip(), m_link.group(2).strip()))
            continue
        if cur and s.startswith('- 💡'):
            cur['note'] = s[3:].strip()

    # 后处理：区域/档位/日期兜底 + 达标估算
    for it in items:
        if not it['region']:
            r = re.search('|'.join(REGIONS), it['location'])
            it['region'] = r.group(0) if r else ('全宁波' if '宁波' in it['location'] else '宁波')
        if not it['tier']:
            p = to_number(it['price'])
            it['tier'] = '高端' if (p and p >= 3000) else '中端' if (p and p >= 1000) else '低端'
        if not it['publish_date']:
            it['publish_date'] = update_date.isoformat()
        it['est'] = estimate(it['salary'], it['price'])
        it['qualify'] = it['est'] is not None and it['est'] >= QUALIFY_THRESHOLD
        it['tier_key'] = TIER_KEY.get(it['tier'][0], 'low')
        m_pub = re.search(r'(\d{4})[-年](\d{1,2})[-月](\d{1,2})', it['publish_date'])
        try:
            it['date_ord'] = date(int(m_pub.group(1)), int(m_pub.group(2)), int(m_pub.group(3))).toordinal()
        except Exception:
            it['date_ord'] = update_date.toordinal()

    # 排序：达标优先 → 有直链优先 → 估算月入降序 → 发布日期新 → 序号
    items.sort(key=lambda x: (
        0 if x['qualify'] else 1,
        1 if ('待核验' in x['company'] or (x['guide'] and not x['links'])) else 0,
        -(x['est'] or -1), -x['date_ord'], x['number']))
    return {'update_date': update_date, 'summary': summary, 'items': items}

# ── 渲染 ──────────────────────────────────────────────────────

CSS = """
:root {
  --bg:#f8f6f3; --card:#ffffff; --text:#20232a; --muted:#6b7280;
  --border:#e7e1d8; --accent:#d6336c; --accent-soft:#fce8f0;
  --gold:#a16207; --gold-soft:#fbf3d9; --blue:#1d4ed8; --blue-soft:#e3edff;
  --gray:#6b7280; --gray-soft:#f1f1f3; --red:#dc2626; --red-soft:#fdeaea;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg:#17171c; --card:#1f1f26; --text:#eceaf0; --muted:#9b97a3;
    --border:#2e2e38; --accent:#f06595; --accent-soft:#3a2230;
    --gold:#e8c84a; --gold-soft:#38301a; --blue:#7cb0ff; --blue-soft:#1d2740;
    --gray:#9b97a3; --gray-soft:#26262e; --red:#ff6b5b; --red-soft:#3a2222;
  }
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  background: var(--bg); color: var(--text);
  font-family: -apple-system, "PingFang SC", "Microsoft YaHei", "Segoe UI", sans-serif;
  line-height: 1.6;
}
.wrap { max-width: 860px; margin: 0 auto; padding: 24px 16px 48px; }
header h1 { font-size: 1.6rem; margin-bottom: 4px; }
.meta { color: var(--muted); font-size: .9rem; margin-bottom: 12px; }
.stats-bar { display: flex; gap: 8px; flex-wrap: wrap; margin: 12px 0 16px; }
.stat-item {
  background: var(--card); border: 1px solid var(--border); border-radius: 10px;
  padding: 6px 14px; font-size: .9rem; color: var(--muted);
}
.stat-item b { color: var(--text); }
.update-banner {
  background: var(--card); border: 1px dashed var(--accent); border-radius: 10px;
  padding: 10px 16px; font-size: .9rem; margin: 0 0 12px; color: var(--muted);
}
.update-banner a { color: var(--accent); font-weight: 600; }
.summary {
  background: var(--card); border: 1px solid var(--border); border-left: 4px solid var(--accent);
  border-radius: 10px; padding: 8px 12px; font-size: .86rem; margin-bottom: 12px;
}
.summary-src { display: none; }
.filter-bar {
  position: sticky; top: 0; z-index: 10; background: var(--bg);
  padding: 10px 0; display: flex; gap: 8px; flex-wrap: wrap; align-items: center;
  border-bottom: 1px solid var(--border); margin-bottom: 16px;
}
.filter-bar button {
  border: 1px solid var(--border); background: var(--card); color: var(--text);
  border-radius: 999px; padding: 5px 14px; font-size: .85rem; cursor: pointer;
}
.filter-bar button.active {
  background: var(--accent); border-color: var(--accent); color: #fff;
}
.filter-bar input, .filter-bar select {
  border: 1px solid var(--border); background: var(--card); color: var(--text);
  border-radius: 999px; padding: 5px 14px; font-size: .85rem; outline: none;
}
.filter-bar input { flex: 1; min-width: 140px; }
.filter-bar input:focus { border-color: var(--accent); }
mark { background: #ffe066; color: #20232a; border-radius: 3px; padding: 0 1px; }
.job-section { margin-bottom: 28px; }
.job-section.hidden { display: none; }
.section-title {
  font-size: 1.15rem; margin-bottom: 10px; display: flex; align-items: center; gap: 8px;
}
.count-badge {
  background: var(--accent-soft); color: var(--accent); border-radius: 999px;
  padding: 2px 10px; font-size: .8rem; font-weight: 600;
}
.job-item {
  background: var(--card); border: 1px solid var(--border); border-radius: 12px;
  padding: 14px 16px; margin-bottom: 10px;
}
.job-item.hidden { display: none; }
.job-item.qualify-row { border-left: 4px solid var(--red); }
.job-header { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.job-number { color: var(--muted); font-size: .8rem; }
.job-title { font-weight: 600; font-size: .98rem; }
.qualify-badge {
  background: var(--red); color: #fff; border-radius: 999px;
  padding: 1px 9px; font-size: .75rem; font-weight: 600; white-space: nowrap;
}
.guide-badge {
  background: #fff1d6; color: #a15c00; border-radius: 999px;
  padding: 1px 9px; font-size: .75rem; font-weight: 600; white-space: nowrap;
}
.verified-badge, .located-badge, .weak-badge {
  border-radius: 999px; padding: 1px 9px; font-size: .75rem;
  font-weight: 600; white-space: nowrap;
}
.verified-badge { background: #dcfce7; color: #166534; }
.located-badge { background: var(--blue-soft); color: var(--blue); }
.weak-badge { background: #fff1d6; color: #a15c00; }
.new-badge {
  background: var(--accent-soft); color: var(--accent); border-radius: 999px;
  padding: 1px 9px; font-size: .75rem; white-space: nowrap;
}
.job-meta { display: flex; gap: 6px 14px; flex-wrap: wrap; margin-top: 8px; font-size: .88rem; color: var(--muted); }
.job-meta .salary { color: var(--text); font-weight: 600; }
.job-meta .est { color: var(--accent); font-weight: 600; font-size: .82rem; font-style: normal; }
.tier { border-radius: 6px; padding: 0 7px; font-size: .8rem; font-weight: 500; }
.tier-high { background: var(--gold-soft); color: var(--gold); }
.tier-mid { background: var(--blue-soft); color: var(--blue); }
.tier-low { background: var(--gray-soft); color: var(--gray); }
.job-link { margin-top: 8px; font-size: .88rem; }
.job-link a { color: var(--accent); text-decoration: none; word-break: break-all; }
.job-link a.verify-link { color: var(--blue); margin-left: 12px; }
.job-link a:hover { text-decoration: underline; }
.job-note { margin-top: 6px; font-size: .85rem; color: var(--text); background: var(--accent-soft); border-left: 3px solid var(--accent); padding: 5px 10px; border-radius: 4px; }
.job-guide { margin-top: 6px; font-size: .85rem; color: var(--blue); background: var(--blue-soft); border-left: 3px solid var(--blue); padding: 5px 10px; border-radius: 4px; }
.disclaimer {
  background: var(--card); border: 1px dashed var(--border); border-radius: 10px;
  padding: 10px 14px; font-size: .8rem; color: var(--muted); margin-top: 24px;
}
footer { margin-top: 16px; font-size: .8rem; color: var(--muted); text-align: center; }
footer a { color: var(--muted); }
@media (max-width: 600px) {
  .wrap { padding: 14px 10px 32px; }
  header h1 { font-size: 1.28rem; }
  .meta { font-size: .82rem; margin-bottom: 8px; }
  .stats-bar { margin: 8px 0 10px; }
  .update-banner { padding: 8px 10px; font-size: .82rem; }
  .summary { padding: 7px 10px; font-size: .82rem; margin-bottom: 8px; }
  .filter-bar { padding: 7px 0; gap: 6px; margin-bottom: 12px; }
  .filter-bar button { padding: 5px 10px; font-size: .78rem; }
  .filter-bar input, .filter-bar select { padding: 5px 10px; font-size: .8rem; }
  .filter-bar input { min-width: 120px; }
  .job-section { margin-bottom: 20px; }
  .section-title { font-size: 1.05rem; margin-bottom: 8px; }
  .job-item { padding: 11px 12px; margin-bottom: 8px; border-radius: 10px; }
  .job-title { font-size: .92rem; }
  .job-meta { gap: 4px 9px; margin-top: 6px; font-size: .8rem; }
  .job-link, .job-note, .job-guide { font-size: .8rem; }
  .job-link a.verify-link { margin-left: 8px; }
  .job-note, .job-guide { padding: 4px 8px; }
  .disclaimer { margin-top: 16px; padding: 8px 10px; }
}
"""

def tier_icon(key: str) -> str:
    return {'high': '🏆', 'mid': '🥈', 'low': '⚙️'}[key]

def render_card(it: dict) -> str:
    meta = []
    if it['salary']:
        est_html = f' <em class="est">估月入约{fmt_money(it["est"])}</em>' if it['est'] else ''
        meta.append(f'<span class="salary">💰 {html_mod.escape(it["salary"])}{est_html}</span>')
    meta.append(f'<span class="tier tier-{it["tier_key"]}">{tier_icon(it["tier_key"])} {html_mod.escape(it["tier"])}</span>')
    meta.append(f'<span>📍 {html_mod.escape(it["region"] or it["location"] or "宁波")}</span>')
    if it['price']:
        meta.append(f'<span>💵 客单价 {html_mod.escape(it["price"])}</span>')
    if it['education']:
        meta.append(f'<span>🎓 {html_mod.escape(it["education"])}</span>')
    if it['publish_date']:
        meta.append(f'<span>📅 {html_mod.escape(it["publish_date"][:10])}</span>')
    links = ''.join(
        f'<a href="{html_mod.escape(u)}" target="_blank" rel="noopener">🔗 {html_mod.escape(t)}</a>'
        for t, u in it['links'])
    verify_url = it.get('direct_url') or it.get('verification_url')
    if verify_url and verify_url not in {u for _, u in it['links']}:
        verify_label = 'BOSS详情（可能需登录）' if 'zhipin.com/job_detail/' in verify_url else '核验来源'
        links += f'<a class="verify-link" href="{html_mod.escape(verify_url)}" target="_blank" rel="noopener">🔎 {verify_label}</a>'
    if not links:
        links = '<span>🧭 见下方指路（无直链）</span>'
    note = f'<div class="job-note">💡 {md_inline(it["note"])}</div>' if it['note'] else ''
    guide = f'<div class="job-guide">🧭 指路：{md_inline(it["guide"])}</div>' if it['guide'] else ''

    search_text = ' '.join([it['company'], it['position'], it['salary'], it['tier'],
                            it['region'], it['price'], it['location'],
                            it.get('verification_source', ''), it.get('verification_status', '')]).lower()
    badges = ''
    if it['qualify']:
        badges += '<span class="qualify-badge">✅ 达标8K+</span>'
    status = it.get('verification_status', '')
    if not status and not it['links']:
        status = 'pending'
    status_labels = {
        'verified': ('verified-badge', '✅ 已核验'),
        'located': ('located-badge', '🔗 已确认在招'),
        'weak_verified': ('weak-badge', '⚠️ 已发现相关招聘'),
        'pending': ('guide-badge', '🧭 待核验'),
    }
    if status in status_labels:
        cls, label = status_labels[status]
        badges += f'<span class="{cls}">{label}</span>'
    if it['is_new']:
        badges += '<span class="new-badge">🆕 新增</span>'
    title = f"{it['company']} — {it['position']}" if it['position'] else it['company']

    return f'''<div class="job-item{" qualify-row" if it["qualify"] else ""}" data-tier="{it["tier_key"]}" data-qualify="{1 if it["qualify"] else 0}" data-direct="{1 if it["links"] else 0}" data-status="{html_mod.escape(status)}" data-salary="{it["est"] or ''}" data-region="{html_mod.escape(it["region"] or '宁波')}" data-search="{html_mod.escape(search_text)}">
  <div class="job-header">
    <span class="job-number">#{it["number"]}</span>
    <span class="job-title">{html_mod.escape(title)}</span>
{badges}
  </div>
  <div class="job-meta">{''.join(meta)}</div>
  <div class="job-link">{links}</div>
{note}
{guide}
</div>'''

JS = """
const state = { tab: 'all', region: 'all', kw: '' };
function escapeHtml(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
function apply() {
  const kw = state.kw.trim().toLowerCase();
  let shown = 0, shownQual = 0;
  document.querySelectorAll('.job-item').forEach(card => {
    const okTab = state.tab === 'all'
      || (state.tab === 'qualify' && card.dataset.qualify === '1')
      || (state.tab === 'direct' && card.dataset.direct === '1')
      || card.dataset.tier === state.tab;
    const okRegion = state.region === 'all' || card.dataset.region === state.region;
    const okKw = !kw || card.dataset.search.includes(kw);
    const show = okTab && okRegion && okKw;
    card.classList.toggle('hidden', !show);
    if (show) { shown++; if (card.dataset.qualify === '1') shownQual++; }
  });
  document.querySelectorAll('.job-section').forEach(sec => {
    const any = Array.from(sec.querySelectorAll('.job-item')).some(c => !c.classList.contains('hidden'));
    sec.classList.toggle('hidden', !any);
  });
  document.querySelectorAll('.filter-bar button').forEach(b =>
    b.classList.toggle('active', b.dataset.tab === state.tab));
  document.getElementById('stat-shown').textContent = shown;
  document.getElementById('stat-shown-qualify').textContent = shownQual;
  document.querySelectorAll('.job-title').forEach(el => {
    const orig = el.dataset.orig || (el.dataset.orig = el.textContent);
    if (kw && kw.length >= 2) {
      const i = orig.toLowerCase().indexOf(kw);
      if (i >= 0) {
        el.innerHTML = escapeHtml(orig.slice(0, i)) + '<mark>' + escapeHtml(orig.slice(i, i + kw.length)) + '</mark>' + escapeHtml(orig.slice(i + kw.length));
        return;
      }
    }
    el.textContent = orig;
  });
}
document.querySelectorAll('.filter-bar button').forEach(b =>
  b.addEventListener('click', () => { state.tab = b.dataset.tab; apply(); }));
document.getElementById('region-filter').addEventListener('change', e => { state.region = e.target.value; apply(); });
let timer = null;
document.getElementById('kw-filter').addEventListener('input', e => {
  clearTimeout(timer);
  timer = setTimeout(() => { state.kw = e.target.value; apply(); }, 200);
});
apply();
"""

def build_html(data: dict) -> str:
    update_date = data['update_date'].isoformat()
    items = data['items']
    total = len(items)
    qualified = sum(1 for it in items if it['qualify'])
    def status_of(it):
        status = it.get('verification_status', '')
        return status if status in {'verified', 'located', 'weak_verified', 'pending'} else ('pending' if not it['links'] else 'verified')
    verified = sum(1 for it in items if status_of(it) == 'verified')
    located = sum(1 for it in items if status_of(it) == 'located')
    weak = sum(1 for it in items if status_of(it) == 'weak_verified')
    pending = sum(1 for it in items if status_of(it) == 'pending')
    summary_html = (
        f'<p><b>{verified}</b> 条已核验 · <b>{located}</b> 条已确认在招 · <b>{weak}</b> 条待复核 · <b>{pending}</b> 条待核验 · <b>✅ {qualified}</b> 条估算月入8K+</p>'
        f'<p class="summary-src">数据来源：58同城、智联、前程无忧、店长直聘、全影人才网、象山明聘、小红书、抖音等。</p>'
    )

    # 区域选项：常见区域排序靠前（'鄞州区'→按'鄞州'排序）
    def region_key(r):
        base = r[:-1] if r.endswith('区') else r
        return REGIONS.index(base) if base in REGIONS else 99
    regions = sorted({it['region'] for it in items if it['region']}, key=region_key)
    region_opts = '<option value="all">全部区域</option>' + ''.join(
        f'<option value="{html_mod.escape(r)}">{html_mod.escape(r)}</option>' for r in regions)

    sections = []
    for key, icon, label in TIER_ORDER:
        group = [it for it in items if it['tier_key'] == key]
        if not group:
            continue
        cards = ''.join(render_card(it) for it in group)
        sections.append(f'''<section class="job-section" id="sec-{key}">
  <h2 class="section-title">{icon} {label} <span class="count-badge">{len(group)} 岗</span></h2>
  {cards}
</section>''')

    return f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>💄 宁波化妆师招聘聚合 | {update_date}更新</title>
<meta name="description" content="宁波本地化妆师/彩妆师/造型师/摄影岗位聚合，高端影楼优先，月入8K+达标标注，每两日更新。">
<script>if(location.protocol==='http:'&&!location.hostname.startsWith('localhost')&&location.hostname!=='127.0.0.1')location.replace('https://'+location.host+location.pathname+location.search)</script>
<meta property="og:title" content="宁波化妆师招聘聚合 | {update_date}更新">
<meta property="og:description" content="宁波本地化妆师/摄影岗位聚合，月入8K+达标优先展示，共 {total} 个岗位">
<meta property="og:url" content="https://{DOMAIN}/">
<meta property="og:type" content="website">
<meta property="og:site_name" content="宁波化妆师招聘聚合">
<meta name="twitter:card" content="summary">
<link rel="canonical" href="https://{DOMAIN}/">
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>💄 宁波化妆师招聘聚合</h1>
    <p class="meta">📅 更新于 {update_date} · 共 <b>{total}</b> 条 · 已核验 <b>{verified}</b> · 已确认在招 <b>{located}</b> · 待核验 <b>{pending}</b> · 达标 <b id="stat-shown-qualify">{qualified}</b></p>
  </header>
  <div class="update-banner">🧑‍💻 要更新岗位？<a href="update.html">查看运营说明</a>（发布前自动核验：直链、在招状态与待复核分层）</div>
  <div class="summary">{summary_html}</div>
  <div class="filter-bar">
    <button data-tab="all" class="active">全部</button>
    <button data-tab="qualify">✅ 达标8K+</button>
    <button data-tab="direct">🔗 有直链</button>
    <button data-tab="high">🏆 高端</button>
    <button data-tab="mid">🥈 中端</button>
    <button data-tab="low">⚙️ 低端</button>
    <select id="region-filter">{region_opts}</select>
    <input id="kw-filter" type="search" placeholder="🔍 搜岗位/公司/关键词…" autocomplete="off">
  </div>
  {''.join(sections)}
  <div class="disclaimer">⚠️ 岗位来自公开网络，真实性与薪资请以原始发布为准；「达标」为薪资中值或底薪+提成的估算（月入≥8K），实际收入以面试为准。</div>
  <footer>
    <a href="{GH_REPO}">GitHub</a> ｜ 每两日更新 ｜ 适合有经验的化妆师/造型师/摄影师
  </footer>
</div>
<script>{JS}</script>
</body>
</html>'''

def build_sitemap(update_date: str) -> str:
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://{DOMAIN}/</loc>
    <lastmod>{update_date}</lastmod>
    <changefreq>weekly</changefreq>
    <priority>1.0</priority>
  </url>
</urlset>
'''

def main():
    # GitHub Actions runs this builder after checkout.  Run the verifier here
    # so the existing deploy workflow also gets the verification gate even if
    # its YAML cannot be edited with the current token scope.  Local builds
    # remain deterministic and do not make network requests.
    if os.environ.get('GITHUB_ACTIONS', '').lower() == 'true' and not os.environ.get('MAKEUP_VERIFY_IN_BUILD'):
        verifier = BASE_DIR / 'verify_jobs.py'
        if verifier.exists():
            verify_env = os.environ.copy()
            verify_env['MAKEUP_VERIFY_IN_BUILD'] = '1'
            subprocess.run([sys.executable, str(verifier), '--apply'], cwd=str(BASE_DIR), check=True, env=verify_env)
    data = parse_jobs(MD_FILE)
    html_out = build_html(data)
    (OUT_DIR / 'index.html').write_text(html_out, encoding='utf-8')
    (OUT_DIR / 'robots.txt').write_text(f'User-agent: *\nAllow: /\n\nSitemap: https://{DOMAIN}/sitemap.xml\n', encoding='utf-8')
    (OUT_DIR / 'sitemap.xml').write_text(build_sitemap(data['update_date'].isoformat()), encoding='utf-8')

    qualified = sum(1 for it in data['items'] if it['qualify'])
    tiers = {k: sum(1 for it in data['items'] if it['tier_key'] == k) for k, _, _ in TIER_ORDER}
    print(f"[BUILD] {data['update_date']} 更新 | 总 {len(data['items'])} 岗 | 达标 {qualified} | "
          f"高端 {tiers['high']} / 中端 {tiers['mid']} / 低端 {tiers['low']}")
    for it in data['items']:
        flag = '✅' if it['qualify'] else '  '
        print(f"  {flag} [{it['tier']}] {it['company']} — {it['position']} | {it['salary'][:40]} | 估{fmt_money(it['est'])}")

if __name__ == '__main__':
    main()

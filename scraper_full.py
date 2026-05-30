# -*- coding: utf-8 -*-
"""
且慢长赢 — 全量发车记录抓取 + Supabase 写入 + QQ邮件通知
S计划 + 150计划，所有基金，upsert 到 Supabase，有新增时发邮件
"""
import sys, os, re, time, smtplib
from datetime import datetime
from email.mime.text import MIMEText
from email.header import Header
sys.stdout.reconfigure(encoding='utf-8')

from playwright.sync_api import sync_playwright
from supabase import create_client

# ===== 配置 =====
SUPABASE_URL = os.environ['SUPABASE_URL']
SUPABASE_KEY = os.environ['SUPABASE_KEY']
QQ_EMAIL = os.environ['QQ_EMAIL']
QQ_AUTH_CODE = os.environ['QQ_AUTH_CODE']

PLANS = {
    'S': {
        'name': '长赢S计划',
        'comp_url': 'https://qieman.com/longwin/compositions/LONG_WIN_S',
        'fund_url': 'https://qieman.com/longwin/funds/{code}?prodCode=LONG_WIN_S',
        'prod': 'LONG_WIN_S',
    },
    '150': {
        'name': '长赢150计划',
        'comp_url': 'https://qieman.com/longwin/compositions/LONG_WIN',
        'fund_url': 'https://qieman.com/longwin/funds/{code}?prodCode=LONG_WIN',
        'prod': 'LONG_WIN',
    },
}

CAT_ORDER = ['A股', '海外新兴市场股票', '境内债券', '海外债券', '海外成熟市场股票', '原油', '黄金']
BATCH_SIZE = 5
DELAY = 2


def extract_funds(html, prod):
    cat_positions = []
    for cat in CAT_ORDER:
        for m in re.finditer(re.escape(cat), html):
            tail = html[m.end():m.end() + 50]
            if '共' in tail or '已清仓' in tail:
                cat_positions.append((m.start(), cat))
                break
    cat_positions.sort()

    funds, seen = [], set()
    link_pat = rf'<a[^>]*href="/longwin/funds/(\d+)\?prodCode={prod}[^"]*"[^>]*>'
    for m in re.finditer(link_pat, html):
        code = m.group(1)
        if code in seen:
            continue
        seen.add(code)
        tail = html[m.end():m.end() + 600]
        nm = re.search(r'<section>([^<]+)', tail)
        name = nm.group(1).strip().replace('&nbsp;', '') if nm else code
        cat = ''
        for i in range(len(cat_positions) - 1, -1, -1):
            if m.start() > cat_positions[i][0]:
                cat = cat_positions[i][1]
                break
        funds.append({'code': code, 'name': name, 'category': cat})
    return funds


def extract_trades(html):
    records = []
    ln_m = re.search(r'最新净值[^0-9]*([\d.]+)', html)
    latest_nav = float(ln_m.group(1)) if ln_m else 0.0
    for m in re.finditer(
        r'(\d{4}-\d{2}-\d{2})<br><span[^>]*>交易时净值：<span[^>]*>([\d.]+)</span>', html
    ):
        act_m = re.search(r'(买入|卖出)\s*(\d+)\s*份', html[m.end():m.end() + 50])
        if act_m:
            u = int(act_m.group(2))
            records.append({
                'date': m.group(1),
                'trade_nav': float(m.group(2)),
                'action': act_m.group(1),
                'units': -u if act_m.group(1) == '卖出' else u,
            })
    return records, latest_nav


def scrape_plan(page, plan_key):
    cfg = PLANS[plan_key]
    print(f'\n{"=" * 60}')
    print(f'[{cfg["name"]}] {cfg["comp_url"]}')
    print(f'{"=" * 60}')

    page.goto(cfg['comp_url'], wait_until='networkidle', timeout=30000)
    html = page.content()
    funds = extract_funds(html, cfg['prod'])
    print(f'  基金: {len(funds)} 只')

    all_records = []
    for i, fund in enumerate(funds):
        url = cfg['fund_url'].format(code=fund['code'])
        page.goto(url, wait_until='networkidle', timeout=30000)
        detail = page.content()
        records, latest_nav = extract_trades(detail)

        for r in records:
            all_records.append({
                'plan': plan_key,
                'category': fund['category'],
                'fund_name': fund['name'],
                'fund_code': fund['code'],
                'trade_date': r['date'],
                'action': r['action'],
                'units': r['units'],
                'trade_nav': r['trade_nav'],
                'latest_nav': latest_nav,
            })

        if (i + 1) % BATCH_SIZE == 0 and i + 1 < len(funds):
            print(f'  {i + 1}/{len(funds)} 已抓取, {len(all_records)} 条, 暂停 {DELAY}s...')
            time.sleep(DELAY)

    print(f'  完成: {len(funds)} 只基金, {len(all_records)} 条记录')
    return all_records


def send_email(new_records, updated_count, stats):
    """发送QQ邮件通知"""
    plan_s = [r for r in new_records if r['plan'] == 'S']
    plan_150 = [r for r in new_records if r['plan'] == '150']

    lines = [f'且慢长赢发车记录更新 — {datetime.now().strftime("%Y-%m-%d %H:%M")}']
    lines.append('')
    lines.append(f'新增 {len(new_records)} 条 | 更新 {updated_count} 条')
    lines.append(f'S计划 {stats["s_count"]} 条 | 150计划 {stats["w150_count"]} 条')
    lines.append(f'数据库 {stats["before"]} → {stats["after"]} | 耗时 {stats["elapsed"]:.0f}s')
    lines.append('')

    if new_records:
        lines.append('—' * 50)
        lines.append(f'{"日期":<12} {"计划":<6} {"基金":<18} {"操作":<5} {"份数":>4} {"净值":>8}')
        for r in new_records[:30]:  # 最多30条
            plan_label = 'S' if r['plan'] == 'S' else '150'
            lines.append(
                f'{r["trade_date"]:<12} {plan_label:<6} {r["fund_name"]:<18} '
                f'{r["action"]:<5} {r["units"]:>4} {r["trade_nav"]:>8.4f}'
            )
        if len(new_records) > 30:
            lines.append(f'  ... 还有 {len(new_records) - 30} 条')

    body = '\n'.join(lines)

    msg = MIMEText(body, 'plain', 'utf-8')
    msg['From'] = QQ_EMAIL
    msg['To'] = QQ_EMAIL
    msg['Subject'] = Header(f'且慢发车更新 +{len(new_records)} | {datetime.now().strftime("%m-%d")}', 'utf-8')

    try:
        with smtplib.SMTP_SSL('smtp.qq.com', 465) as server:
            server.login(QQ_EMAIL, QQ_AUTH_CODE)
            server.sendmail(QQ_EMAIL, [QQ_EMAIL], msg.as_string())
        print('邮件已发送')
    except Exception as e:
        print(f'邮件发送失败: {e}')


def main():
    start_time = time.time()
    print(f'且慢长赢 发车记录全量抓取')
    print(f'开始: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')

    # ===== 1. 抓取 =====
    all_records = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        for pk in ['S', '150']:
            records = scrape_plan(page, pk)
            all_records.extend(records)
        browser.close()

    scrape_time = time.time() - start_time
    print(f'\n抓取总计: {len(all_records)} 条, 耗时 {scrape_time:.0f}s')

    if not all_records:
        print('无记录，退出')
        return

    # ===== 2. Supabase 写入 =====
    print(f'\n写入 Supabase...')
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

    # 查现有数据
    existing = supabase.table('trade_records').select('plan,fund_code,trade_date,action,units').execute()
    existing_keys = set()
    if existing.data:
        for r in existing.data:
            existing_keys.add((r['plan'], r['fund_code'], str(r['trade_date']), r['action'], r['units']))
    before_count = len(existing_keys)

    # Upsert
    supabase.table('trade_records').upsert(
        all_records,
        on_conflict='plan,fund_code,trade_date,action,units',
        ignore_duplicates=False
    ).execute()

    # 查写入后
    after_check = supabase.table('trade_records').select('id', count='exact').execute()
    after_count = after_check.count if hasattr(after_check, 'count') else len(after_check.data or [])

    # 找出新增记录
    new_records = []
    for r in all_records:
        key = (r['plan'], r['fund_code'], r['trade_date'], r['action'], r['units'])
        if key not in existing_keys:
            new_records.append(r)

    new_count = len(new_records)
    updated_count = max(0, after_count - before_count - new_count)

    # ===== 3. 汇总 =====
    total_time = time.time() - start_time
    s_count = len([r for r in all_records if r['plan'] == 'S'])
    w150_count = len([r for r in all_records if r['plan'] == '150'])

    print(f'\n{"=" * 60}')
    print(f'结果汇总')
    print(f'{"=" * 60}')
    print(f'  抓取: {len(all_records)} 条 (S:{s_count} 150:{w150_count})')
    print(f'  数据库: {before_count} -> {after_count}')
    print(f'  新增: {new_count}, 更新: {updated_count}')
    print(f'  总耗时: {total_time:.0f}s')

    # ===== 4. 邮件通知 =====
    if new_count > 0:
        stats = {
            's_count': s_count, 'w150_count': w150_count,
            'before': before_count, 'after': after_count,
            'elapsed': total_time,
        }
        send_email(new_records, updated_count, stats)
    else:
        print('  无新增记录，跳过邮件')

    print(f'  完成: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')


if __name__ == '__main__':
    main()

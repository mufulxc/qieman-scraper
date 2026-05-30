# -*- coding: utf-8 -*-
"""
且慢长赢 — 全量发车记录抓取 + Supabase 写入
S计划 + 150计划，所有基金，upsert 到 Supabase
"""
import sys, os, re, time
from datetime import datetime
sys.stdout.reconfigure(encoding='utf-8')

from playwright.sync_api import sync_playwright
from supabase import create_client

# ===== 配置 =====
SUPABASE_URL = os.environ['SUPABASE_URL']
SUPABASE_KEY = os.environ['SUPABASE_KEY']

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
DELAY = 2  # 批次间延迟（秒）


def extract_funds(html, prod):
    """从组合页提取基金列表和分类"""
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
    """从基金详情页提取交易记录和最新净值"""
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
    """抓取单个计划的所有发车记录"""
    cfg = PLANS[plan_key]
    print(f'\n{"=" * 60}')
    print(f'[{cfg["name"]}] {cfg["comp_url"]}')
    print(f'{"=" * 60}')

    # 1. 组合页
    page.goto(cfg['comp_url'], wait_until='networkidle', timeout=30000)
    html = page.content()
    funds = extract_funds(html, cfg['prod'])
    print(f'  基金: {len(funds)} 只')

    # 2. 逐个抓取基金详情
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

        # 批次间暂停
        if (i + 1) % BATCH_SIZE == 0 and i + 1 < len(funds):
            done = i + 1
            print(f'  {done}/{len(funds)} 已抓取, {len(all_records)} 条记录, 暂停 {DELAY}s...')
            time.sleep(DELAY)

    print(f'  完成: {len(funds)} 只基金, {len(all_records)} 条记录')
    return all_records


def main():
    start_time = time.time()
    print(f'且慢长赢 发车记录全量抓取')
    print(f'开始: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    print(f'目标: S计划 + 150计划, 全部基金')

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

    # ===== 2. 写入 Supabase =====
    if not all_records:
        print('无记录，退出')
        return

    print(f'\n写入 Supabase...')
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

    # 先查询已有数量
    before = supabase.table('trade_records').select('id', count='exact').execute()
    before_count = before.count if hasattr(before, 'count') else len(before.data or [])

    # Upsert
    result = supabase.table('trade_records').upsert(
        all_records,
        on_conflict='plan,fund_code,trade_date,action,units',
        ignore_duplicates=False
    ).execute()

    # 查询写入后数量
    after = supabase.table('trade_records').select('id', count='exact').execute()
    after_count = after.count if hasattr(after, 'count') else len(after.data or [])

    new_count = after_count - before_count
    updated_count = after_count - (before_count + new_count)

    # ===== 3. 汇总 =====
    total_time = time.time() - start_time
    print(f'\n{"=" * 60}')
    print(f'结果汇总')
    print(f'{"=" * 60}')
    print(f'  抓取: {len(all_records)} 条')
    print(f'  S计划: {len([r for r in all_records if r["plan"]=="S"])} 条')
    print(f'  150计划: {len([r for r in all_records if r["plan"]=="150"])} 条')
    print(f'  数据库: {before_count} -> {after_count}')
    print(f'  新增: {new_count}, 更新: {updated_count}')
    print(f'  总耗时: {total_time:.0f}s')
    print(f'  完成: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')


if __name__ == '__main__':
    main()

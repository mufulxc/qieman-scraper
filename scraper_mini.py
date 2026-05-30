# -*- coding: utf-8 -*-
"""
且慢长赢 S计划 — 发车记录抓取 (最小测试版)
仅抓取 2 只基金验证 GitHub Actions 可行性
"""
import re, time, sys
from playwright.sync_api import sync_playwright

CAT_ORDER = ['A股', '海外新兴市场股票', '境内债券', '海外债券', '海外成熟市场股票', '原油', '黄金']
MAX_FUNDS = 2  # 只抓前2只基金做测试

def run():
    start = time.time()
    print(f'且慢长赢 发车记录抓取测试')
    print(f'目标: S计划组合页 + {MAX_FUNDS} 只基金详情')
    print()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        # 1. 组合页 — 获取基金列表
        comp_url = 'https://qieman.com/longwin/compositions/LONG_WIN_S'
        print(f'[1/3] 抓取组合页...')
        page.goto(comp_url, wait_until='networkidle', timeout=30000)
        html = page.content()
        print(f'      页面 {len(html)} 字节')

        # 分类定位
        cat_positions = []
        for cat in CAT_ORDER:
            for m in re.finditer(re.escape(cat), html):
                tail = html[m.end():m.end() + 50]
                if '共' in tail or '已清仓' in tail:
                    cat_positions.append((m.start(), cat))
                    break
        cat_positions.sort()

        # 提取基金
        funds = []
        seen = set()
        prod = 'LONG_WIN_S'
        link_pat = rf'<a[^>]*href="/longwin/funds/(\d+)\?prodCode={prod}[^"]*"[^>]*>'
        for m in re.finditer(link_pat, html):
            code = m.group(1)
            if code in seen:
                continue
            seen.add(code)
            tail = html[m.end():m.end() + 600]
            nm = re.search(r'<section>([^<]+)', tail)
            name = nm.group(1).strip() if nm else code
            name = name.replace('&nbsp;', '').strip()
            cat = ''
            for i in range(len(cat_positions) - 1, -1, -1):
                if m.start() > cat_positions[i][0]:
                    cat = cat_positions[i][1]
                    break
            funds.append({'code': code, 'name': name, 'category': cat})

        print(f'      发现 {len(funds)} 只基金，测试前 {min(MAX_FUNDS, len(funds))} 只')
        if funds:
            for f in funds[:MAX_FUNDS]:
                print(f'        - [{f["category"]}] {f["name"]} ({f["code"]})')

        # 2. 抓取前2只基金的详情页
        print(f'\n[2/3] 抓取基金详情页...')
        all_records = []
        for i, fund in enumerate(funds[:MAX_FUNDS]):
            code = fund['code']
            url = f'https://qieman.com/longwin/funds/{code}?prodCode={prod}'
            print(f'      [{i+1}/{MAX_FUNDS}] {fund["name"]} ({code})...')
            page.goto(url, wait_until='networkidle', timeout=30000)
            detail = page.content()

            records = []
            for m in re.finditer(
                r'(\d{4}-\d{2}-\d{2})<br><span[^>]*>交易时净值：<span[^>]*>([\d.]+)</span>',
                detail
            ):
                act_m = re.search(r'(买入|卖出)\s*(\d+)\s*份', detail[m.end():m.end() + 50])
                if act_m:
                    records.append({
                        'date': m.group(1),
                        'nav': float(m.group(2)),
                        'action': act_m.group(1),
                        'units': int(act_m.group(2)) * (-1 if act_m.group(1) == '卖出' else 1),
                    })

            ln_m = re.search(r'最新净值[^0-9]*([\d.]+)', detail)
            latest_nav = float(ln_m.group(1)) if ln_m else 0

            for r in records:
                r['fund_name'] = fund['name']
                r['fund_code'] = code
                r['category'] = fund['category']
                r['latest_nav'] = latest_nav

            all_records.extend(records)
            print(f'            {len(records)} 条记录, 最新净值 {latest_nav}')

        browser.close()

    # 3. 打印结果
    print(f'\n[3/3] 结果汇总')
    print(f'{"=" * 70}')
    print(f'总计: {len(all_records)} 条发车记录')
    if all_records:
        print(f'{"日期":<12} {"基金":<20} {"操作":<6} {"份数":>4} {"交易净值":>8} {"最新净值":>8}')
        print(f'{"-" * 70}')
        for r in sorted(all_records, key=lambda x: x['date']):
            print(f'{r["date"]:<12} {r["fund_name"]:<20} {r["action"]:<6} {r["units"]:>4} {r["nav"]:>8.4f} {r["latest_nav"]:>8.4f}')

    elapsed = time.time() - start
    print(f'\n耗时: {elapsed:.1f}s')
    print('SUCCESS' if all_records else 'FAIL: no records found')
    return len(all_records) > 0


if __name__ == '__main__':
    ok = run()
    sys.exit(0 if ok else 1)

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""今天的行情里，谁符合 v5。

用法：
    python3 scripts/scan_today.py                    # 全主板
    python3 scripts/scan_today.py --codes sz002044,sh600809
    python3 scripts/scan_today.py --no-earnings      # 不拉财报，快一点

输出分三段，只在「除价格外全部条件成立」的标的里分：
    第1段  现价已在买区内        → 理论上今天可以下单
    第2段  还没涨到买区下沿 P    → 等
    第3段  已越过买区上沿 e_max  → 等回落

判定不通过的（资格关、缩量、止损宽、结构缺失）不进这三段，
只在末尾给一个原因分布——因为「差一条」和「可以买」是两回事，
混在一起看会误判。
"""
import argparse
import json
import os
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from v5 import config as C        # noqa: E402
from v5 import data               # noqa: E402
from v5 import scan as SC         # noqa: E402


def load_codes(history='data/history.jsonl'):
    codes, names = [], {}
    if not os.path.exists(history):
        return codes, names
    for row in data.load_history(history):
        c = row.get('c')
        if c:
            codes.append(c)
            names[c] = row.get('n', c)
    return codes, names


def show(title, rows, note=''):
    print('\n' + '=' * 104)
    print('%s  %d 只' % (title, len(rows)))
    print('=' * 104)
    if not rows:
        print('  （无）%s' % note)
        return
    head = ('%-9s %-9s %8s %8s %15s %8s %8s %7s %7s'
            % ('名称', '代码', '现价', '涨跌%', '买区 P~e_max', '止损S', '距买区', '缩量', '止损宽'))
    print(head)
    print('-' * 104)
    for r in rows:
        c = r['conf']
        print('%-9s %-9s %8.2f %7.2f%% %15s %8.2f %7.2f%% %7.3f %6.2f%%'
              % (r['name'][:8], r['code'], r['px'], r['chg'],
                 '%.2f~%.2f' % (c['P'], c['emax']), c['S'],
                 c.get('dist') or 0.0, c.get('pbr') or 0.0,
                 (c.get('sw') or 0) * 100))


def earnings_lines(rows):
    """财报四项。缺字段就写未披露，不填 0。"""
    from concurrent.futures import ThreadPoolExecutor
    codes = [r['code'] for r in rows]
    if not codes:
        return
    with ThreadPoolExecutor(max_workers=6) as ex:
        fins = list(ex.map(lambda c: data.earnings(c[2:]), codes))
    print('\n财报四项（单季，只作补充信息，不参与上面的判定）')
    print('%-9s %10s %10s %12s %12s' % ('名称', '单季营收', '单季归母', '毛利率同比', '经营现金流'))
    print('-' * 68)
    for r, f in zip(rows, fins):
        def pct(v):
            return '%+.2f%%' % v if isinstance(v, (int, float)) else '未披露'
        inc = ('%.2f亿' % f['q_inc']) if 'q_inc' in f else '—'
        npf = ('%.2f亿' % f['q_np']) if 'q_np' in f else '—'
        gm = ('%+.2fpp' % f['q_gm_yoy']) if 'q_gm_yoy' in f else '—'
        cf = ('%.2f亿' % f['cf']) if 'cf' in f else '—'
        tag = '（累计口径）' if f.get('cum_only') else ''
        print('%-9s %10s %10s %12s %12s %s'
              % (r['name'][:8], '%s %s' % (inc, pct(f.get('q_inc_yoy'))),
                 '%s %s' % (npf, pct(f.get('q_np_yoy'))), gm, cf, tag))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--codes', default='', help='逗号分隔，如 sz002044,sh600809')
    ap.add_argument('--history', default='data/history.jsonl')
    ap.add_argument('--json', default='data/scan.json')
    ap.add_argument('--workers', type=int, default=6)
    ap.add_argument('--earnings', dest='earnings', action='store_true', default=True)
    ap.add_argument('--no-earnings', dest='earnings', action='store_false')
    args = ap.parse_args()

    t0 = time.time()
    if args.codes:
        codes = [c.strip() for c in args.codes.split(',') if c.strip()]
    else:
        codes, _ = load_codes(args.history)
        if not codes:
            print('找不到股票池。先跑 scripts/fetch_history.py，'
                  '或用 --codes 直接指定。')
            return 2

    print('拉实时快照...')
    qs = data.quote(codes)
    src = data.detect_source()
    print('快照 %d/%d    K线源 %s' % (len(qs), len(codes), src))

    rows, failed = SC.scan(codes, workers=args.workers, quotes=qs, progress=True)
    f = SC.funnel(len(codes), rows)
    seg, _ = SC.bucket(rows)

    # 结构基准直接取自实际参与判定的那根K（conf['base']），不靠时间猜：
    # 盘中剔除当日未完成K用昨日，收盘后用今日，非交易日仍是上一个交易日。
    basis = '今日收盘'
    if rows:
        base_date = (rows[0]['conf'].get('base') or '')[:10]
        if base_date and base_date != time.strftime('%Y-%m-%d'):
            basis = '昨日收盘（%s）' % base_date
    print('\n口径：A 股主板 | 前复权日线 | 结构=%s确认，价格=%s 实时'
          % (basis, time.strftime('%H:%M')))
    if failed:
        print('失败 %d 只：%s' % (len(failed), failed[:5]))
    print('\n漏斗：全市场 %d → 有结构 %d → 除价格外全过 %d → 价格在买区内 %d'
          % (f['universe'], f['structured'], f['all_ok'], f['inside']))

    show('第1段 · 现价已在买区内（理论可下单）', seg['inside'])
    show('第2段 · 除价格外全过，等待涨到买区下沿', seg['below'])
    show('第3段 · 除价格外全过，已越过买区上沿', seg['above'])

    reasons = Counter()
    for r in seg['other']:
        c = r['conf']
        if c['gate']:
            reasons['卡资格关：' + c['gate'][0]] += 1
        elif c.get('state') == 'no_stop':
            reasons['无合法止损'] += 1
        elif c.get('state') == 'no_zone':
            reasons['买区倒挂'] += 1
        elif c.get('state') == 'no_node':
            reasons['无止跌K'] += 1
        elif not c.get('pbr_ok'):
            reasons['缩量不够'] += 1
        elif not c.get('sw_ok'):
            reasons['止损宽超限'] += 1
    print('\n其余 %d 只的落选原因（只列前 8 项）：' % len(seg['other']))
    for k, v in reasons.most_common(8):
        print('  %-22s %d' % (k, v))

    if args.earnings:
        target = seg['inside'] + seg['below'] + seg['above']
        if target:
            earnings_lines(target)
        else:
            print('\n今天没有除价格外全过的标的，不拉财报。')

    os.makedirs(os.path.dirname(args.json) or '.', exist_ok=True)
    json.dump(dict(funnel=f, rows=rows), open(args.json, 'w', encoding='utf-8'),
              ensure_ascii=False, default=str)
    print('\n耗时 %.0fs   明细写入 %s' % (time.time() - t0, args.json))
    return 0


if __name__ == '__main__':
    sys.exit(main())

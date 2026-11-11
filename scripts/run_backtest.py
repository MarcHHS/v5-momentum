#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回测：读 data/history.jsonl，跑出全部信号和交易明细。

用法：
    python3 scripts/run_backtest.py
    python3 scripts/run_backtest.py --start 2024-01-01
    python3 scripts/run_backtest.py --csv data/trades.csv

先说清楚这个结果是什么、不是什么：
  · 是「单笔等权平均收益」，不是账户净值曲线。
    真要算净值还要定资金分配、佣金、印花税、滑点，那是另一层的事。
  · 复权口径是前复权，分红除权已在价格里处理。
  · 不含涨停买不进、跌停卖不出的情况。
"""
import argparse
import csv
import json
import os
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from v5 import backtest as BT     # noqa: E402
from v5 import config as C        # noqa: E402
from v5 import data               # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--history', default='data/history.jsonl')
    ap.add_argument('--start', default=C.BACKTEST_START)
    ap.add_argument('--benchmark', default='data/hs300.json',
                    help='可选，沪深300 日线；配合 --market-gate 使用')
    ap.add_argument('--market-gate', action='store_true',
                    help='大盘跌破 MA20 时否决信号（v5 默认关闭）')
    ap.add_argument('--min-mcap', type=float, default=None)
    ap.add_argument('--atr-max', type=float, default=None)
    ap.add_argument('--csv', default='data/trades.csv')
    ap.add_argument('--json', default='data/summary.json')
    ap.add_argument('--limit', type=int, default=0, help='只跑前 N 只，调试用')
    args = ap.parse_args()

    if not os.path.exists(args.history):
        print('找不到 %s，先跑 scripts/fetch_history.py' % args.history)
        return 2

    bench = None
    if args.market_gate:
        if not os.path.exists(args.benchmark):
            print('开了 --market-gate 但缺少 %s' % args.benchmark)
            return 2
        bench = data.load_benchmark(args.benchmark)

    params = dict(market_gate=args.market_gate,
                  min_mcap=args.min_mcap, atr_max=args.atr_max)

    t0 = time.time()
    stocks, sig_total, all_trades = 0, 0, []
    for row in data.load_history(args.history):
        if args.limit and stocks >= args.limit:
            break
        ks = [[x[0], x[1], x[2], x[3], x[4], x[5], x[6]] for x in row['k']]
        if len(ks) < C.KLINE_MIN_BARS:
            continue
        B = data.prep(row['c'], row.get('n', row['c']), ks,
                      row.get('m', 0.0), row.get('p', 0.0))
        if B is None:
            continue
        stocks += 1
        sigs = BT.build_signals(B, args.start, params)
        sig_total += len(sigs)
        if sigs:
            all_trades += BT.simulate(B, sigs, bench, params)
        if stocks % 300 == 0:
            print('  %d 只  信号 %d  成交 %d  %.0fs'
                  % (stocks, sig_total, len(all_trades), time.time() - t0), flush=True)

    print('\n股票 %d 只  信号 %d 个  成交 %d 笔  %.0fs'
          % (stocks, sig_total, len(all_trades), time.time() - t0))

    if not all_trades:
        print('没有成交。检查数据区间或参数。')
        return 1

    s = BT.summarize(all_trades)
    print('\n' + '=' * 62)
    print('成交 %d 笔   笔均 %+.2f%%   中位 %+.2f%%   胜率 %.0f%%'
          % (s['trades'], s['avg_pct'] * 100, s['median_pct'] * 100, s['win_rate'] * 100))
    print('平均 %.1f R   平均持有 %.1f 天   盈亏比 %.2f'
          % (s['avg_r'], s['avg_hold'], s['profit_factor']))
    print('最好 %+.1f%%   最差 %+.1f%%'
          % (s['best'] * 100, s['worst'] * 100))

    by_year = defaultdict(list)
    by_reason = defaultdict(list)
    for t in all_trades:
        by_year[t['date_in'][:4]].append(t['pct'])
        by_reason[t['reason']].append(t['pct'])
    print('\n分年：')
    for y in sorted(by_year):
        v = by_year[y]
        win = sum(1 for x in v if x > 0) / len(v)
        print('  %s  %4d 笔  笔均 %+.2f%%  胜率 %.0f%%'
              % (y, len(v), sum(v) / len(v) * 100, win * 100))
    print('\n离场原因：')
    for r, v in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
        print('  %-6s %4d 笔  笔均 %+.2f%%' % (r, len(v), sum(v) / len(v) * 100))
    print('=' * 62)

    out_csv = args.csv
    os.makedirs(os.path.dirname(out_csv) or '.', exist_ok=True)
    with open(out_csv, 'w', newline='', encoding='utf-8') as fh:
        w = csv.DictWriter(fh, fieldnames=list(all_trades[0].keys()))
        w.writeheader()
        w.writerows(all_trades)
    print('明细写入 %s' % out_csv)
    json.dump(dict(stocks=stocks, signals=sig_total, summary=s,
                   by_year={k: len(v) for k, v in by_year.items()},
                   by_reason={k: len(v) for k, v in by_reason.items()}),
              open(args.json, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    return 0


if __name__ == '__main__':
    sys.exit(main())

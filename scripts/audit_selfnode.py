#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""审计：量化「止跌K当日自证」这个 bug 的影响面。

背景见 README 第七节。旧实现把 j == t 也当成候选，导致
range(t+1, t+1) 为空、all([]) 恒真，当日K自己证明自己没破底。

这个脚本用同一份代码跑两种口径，把差异摆出来，方便复现和复核。

用法：
    python3 scripts/audit_selfnode.py --limit 400
    python3 scripts/audit_selfnode.py --history data/history.jsonl
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from v5 import config as C        # noqa: E402
from v5 import data               # noqa: E402
from v5 import signal as SIG      # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--history', default='data/history.jsonl')
    ap.add_argument('--limit', type=int, default=400)
    ap.add_argument('--start', default=C.BACKTEST_START)
    args = ap.parse_args()

    if not os.path.exists(args.history):
        print('找不到 %s，先跑 scripts/fetch_history.py' % args.history)
        return 2

    n_stock = 0
    tot = dict(points=0, legacy_self=0, strict_ok=0, legacy_ok=0,
               only_legacy=0, p_changed=0, gone=0)
    t0 = time.time()

    for row in data.load_history(args.history):
        if args.limit and n_stock >= args.limit:
            break
        ks = [[x[0], x[1], x[2], x[3], x[4], x[5], x[6]] for x in row['k']]
        if len(ks) < C.KLINE_MIN_BARS:
            continue
        B = data.prep(row['c'], row.get('n', row['c']), ks,
                      row.get('m', 0.0), row.get('p', 0.0))
        if B is None:
            continue
        n_stock += 1
        for t in range(80, len(B['d']) - 1):
            if B['d'][t] < args.start:
                continue
            tot['points'] += 1
            node_l = SIG.find_node(B, t, allow_self_node=True)
            if node_l and node_l[0] == t:
                tot['legacy_self'] += 1
            node_s = SIG.find_node(B, t, allow_self_node=False)

            # 只有"两种口径下结构都成立且买区有效"的时点才值得比 P
            rl = SIG.analyse_at(B, t, B['mcap_now'], B['c'][t], allow_self_node=True)
            rs = SIG.analyse_at(B, t, B['mcap_now'], B['c'][t], allow_self_node=False)
            if rl['state'] == 'ok':
                tot['legacy_ok'] += 1
            if rs['state'] == 'ok':
                tot['strict_ok'] += 1
            if rl['state'] == 'ok' and rs['state'] != 'ok':
                tot['only_legacy'] += 1
            elif rl['state'] == 'ok' and rs['state'] == 'ok':
                if abs(rl['P'] - rs['P']) > 1e-9:
                    tot['p_changed'] += 1
    if tot['legacy_self'] and not tot['p_changed'] and not tot['only_legacy']:
        pass

    def pct(a, b):
        return '%.1f%%' % (100.0 * a / b) if b else '—'

    print('样本 %d 只  判定时点 %d  %.0fs' % (n_stock, tot['points'], time.time() - t0))
    print()
    print('止跌K落在当日的时点（旧实现会采纳）   %6d  %s'
          % (tot['legacy_self'], pct(tot['legacy_self'], tot['points'])))
    print('买区成立：旧口径                     %6d  %s'
          % (tot['legacy_ok'], pct(tot['legacy_ok'], tot['points'])))
    print('买区成立：修复后                     %6d  %s'
          % (tot['strict_ok'], pct(tot['strict_ok'], tot['points'])))
    print()
    print('只有旧口径才成立的（虚假信号）        %6d  %s'
          % (tot['only_legacy'], pct(tot['only_legacy'], tot['legacy_ok'])))
    print('两口径都成立但 P 变了（买区整体平移） %6d  %s'
          % (tot['p_changed'], pct(tot['p_changed'], tot['strict_ok'])))
    return 0


if __name__ == '__main__':
    sys.exit(main())

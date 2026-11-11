#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""拉取全主板历史日K，落成 data/history.jsonl。

这个脚本不在仓库里带数据——数据太大（全主板约 300MB），GitHub 装不下。
所以所有使用者都要自己跑一次。

用法：
    python3 scripts/fetch_history.py                # 全主板，约 3000 只
    python3 scripts/fetch_history.py --limit 30     # 只拉 30 只，先试试
    python3 scripts/fetch_history.py --bars 500     # 多要一点历史

输出格式（每行一只股票）：
    {"c": "sh600000", "n": "浦发银行", "m": 3417.0, "p": 10.26,
     "k": [["2025-03-19", 开, 收, 高, 低, 成交量(股), 成交额(元)], ...]}

注意 m 和 p 是「抓取当时」的市值与价格，仅用于回测里反推股本。
历史价格全部来自前复权日K，与这两个数无关。
"""
import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from v5 import config as C          # noqa: E402
from v5 import data                  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--bars', type=int, default=C.KLINE_BARS)
    ap.add_argument('--limit', type=int, default=0, help='只拉前 N 只，调试用')
    ap.add_argument('--workers', type=int, default=8)
    ap.add_argument('--out', default='data/history.jsonl')
    args = ap.parse_args()

    os.makedirs(os.path.dirname(args.out) or '.', exist_ok=True)
    t0 = time.time()
    print('拉股票列表...')
    uni = data.list_universe(args.limit or None)
    print('主板股票 %d 只 | %.1fs' % (len(uni), time.time() - t0))
    if not uni:
        print('列表拉取失败，检查网络或接口是否变动。')
        return 2

    done, fail, skip = [0], [], []
    fp_tmp = args.out + '.tmp'
    fh = open(fp_tmp, 'w', encoding='utf-8')

    def work(item):
        code, name, mcap, px = item
        try:
            ks, src, adjusted = data.kline(code, args.bars, drop_today=True)
        except Exception as e:
            fail.append((code, '拉取异常:%s' % e))
            return None
        done[0] += 1
        if done[0] % 100 == 0:
            print('  %d/%d  %.0fs' % (done[0], len(uni), time.time() - t0), flush=True)
        if len(ks) < C.KLINE_MIN_BARS:
            skip.append(code)
            return None
        if not adjusted:
            fail.append((code, '仅剩不复权源'))
        return dict(c=code, n=name, m=mcap, p=px,
                    k=[[x[0], round(x[1], 4), round(x[2], 4), round(x[3], 4),
                        round(x[4], 4), int(x[5]), int(x[6])] for x in ks])

    try:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            for rec in ex.map(work, uni):
                if rec:
                    fh.write(json.dumps(rec, ensure_ascii=False) + '\n')
    finally:
        fh.close()

    os.replace(fp_tmp, args.out)
    size = os.path.getsize(args.out) / 1e6
    print('\n完成 %d 只  跳过(数据不足) %d  失败 %d  |  %.1fMB  %.0fs'
          % (len(uni) - len(fail) - len(skip), len(skip), len(fail), size, time.time() - t0))
    if fail:
        print('失败样例：', fail[:10])
    print('写入 %s' % args.out)
    return 0


if __name__ == '__main__':
    sys.exit(main())

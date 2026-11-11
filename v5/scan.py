# -*- coding: utf-8 -*-
"""实盘扫描：把 signal 的口径套到今天的行情上。

一个容易搞错的地方，单独说清楚：

    结构用「昨日收盘」确认，价格用「实时快照」。

盘中今天的K线还没走完，拿它做结构判定等于用半根K确认止跌K，
信号会在盘中反复出现又消失。所以：

    盘中（09:30–15:00）  t = 倒数第二根（昨天）
    收盘后               t = 最后一根（今天）

「距买区 %」永远用实时价算，正数=还要涨才到 P，负数=已越过上沿。
"""
import time
from concurrent.futures import ThreadPoolExecutor

from . import config as C
from . import data
from . import signal as SIG


def is_market_open(now=None):
    hm = (now or time.strftime('%H%M'))
    return '0930' <= hm < '1500'


def analyse_one(code, q, use_cache=True):
    """单只股票的完整判定。返回 None 表示数据不足。"""
    ks, src, adjusted = data.kline_cached(code, use_cache=use_cache)
    if len(ks) < C.KLINE_MIN_BARS:
        return None
    ks2 = data.merge_today(ks, q)
    B = data.prep(code, q['name'], ks2, q.get('mcap', 0.0), q.get('px', 0.0))
    if B is None:
        return None
    intraday = is_market_open() and ks2[-1][0] == time.strftime('%Y-%m-%d')
    t = len(ks2) - 2 if intraday else len(ks2) - 1
    if t < 80:
        return None
    res = SIG.analyse_at(B, t, q.get('mcap', 0.0), q.get('px', 0.0))
    # 买区有效时才有位置和距离；倒挂/无止损时保持 None，不制造假距离
    pos, dist = SIG.position(q['px'], res.get('P'), res.get('emax'))
    res['pos'], res['dist'] = pos, dist
    res['verdict'] = SIG.verdict(res, q['px'])
    return dict(
        code=code, name=q['name'], px=q['px'], chg=q.get('chg', 0.0),
        mcap=q.get('mcap', 0.0), lb=q.get('lb', 0.0), avg=q.get('avg', 0.0),
        amt_today=q.get('amt', 0.0),
        intraday=intraday, source=src, adjusted=adjusted,
        conf=res,
    )


def scan(codes, workers=6, quotes=None, progress=False, use_cache=True):
    """批量扫描。返回 (rows, failed)。"""
    codes = list(dict.fromkeys(codes))          # 去重但保序
    qs = quotes if quotes is not None else data.quote(codes)
    rows, failed, done = [], [], [0]
    t0 = time.time()

    def work(c):
        q = qs.get(c)
        done[0] += 1
        if progress and done[0] % 200 == 0:
            print('  %d/%d  %.0fs' % (done[0], len(codes), time.time() - t0), flush=True)
        if not q:
            failed.append((c, '无实时快照'))
            return None
        try:
            return analyse_one(c, q, use_cache=use_cache)
        except Exception as e:
            failed.append((c, str(e)))
            return None

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for r in ex.map(work, codes):
            if r:
                rows.append(r)
    return rows, failed


def bucket(rows):
    """按「能不能买」分三段。

    inside  价格已在买区内，且其它条件全过  → 当天可下单
    below   除价格外全过，还需上涨到 P      → 等
    above   除价格外全过，已涨过上沿        → 等回落
    """
    ok = [r for r in rows if r['conf'].get('all_ok')]
    out = {'inside': [], 'below': [], 'above': [], 'other': []}
    for r in rows:
        if not r['conf'].get('all_ok'):
            out['other'].append(r)
            continue
        pos = r['conf'].get('pos')
        if pos is None:
            P, e = r['conf'].get('P'), r['conf'].get('emax')
            pos, _ = SIG.position(r['px'], P, e)
        out[pos or 'other'].append(r)
    for key in ('below', 'above'):
        out[key].sort(key=lambda x: abs(x['conf'].get('dist') or 0))
    return out, ok


def funnel(total, rows):
    """漏斗统计：全市场 → 有结构 → 除价格外全过 → 价格在区内。"""
    def has_state(r, *names):
        return r['conf'].get('state') in names
    return dict(
        universe=total,
        scanned=len(rows),
        structured=sum(1 for r in rows if r['conf'].get('state') == 'ok'),
        all_ok=sum(1 for r in rows if r['conf'].get('all_ok')),
        inside=sum(1 for r in rows if r['conf'].get('all_ok') and r['conf'].get('pos') == 'inside'),
        no_node=sum(1 for r in rows if has_state(r, 'no_node')),
        no_stop=sum(1 for r in rows if has_state(r, 'no_stop')),
        no_zone=sum(1 for r in rows if has_state(r, 'no_zone')),
    )

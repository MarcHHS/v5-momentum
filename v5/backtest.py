# -*- coding: utf-8 -*-
"""回测引擎。

执行假设（全部写在明面上，别在别处偷偷改）：

  信号确认   用第 t 日收盘数据算出信号
  建仓       第 t+1 日。开盘价 ≥ P 且当日最低价 ≤ e_max 才算成交；
             成交价 = min(开盘价, e_max)。开盘价 < P 视为没突破，当天放弃。
  止损       固定 S。盘中触及即按 S 成交（跳空低开则按开盘价）。
  移动止损   吊灯 max(持仓期最高价 − K×ATR14_昨日, S)，只上移不下移。
  止盈       v5 不设。名义目标 T 只用于反解买区上沿，不参与出场。
  持有上限   无。信号消失不卖，只有止损或吊灯触发才卖。
  冷却       平仓后 COOLDOWN_DAYS 天内同一只不再开仓。

已知的近似：
  · 同日既触止损又触吊灯时按止损优先（保守）
  · 涨停开盘无法买入的情况没有建模
  · 佣金/印花税不在本模块计算，由上层账户模拟处理
"""
from . import config as C
from . import signal as SIG


DEFAULT = dict(
    swmax=C.STOP_WIDTH_MAX,
    chandelier=C.CHANDELIER_K,
    market_gate=C.MARKET_GATE,
    min_mcap=None,
    atr_max=None,
    cooldown=C.COOLDOWN_DAYS,
)


def build_signals(B, start_date=C.BACKTEST_START, params=None):
    """逐日扫描出一个股票的全部有效信号。耗时集中在这里。"""
    p = dict(DEFAULT, **(params or {}))
    n = len(B['d'])
    out = {}
    for t in range(80, n - 1):
        if B['d'][t] < start_date:
            continue
        # 时点市值：用信号日收盘价 × 股本，避免用今天的市值回测过去
        mcap = B['shares'] * B['c'][t] / 1e8 if B['shares'] else B['mcap_now']
        if p['min_mcap'] and mcap < p['min_mcap']:
            continue
        if p['atr_max'] and B['c'][t] > 0 and B['atr'][t] / B['c'][t] > p['atr_max']:
            continue
        r = SIG.analyse_at(B, t, mcap, B['c'][t])
        if r['state'] != 'ok' or not r['all_ok']:
            continue
        out[t] = r
    return out


def simulate(B, signals, bench=None, params=None):
    """按执行假设把信号变成交易明细。"""
    p = dict(DEFAULT, **(params or {}))
    d, o, h, l, c, atr = B['d'], B['o'], B['h'], B['l'], B['c'], B['atr']
    n = len(d)
    k = p['chandelier']
    trades = []
    free_from = 0

    for t in sorted(signals):
        s = signals[t]
        e = t + 1
        if e >= n or e < free_from:
            continue
        if p['market_gate'] and bench:
            dt = d[t]
            if dt in bench and bench[dt][0] < bench[dt][1]:
                continue
        P, emax, S = s['P'], s['emax'], s['S']
        if not (o[e] >= P and l[e] <= emax):
            continue
        entry = min(o[e], emax)
        if entry <= S:
            continue
        risk = entry - S
        if risk <= 0 or risk / entry > p['swmax']:
            continue

        peak = entry
        exit_px = None
        reason = None
        i = e
        while i < n:
            oi, hi, li = o[i], h[i], l[i]
            trail = max(peak - k * atr[i - 1], S) if i > 0 else S
            if i == e:
                if li <= S:
                    exit_px, reason = (oi if oi <= S else S), '止损'
            else:
                if oi <= S:
                    exit_px, reason = oi, '止损'
                elif oi <= trail:
                    exit_px, reason = oi, '吊灯'
                elif li <= S:
                    exit_px, reason = S, '止损'
                elif li <= trail:
                    exit_px, reason = trail, '吊灯'
            if exit_px is not None:
                break
            if hi > peak:
                peak = hi
            i += 1

        if exit_px is None:                 # 到数据末尾仍未触发
            i = n - 1
            exit_px, reason = c[i], '数据结束'

        trades.append(dict(
            code=B['code'], name=B['name'],
            date_in=d[e], date_out=d[i],
            entry=entry, exit=exit_px, stop=S, target=s['T'],
            P=P, emax=emax,
            pct=exit_px / entry - 1,
            r=(exit_px - entry) / risk,
            hold=i - e,
            reason=reason,
            atr_pct=s.get('atrp', 0.0),
            pbr=s.get('pbr'),
        ))
        free_from = i + 1 + int(p['cooldown'])
    return trades


def summarize(trades):
    """基础统计。收益率是单笔等权平均，不是账户净值曲线。"""
    n = len(trades)
    if not n:
        return dict(trades=0)
    pcts = [x['pct'] for x in trades]
    wins = [x for x in pcts if x > 0]
    gross_win = sum(wins)
    gross_loss = -sum(x for x in pcts if x <= 0)
    return dict(
        trades=n,
        avg_pct=sum(pcts) / n,
        median_pct=sorted(pcts)[n // 2],
        win_rate=len(wins) / n,
        avg_r=sum(x['r'] for x in trades) / n,
        avg_hold=sum(x['hold'] for x in trades) / n,
        profit_factor=(gross_win / gross_loss) if gross_loss > 0 else float('inf'),
        best=max(pcts), worst=min(pcts),
    )

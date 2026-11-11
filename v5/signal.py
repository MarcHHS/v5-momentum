# -*- coding: utf-8 -*-
"""v5 信号计算：资格关卡 → 止跌K → 止损 S → 买区 [P, e_max] → 目标 T。

这是整套体系唯一的"定义"所在。回测和实盘扫描都调用这里，
不允许任何地方复制粘贴一份改过的版本，否则两边口径会悄悄分叉。
"""
from . import config as C
from . import indicators as I


# ---------------------------------------------------------------- 结构

def find_node(B, t, allow_self_node=False):
    """在 t 及之前 NODE_LOOKBACK 根内找最近的有效止跌K。

    有效 = 形态成立 + 它之后所有收盘都没跌破它的最低价。

    返回 (索引 j, 形态名) 或 None。

    allow_self_node=False（默认，也是唯一正确的口径）：
        止跌K必须是"已经过去"的 K 线，它之后至少有一根K线来验证没破底。
        少了这一条，当日K会自己证明自己是止跌K——因为 range(t+1, t+1)
        是空循环，all([]) 恒为 True。

    allow_self_node=True：
        仅用于 scripts/audit_selfnode.py 复现旧实现、量化这个 bug 的影响。
        策略代码里不要传 True。
    """
    o, h, l, c, v = B['o'], B['h'], B['l'], B['c'], B['v']
    lo = max(t - C.NODE_LOOKBACK, 0)
    start = t if allow_self_node else t - 1
    for j in range(start, lo - 1, -1):
        kind = None
        if I.is_hammer(o[j], h[j], l[j], c[j]):
            kind = '锤子'
        elif j >= 1 and I.is_engulf(o[j], h[j], l[j], c[j],
                                    o[j - 1], h[j - 1], l[j - 1], c[j - 1]):
            kind = '吞没'
        elif j >= 2 and I.is_shrink(o[j], h[j], l[j], c[j], o[j - 1], c[j - 1],
                                    o[j - 2], c[j - 2], v[j - 1], v[j - 2]):
            kind = '缩量阳'
        if kind is None:
            continue
        if all(c[k] >= l[j] for k in range(j + 1, t + 1)):
            return j, kind
    return None


def measure(B, t, j):
    """由止跌K索引 j 推出 P / T / 缩量比。

    P  = 止跌K的最高价（结构确认位）
    Hh = 止跌K之前 RALLY_LOOKBACK 根内的最高价
    Ll = Hh 所在那根往前 RALLY_LOW_LOOKBACK 根的最低价（起涨点）
    T  = min(Hh, P + (P - Ll))   —— 取"前高"与"等幅投影"中较近的一个
    """
    h, l, v = B['h'], B['l'], B['v']
    P = h[j]
    up = list(range(max(0, j - C.RALLY_LOOKBACK), j))
    if not up:
        return None
    Hh = max(h[k] for k in up)
    hi = max(k for k in up if h[k] == Hh)
    Ll = min(l[k] for k in range(max(0, hi - C.RALLY_LOW_LOOKBACK), hi + 1))
    T = min(Hh, P + (P - Ll))

    # 缩量比：回调期均量 ÷ 上涨末段（含前高当日）均量
    pb = list(range(hi + 1, j + 1))
    pbr = None
    if pb:
        base_lo = max(0, hi - C.RALLY_LOW_LOOKBACK)
        den = sum(v[k] for k in range(base_lo, hi + 1)) / (hi - base_lo + 1)
        pbr = (sum(v[k] for k in pb) / len(pb)) / den if den else None
    return dict(P=P, T=T, A=B['atr'][t], pbr=pbr, j=j, date=B['d'][j])


# ---------------------------------------------------------------- 止损

def stop_candidates(B, t):
    """五个候选支撑位，取其中最高的合法者作为止损。"""
    l = B['l']
    return [
        min(l[max(0, t - 4):t + 1]),      # 5 日低
        min(l[max(0, t - 9):t + 1]),      # 10 日低
        min(l[max(0, t - 19):t + 1]),     # 20 日低
        B['boll'][t],                     # 布林下轨
        B['ma21'][t],                     # MA21
    ]


def pick_stop(cands, P, A):
    """挑出合法止损里最高的那个。

    合法 = 严格低于 P，且距离在 [STOP_MIN_ATR, STOP_MAX_ATR] × ATR 之间，
    同时不超过 P 的 STOP_MAX_PCT。
    """
    legal = [p for p in cands
             if p < P
             and (P - p) >= C.STOP_MIN_ATR * A
             and (P - p) <= C.STOP_MAX_ATR * A
             and (P - p) <= C.STOP_MAX_PCT * P]
    return max(legal) if legal else None


def zone(S, T, P):
    """买区上沿 e_max。

    (T + 2S) / 3 就是盈亏比恰好为 2 的那个价位：
        R = (T - e) / (e - S) = 2  =>  e = (T + 2S) / 3
    再叠加一道"不得高于 P 的 ZONE_CAP 倍"的追高刹车。
    """
    e = (T + 2 * S) / 3.0
    if C.ZONE_USE_CAP:
        e = min(e, P * C.ZONE_CAP)
    return e


def shrink_grade(pbr):
    """A=标准合格 / B=边界合格 / X=不合格。"""
    if pbr is None:
        return 'X'
    if pbr <= C.SHRINK_A + 1e-9:
        return 'A'
    if pbr <= C.SHRINK_B + 1e-9:
        return 'B'
    return 'X'


# ---------------------------------------------------------------- 资格关

def amt_need(mcap):
    """按市值给出 20 日均额门槛（亿元）。"""
    for cap, need in C.AMT_TIERS:
        if mcap <= cap:
            return need
    return C.AMT_TIERS[-1][1]


def check_gates(B, t, mcap):
    """五道资格关。返回不满足项的文字列表，空列表=全过。"""
    g = []
    if not (B['c'][t] > B['e50'][t]):
        g.append('收盘<EMA50')
    if not (B['e50'][t] > B['e50'][t - 5]):
        g.append('EMA50下行')
    if not (B['e21'][t] > B['e21'][t - 5]):
        g.append('EMA21下行')
    if not (B['e9'][t] > B['e21'][t]):
        g.append('EMA9<EMA21')
    need = amt_need(mcap)
    amt = B['amt20'][t] / 1e8
    if amt < need:
        g.append('均额%.1f<%.0f' % (amt, need))
    return g


# ---------------------------------------------------------------- 汇总

def analyse_at(B, t, mcap, price, allow_self_node=False):
    """对单个时点做完整判定，返回一个 dict。

    state 取值：
      ok        结构完整，买区成立
      no_node   最近 20 根里没有有效止跌K
      no_stop   有止跌K但五个候选位都凑不出合法止损
      no_zone   买区倒挂（e_max < P），意味着盈亏比到不了 2

    allow_self_node 只给审计脚本用，策略代码永远用默认值。
    """
    A = B['atr'][t]
    gates = check_gates(B, t, mcap)
    res = dict(
        base=B['d'][t], gate=gates, gate_ok=not gates,
        amt=B['amt20'][t] / 1e8,
        atr=A, atrp=(A / price * 100) if price else 0.0,
        e9=B['e9'][t], e21=B['e21'][t], e50=B['e50'][t],
    )
    node = find_node(B, t, allow_self_node)
    if node is None:
        res['state'] = 'no_node'
        return res
    j, kind = node
    m = measure(B, t, j)
    if m is None:
        res['state'] = 'no_node'
        return res
    P, T, pbr = m['P'], m['T'], m['pbr']
    res.update(P=P, T=T, pbr=pbr, kind=kind, pdate=m['date'],
               shrink=shrink_grade(pbr), pbr_ok=(shrink_grade(pbr) in ('A', 'B')))

    S = pick_stop(stop_candidates(B, t), P, A)
    if S is None:
        res['state'] = 'no_stop'
        return res
    sw = (P - S) / P
    e = zone(S, T, P)
    res.update(S=S, sw=sw, sw_ok=(sw <= C.STOP_WIDTH_MAX), emax=e)
    if e < P - 1e-9:
        res['state'] = 'no_zone'
        return res
    res.update(state='ok', R=(T - e) / (e - S) if e > S else 0.0)
    res['all_ok'] = res['gate_ok'] and res['pbr_ok'] and res['sw_ok']
    return res


def position(px, P, emax):
    """现价相对买区的位置。

    买区倒挂（emax < P）时没有可执行区间，返回 (None, None)——
    不能拿倒挂的上下沿去算一个看起来正常的距离。
    """
    if P is None or emax is None or emax < P - 1e-9:
        return None, None
    dist = (P - px) / px * 100        # 正数=还要涨到 P；负数=已越过上沿
    if px >= P and px <= emax:
        return 'inside', dist
    if px < P:
        return 'below', dist
    return 'above', dist


def verdict(res, px):
    """一句人话结论。只描述结构，不含任何执行建议。"""
    st = res.get('state')
    if st == 'no_node':
        return '无止跌K'
    if st == 'no_stop':
        return '无合法止损'
    if st == 'no_zone':
        return '买区倒挂'
    if res['gate']:
        return '卡' + res['gate'][0]
    pos, dist = position(px, res['P'], res['emax'])
    if pos == 'inside':
        return '已在买区内'
    if pos == 'below':
        return '等涨%.2f%%' % dist
    return '已涨过%.2f%%' % (-dist)

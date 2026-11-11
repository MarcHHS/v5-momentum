# -*- coding: utf-8 -*-
"""技术指标与 K 线形态。

全部手写、不依赖 numpy/pandas，方便逐行核对。
每个函数都吃 list[float]、吐 list[float]，长度与输入一致，
前面不足周期的部分用初始值填充，不做 None 对齐——这是为了让
下游可以无条件按索引取用。
"""
import math


def ema(xs, n):
    """指数移动平均。首个值直接取 xs[0] 作为种子（无头部空窗）。"""
    k = 2.0 / (n + 1)
    out, prev = [], None
    for x in xs:
        prev = x if prev is None else x * k + prev * (1 - k)
        out.append(prev)
    return out


def sma(xs, n):
    """简单移动平均。不足 n 个时按已有数量取均值。"""
    out, buf, s = [], [], 0.0
    for x in xs:
        buf.append(x)
        s += x
        if len(buf) > n:
            s -= buf.pop(0)
        out.append(s / len(buf))
    return out


def atr14(h, l, c, n=14):
    """ATR（Wilder 递推）。返回与输入等长的列表，前 n 个填种子值。"""
    if not c:
        return []
    tr = [h[0] - l[0]]
    for i in range(1, len(c)):
        tr.append(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))
    out = [0.0] * len(c)
    if len(c) <= n:
        return out
    seed = sum(tr[1:n + 1]) / n
    for i in range(n):
        out[i] = seed
    prev = seed
    for i in range(n, len(c)):
        prev = (prev * (n - 1) + tr[i]) / n
        out[i] = prev
    return out


def boll_low(c, n=20, k=2.0):
    """布林带下轨。用滚动平方和计算方差，避免重复遍历。"""
    out = [0.0] * len(c)
    s = ss = 0.0
    for i, x in enumerate(c):
        s += x
        ss += x * x
        if i >= n:
            y = c[i - n]
            s -= y
            ss -= y * y
        m = s / min(i + 1, n)
        var = max(0.0, ss / min(i + 1, n) - m * m)
        out[i] = m - k * math.sqrt(var)
    return out


# ---------------- 止跌K 的三种形态 ----------------
# 只判断单根（或两三根）K 的形状，不判断位置。
# 「这根K之后有没有再破底」由 signal.py 负责，两者分开才好单独测试。

def is_hammer(o, h, l, c):
    """锤子线：下影线至少是实体的两倍，上影线不超过实体。"""
    body = abs(c - o)
    lower = min(o, c) - l
    upper = h - max(o, c)
    if body < 1e-9:
        return lower > 0
    return lower >= 2 * body and upper <= body


def is_engulf(o, h, l, c, po, ph, pl, pc):
    """看涨吞没：今日阳线实体完全包住昨日阴线实体。"""
    return c > o and pc < po and o <= pc and c >= po


def is_shrink(o, h, l, c, o1, c1, o2, c2, v1, v2):
    """缩量阳：连续两根阴线且第二根缩量，今日收阳，量能继续萎缩。

    o1/c1/v1 是昨日，o2/c2/v2 是前日。
    """
    return c2 < o2 and c1 < o1 and v1 < v2 and c > o

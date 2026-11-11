# -*- coding: utf-8 -*-
"""数据层：日K、实时快照、财报。

============ 数据口径（改代码前请先读这段） ============

市场范围
    A 股主板。沪市 600/601/603/605，深市 000/001/002/003。
    不含创业板(300/301)、科创板(688)、北交所(bj)。

日K 频率
    日线，一根 = 一个交易日。不做分钟级、不做周线。

复权方式
    前复权（qfq）。这是关键：止损位、止跌K高低点、均线全都在
    同一条前复权序列上算。若临时降级到不复权源，历史价位会与
    判定口径错位，必须显式标注，不能闷着用。

数据源（按优先级自动降级）
    1) 腾讯 proxy.finance.qq.com  —— 前复权，约 640 根上限
    2) 东方财富 push2his          —— 前复权（fqt=1）
    3) 新浪 money.finance.sina.com.cn —— 不复权，最后的兜底
    三者字段统一转成：[日期, 开, 收, 高, 低, 成交量(股), 成交额(元)]
    K 线不足 90 根视为次新/异常，不参与判定。

实时快照
    腾讯 qt.gtimg.cn，批量 30 个代码一次请求。
    单位：成交量=手，成交额=万元（内部转成元），总市值=亿元。

财报
    东方财富 datacenter。
    利润表用 RPT_F10_FINANCE_GINCOMEQC（单季口径），
    银行/保险等无营收字段的用 RPT_LICO_FN_CPD 兜底（累计口径），
    此时结果里会带 cum_only=True，输出时必须标明。
    现金流用 RPT_F10_FINANCE_GCASHFLOW（累计口径）。

时区与"今天"
    所有日期按本机本地日期（Asia/Shanghai）判断。
    盘中取历史K时会剔除数据源返回的当日未完成K线，
    再由实时快照拼一根合成K，避免半根K进入结构计算。
"""
import json
import os
import time
import urllib.parse
import urllib.request

from . import config as C
from . import indicators as I

UA = {'User-Agent': 'Mozilla/5.0'}
_EM_REFERER = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://quote.eastmoney.com/'}

# 历史K按交易日缓存。同一交易日内重复扫描不必重拉；
# 跨日自动失效（目录名带日期）。想强制刷新就删掉缓存目录。
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(_ROOT, 'data', 'kcache')


def _fetch(url, tries=None, timeout=None, headers=None):
    tries = tries or C.REQUEST_TRIES
    timeout = timeout or C.REQUEST_TIMEOUT
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=headers or UA)
            return urllib.request.urlopen(req, timeout=timeout).read()
        except Exception as e:          # 网络抖动、限流、断连，一律重试
            last = e
            time.sleep(0.5 * (i + 1))
    raise last


# ---------------------------------------------------------------- 日K

def _kline_tencent(code, n):
    """腾讯前复权日K。返回 [date, o, c, h, l, vol(股), amt(元)]。"""
    url = ('https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get'
           '?param=%s,day,,,%d,qfq' % (code, n))
    raw = _fetch(url).decode('utf-8', 'ignore')
    node = json.loads(raw)['data'][code]
    arr = node.get('qfqday') or node.get('day') or []
    ks = []
    for p in arr:
        close = float(p[2])
        vol = float(p[5]) * 100.0                      # 手 -> 股
        amt = float(p[8]) * 1e4 if len(p) > 8 and p[8] not in ('', None) else close * vol
        ks.append([p[0], float(p[1]), close, float(p[3]), float(p[4]), vol, amt])
    return ks


def _kline_eastmoney(code, n):
    sec = ('1.' if code.startswith('sh') else '0.') + code[2:]
    url = ('https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=%s'
           '&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57'
           '&klt=101&fqt=1&end=20500101&lmt=%d' % (sec, n))
    raw = _fetch(url, headers=_EM_REFERER, timeout=25)
    days = json.loads(raw)['data']['klines']
    ks = []
    for s in days:
        p = s.split(',')
        ks.append([p[0], float(p[1]), float(p[2]), float(p[3]), float(p[4]),
                   float(p[5]) * 100.0, float(p[6])])   # 手 -> 股，口径与腾讯一致
    return ks


def _kline_sina(code, n):
    """新浪日K。不复权，仅在腾讯与东财都不可用时兜底。"""
    url = ('https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/'
           'CN_MarketData.getKLineData?symbol=%s&scale=240&ma=no&datalen=%d' % (code, n))
    raw = _fetch(url).decode('utf-8', 'ignore')
    ks = []
    for p in json.loads(raw):
        c = float(p['close'])
        vol = float(p['volume'])
        ks.append([p['day'][:10], float(p['open']), c,
                   float(p['high']), float(p['low']), vol, c * vol])
    return ks


_SOURCES = (('tencent', _kline_tencent), ('eastmoney', _kline_eastmoney), ('sina', _kline_sina))
_active_source = None


def detect_source():
    """探一次源，避免几千只股票逐个去撞已经挂掉的接口。"""
    global _active_source
    if _active_source:
        return _active_source
    for name, fn in _SOURCES:
        try:
            if len(fn('sh600000', 40)) >= 30:
                _active_source = name
                return name
        except Exception:
            continue
    _active_source = 'sina'
    return _active_source


def kline(code, n=None, drop_today=True):
    """取日K，自动降级。返回 (bars, source_name, adjusted)。

    adjusted=False 表示走到了新浪兜底（不复权），调用方必须在输出里标注。
    """
    n = n or C.KLINE_BARS
    order = detect_source()
    names = [x[0] for x in _SOURCES]
    start = names.index(order)
    fn_map = dict(_SOURCES)
    for name in names[start:]:
        try:
            ks = fn_map[name](code, n)
            if not ks:
                continue
            if drop_today and ks[-1][0] >= time.strftime('%Y-%m-%d'):
                ks = ks[:-1]
            return ks, name, name != 'sina'
        except Exception:
            continue
    return [], 'none', False


def kline_cached(code, n=None, use_cache=True):
    """带当日缓存的 kline。扫描几千只时能把重复请求降到 0。"""
    if not use_cache:
        return kline(code, n)
    day = time.strftime('%Y-%m-%d')
    path = os.path.join(CACHE_DIR, day, code + '.json')
    if os.path.exists(path):
        try:
            d = json.load(open(path, 'r', encoding='utf-8'))
            if d.get('ks') and len(d['ks']) >= C.KLINE_MIN_BARS:
                return d['ks'], d.get('src', 'cache'), d.get('adj', True)
        except Exception:
            pass
    ks, src, adj = kline(code, n)
    if ks:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            json.dump(dict(ks=ks, src=src, adj=adj), open(path, 'w', encoding='utf-8'))
        except Exception:
            pass
    return ks, src, adj


# ---------------------------------------------------------------- 实时快照

def quote(codes):
    """腾讯实时快照 -> {code: dict}。"""
    out = {}
    for i in range(0, len(codes), C.QUOTE_BATCH):
        chunk = codes[i:i + C.QUOTE_BATCH]
        raw = _fetch('https://qt.gtimg.cn/q=' + ','.join(chunk)).decode('gbk', 'ignore')
        for ln in raw.strip().split(';'):
            if '=' not in ln:
                continue
            key = ln.split('=')[0].strip().replace('v_', '')
            try:
                f = ln.split('="')[1].split('~')
                if len(f) < 52:
                    continue
                out[key] = dict(
                    code=key, name=f[1], px=float(f[3]), prev=float(f[4]),
                    open=float(f[5]), high=float(f[33]), low=float(f[34]),
                    chg=float(f[32]),
                    vol=float(f[36]) if f[36] else 0.0,          # 手
                    amt=float(f[37]) / 1e4 if f[37] else 0.0,    # 万元 -> 亿元
                    mcap=float(f[45]) if f[45] else 0.0,         # 亿元
                    lb=float(f[49]) if f[49] else 0.0,
                    avg=float(f[51]) if f[51] else 0.0,
                )
            except Exception:
                continue
    return out


def merge_today(ks, q):
    """把实时快照拼成当日合成K线，替换掉数据源可能带出的半根K。"""
    if not (q.get('vol', 0) > 0 and q.get('px', 0) > 0):
        return list(ks)
    ks = list(ks)
    today = time.strftime('%Y-%m-%d')
    bar = [today, q['open'], q['px'], q['high'], q['low'],
           q['vol'] * 100.0, q['amt'] * 1e8]     # 手->股，亿元->元
    if ks and ks[-1][0] == today:
        ks[-1] = bar
    else:
        ks.append(bar)
    return ks


# ---------------------------------------------------------------- 结构准备

def prep(code, name, ks, mcap=0.0, px=0.0):
    """把原始K线整理成指标就绪的结构，供 signal.py 使用。"""
    if not ks:
        return None
    B = {
        'code': code, 'name': name,
        'd': [x[0] for x in ks], 'o': [x[1] for x in ks], 'c': [x[2] for x in ks],
        'h': [x[3] for x in ks], 'l': [x[4] for x in ks], 'v': [x[5] for x in ks],
        'a': [x[6] for x in ks],
    }
    c, h, l = B['c'], B['h'], B['l']
    B['e9'] = I.ema(c, C.EMA_FAST)
    B['e21'] = I.ema(c, C.EMA_MID)
    B['e50'] = I.ema(c, C.EMA_SLOW)
    B['ma6'] = I.sma(c, C.SMA_SHORT)
    B['ma20'] = I.sma(c, C.SMA_BOLL)
    B['ma21'] = I.sma(c, C.EMA_MID)
    B['ma50'] = I.sma(c, C.EMA_SLOW)
    B['atr'] = I.atr14(h, l, c, C.ATR_N)
    B['boll'] = I.boll_low(c, C.SMA_BOLL, C.BOLL_K)
    B['amt20'] = I.sma(B['a'], 20)
    B['mcap_now'] = mcap
    # 股本由市值反推，回测里用来算"时点市值"（当前市值会造成前视）
    B['shares'] = (mcap * 1e8 / px) if px > 0 else 0.0
    return B


def load_history(path):
    """逐行读历史数据文件，yield 原始 record。"""
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:
                continue


def load_benchmark(path):
    """读基准指数（沪深300），返回 {date: (close, ma20)}。"""
    raw = json.load(open(path, 'r', encoding='utf-8'))
    pairs = raw if isinstance(raw, list) else list(raw.items())
    closes, out, buf = [], {}, []
    for d, v in pairs:
        closes.append(v)
        buf.append(v)
        if len(buf) > 20:
            buf.pop(0)
        out[d] = (v, sum(buf) / len(buf))
    return out


# ---------------------------------------------------------------- 股票池

_CLIST = 'https://push2.eastmoney.com/api/qt/clist/get?'
# m:1 t:2 / t:23 = 沪市主板；m:0 t:6 / t:80 = 深市主板
_CLIST_FS = 'm:1+t:2,m:1+t:23,m:0+t:6,m:0+t:80'


def list_universe(limit=None):
    """拉全部 A 股主板代码。返回 [(code, name, mcap亿元, price), ...]。

    过滤：停牌（无价/无市值）、退市（名称含"退"或 PT 开头）、
    非主板（前缀不匹配）。这些都是"不该进池子"的，不是数据缺失。
    """
    out, page, size = [], 1, 100
    while True:
        url = _CLIST + urllib.parse.urlencode(dict(
            pn=page, pz=size, po=0, np=1, fltt=2, invt=2, fid='f12',
            fs=_CLIST_FS, fields='f12,f13,f14,f2,f20'))
        d = json.loads(_fetch(url)).get('data')
        if not d or not d.get('diff'):
            break
        rows = d['diff']
        for x in rows:
            code, name = str(x.get('f12', '')), str(x.get('f14', '')).strip()
            px_field, mcap_field = x.get('f2'), x.get('f20')
            if not code.isdigit() or len(code) != 6:
                continue
            if px_field in ('-', None) or mcap_field in ('-', None):
                continue                      # 停牌 / 无价
            if '退' in name or name.startswith('PT'):
                continue
            prefix = ('sh' if int(x.get('f13', 1)) == 1 else 'sz') + code
            if not prefix.startswith(C.BOARD_PREFIXES):
                continue                      # 只要主板
            out.append((prefix, name, float(mcap_field) / 1e8, float(px_field)))
            if limit and len(out) >= limit:
                return out
        if len(out) >= d.get('total', 0) or len(rows) < size:
            break
        page += 1
        time.sleep(0.15)                      # 别把列表接口打穿

    seen, uniq = set(), []                     # 去重保序
    for r in out:
        if r[0] not in seen:
            seen.add(r[0])
            uniq.append(r)
    return uniq


# ---------------------------------------------------------------- 财报

_E_ANN = ('https://np-anotice-stock.eastmoney.com/api/security/ann?sr=-1&page_size=10'
          '&page_index=1&ann_type=A&client_source=web&stock_list=%s&f_node=0')
_E_FIN = 'https://datacenter-web.eastmoney.com/api/data/v1/get?'


def _get_json(url):
    return json.loads(_fetch(url))


def earnings(code):
    """最新单季四项：营收同比 / 归母同比 / 毛利率同比(pp) / 经营现金流。

    返回 dict，字段缺失就缺失，调用方必须显式标注"未披露/拉取失败"，
    不允许静默填 0。
    """
    out = {}
    mk = 'SH' if code.startswith('6') else 'SZ'
    try:
        d = _get_json(_E_FIN + urllib.parse.urlencode(dict(
            reportName='RPT_F10_FINANCE_GINCOMEQC',
            columns='REPORT_DATE,REPORT_DATE_NAME,TOTAL_OPERATE_INCOME,OPERATE_COST,'
                    'PARENT_NETPROFIT,DEDUCT_PARENT_NETPROFIT',
            filter='(SECUCODE="%s.%s")' % (code, mk), pageNumber=1, pageSize=8,
            sortTypes=-1, sortColumns='REPORT_DATE', source='HSF10', client='PC')))
        rows = d['result']['data']
        if rows:
            a = rows[0]
            inc = (a['TOTAL_OPERATE_INCOME'] or 0) / 1e8
            out['q_date'] = a['REPORT_DATE'][:10]
            out['q_inc'] = inc
            out['q_np'] = (a['PARENT_NETPROFIT'] or 0) / 1e8
            out['q_gm'] = (inc - (a['OPERATE_COST'] or 0) / 1e8) / inc * 100 if inc else 0.0
            out['q_deduct_np'] = (a.get('DEDUCT_PARENT_NETPROFIT') or 0) / 1e8
            same = [r for r in rows
                    if r['REPORT_DATE'][:4] == str(int(a['REPORT_DATE'][:4]) - 1)
                    and r['REPORT_DATE'][5:7] == a['REPORT_DATE'][5:7]]
            if same:
                s = same[0]
                si = (s['TOTAL_OPERATE_INCOME'] or 0) / 1e8
                sn = (s['PARENT_NETPROFIT'] or 0) / 1e8
                out['q_inc_yoy'] = (inc / si - 1) * 100 if si else 0.0
                out['q_np_yoy'] = (out['q_np'] / sn - 1) * 100 if sn else 0.0
                if si:
                    sgm = (si - (s['OPERATE_COST'] or 0) / 1e8) / si * 100
                    out['q_gm_prev'] = sgm
                    out['q_gm_yoy'] = out['q_gm'] - sgm
    except Exception:
        pass

    # 银行/保险没有 TOTAL_OPERATE_INCOME，退回累计口径的业绩报表
    if 'q_np_yoy' not in out:
        try:
            d = _get_json(_E_FIN + urllib.parse.urlencode(dict(
                reportName='RPT_LICO_FN_CPD',
                columns='SECURITY_CODE,REPORTDATE,TOTAL_OPERATE_INCOME,YSTZ,'
                        'PARENT_NETPROFIT,SJLTZ',
                filter='(SECURITY_CODE="%s")' % code, pageNumber=1, pageSize=2,
                sortTypes=-1, sortColumns='REPORTDATE', source='WEB', client='WEB')))
            a = d['result']['data'][0]
            out['q_date'] = a['REPORTDATE'][:10]
            out['cum_only'] = True          # 累计口径，输出时必须标注
            out['q_inc'] = (a['TOTAL_OPERATE_INCOME'] or 0) / 1e8
            out['q_np'] = (a['PARENT_NETPROFIT'] or 0) / 1e8
            out['q_inc_yoy'] = a['YSTZ']
            out['q_np_yoy'] = a['SJLTZ']
        except Exception:
            pass

    try:
        d = _get_json(_E_FIN + urllib.parse.urlencode(dict(
            reportName='RPT_F10_FINANCE_GCASHFLOW',
            columns='REPORT_DATE,NETCASH_OPERATE',
            filter='(SECUCODE="%s.%s")' % (code, mk), pageNumber=1, pageSize=6,
            sortTypes=-1, sortColumns='REPORT_DATE', source='HSF10', client='PC')))
        rows = d['result']['data']
        if rows:
            out['cf'] = (rows[0]['NETCASH_OPERATE'] or 0) / 1e8
            out['cf_date'] = rows[0]['REPORT_DATE'][:10]
            same = [r for r in rows
                    if r['REPORT_DATE'][:4] == str(int(rows[0]['REPORT_DATE'][:4]) - 1)
                    and r['REPORT_DATE'][5:7] == rows[0]['REPORT_DATE'][5:7]]
            if same:
                out['cf_prev'] = (same[0]['NETCASH_OPERATE'] or 0) / 1e8
    except Exception:
        pass
    return out

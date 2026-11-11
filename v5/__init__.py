# -*- coding: utf-8 -*-
"""v5 动量回调体系 —— A 股主板的回调买入信号。

对外只暴露四件事：
    signal  怎么算 P / S / T / 买区
    scan    今天的行情里谁符合
    backtest 历史上这么做的结果
    data    数据从哪来、什么口径
"""
__version__ = '1.0.0'

from . import config      # noqa: F401
from . import indicators  # noqa: F401
from . import signal      # noqa: F401
from . import data        # noqa: F401
from . import scan        # noqa: F401
from . import backtest    # noqa: F401

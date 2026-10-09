from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / '.local' / 'ashare'
TZ = ZoneInfo('Asia/Shanghai')
VERSION = 'fixed-1.0.0'
SEEDS = {'000880':'潍柴重机','603061':'金海通','001288':'运机集团',
         '002709':'天赐材料','002675':'东诚药业','002317':'众生药业',
         '603200':'上海洗霸','002083':'孚日股份'}
WEIGHTS = {'relative':20,'industry':15,'structure':15,'volume':15,
           'liquidity':10,'fundamentals':15,'risk':10}
COST = .002
TICK = .01
MAX_WATCH = 50
PORT = 8765

def symbol(code):
    return ('sh' if code.startswith('6') else 'sz') + code

def eligible(code, name):
    return (len(code) == 6 and code.isdigit() and
            code.startswith(('000','001','002','003','600','601','603','605')) and
            bool(name) and not any(x in name.upper() for x in ('ST','退')))

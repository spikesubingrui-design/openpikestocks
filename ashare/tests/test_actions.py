from copy import deepcopy
from datetime import datetime, timedelta
import pytest
from fastapi.testclient import TestClient
from ashare.actions import overview
from ashare.app import create_app
from ashare.config import TZ, ROOT


@pytest.fixture
def inputs():
    t = datetime(2026, 10, 9, 10, 10, 5, tzinfo=TZ)
    q = {'name': '测试股票', 'price': 10., 'bid': 9.99, 'ask': 10.01,
         'bid_size': 100, 'ask_size': 100, 'upper': 11., 'lower': 9.,
         'stamp': t.isoformat(), 'status': 'NORMAL', 'conflict': False, 'source': 'fixture', 'amount': 1e8}
    row = {'code': '002675', 'name': '测试股票', 'qualified': True, 'evaluated_at': t.isoformat(),
           'quote': q, 'score': {'total': 90}, 'net_rr': 3., 'entry': 10.01, 'stop': 9.8, 'target': 10.8}
    lot = {'id': 1, 'remaining': 100, 'bought': '2026-10-08T10:00:00+08:00', 'stop': 10.1, 'target': 11.}
    h = {'account': 'real', 'code': '002675', 'quote': q, 'lots': [lot], 'sellable': 100}
    return t, row, h


def test_global_buy_ignores_selected_stock_and_sorts_qualified(inputs):
    t, row, h = inputs
    higher = deepcopy(row); higher['code'] = '002709'; higher['score']['total'] = 100; higher['qualified'] = False
    a = overview({'rows': [higher, row]}, [h], t)
    assert a['buy']['current']['code'] == row['code']
    assert a['buy']['watch']['code'] == higher['code']
    assert a['sell']['alerts'][0]['reason'] == '跌破原入场失效位置'
    assert a['sell']['alerts'][0]['actionable']


@pytest.mark.parametrize('condition', ['stale', 'closed', 'sealed', 'conflict', 'evaluation_stale'])
def test_overview_buy_rechecks_validity(inputs, condition):
    t, row, h = inputs
    if condition == 'stale': row['quote']['stamp'] = (t - timedelta(seconds=16)).isoformat()
    if condition == 'closed': t = t.replace(hour=16)
    if condition == 'sealed': row['quote']['ask'] = 0
    if condition == 'conflict': row['quote']['conflict'] = True
    if condition == 'evaluation_stale': row['evaluated_at'] = (t - timedelta(seconds=16)).isoformat()
    a = overview({'rows': [row]}, [h], t)
    assert a['buy']['state'] == 'WAIT' and a['buy']['current'] is None


def test_t1_is_per_lot_and_does_not_use_other_lot_sellable(inputs):
    t, row, h = inputs
    h['lots'][0]['bought'] = t.isoformat(); h['sellable'] = 500
    a = overview({'rows': [row]}, [h], t)
    alert = a['sell']['alerts'][0]
    assert alert['sellable'] == 0 and not alert['actionable']
    assert 'T+1' in ' '.join(alert['blockers'])


@pytest.mark.parametrize('condition', ['stale', 'closed', 'no_bid', 'conflict'])
def test_exit_blocks_stale_and_unexecutable_quotes(inputs, condition):
    t, row, h = inputs
    if condition == 'stale': h['quote']['stamp'] = (t - timedelta(seconds=16)).isoformat()
    if condition == 'closed': t = t.replace(hour=16)
    if condition == 'no_bid': h['quote']['bid_size'] = 0
    if condition == 'conflict': h['quote']['conflict'] = True
    a = overview({'rows': []}, [h], t)
    assert a['sell']['state'] == 'BLOCKED'
    assert not a['sell']['alerts'][0]['actionable']


def test_overview_is_read_only_and_separates_paper_and_real(inputs):
    t, row, h = inputs
    before = deepcopy((row, h))
    overview({'rows': [row]}, [h], t)
    assert (row, h) == before
    h['account'] = 'paper'
    assert overview({'rows': []}, [h], t)['sell']['state'] == 'NO_HOLDINGS'


def test_dashboard_and_desktop_identity(tmp_path):
    with TestClient(create_app(tmp_path / 'db', collect=False)) as client:
        h = client.get('/api/health').json()
        assert h['service_name'] == 'OpenPike Stocks' and h['desktop_protocol'] == 1
        assert h['workspace_root'] == str(ROOT)
        assert client.get('/api/actions').json()['sell']['state'] == 'NO_HOLDINGS'
        assert client.get('/api/dashboard').json()['actions']['buy']['state'] == 'WAIT'


@pytest.mark.parametrize('hour,minute', [(11, 29), (14, 59)])
def test_deadline_cannot_extend_past_session_close(inputs, hour, minute):
    t, row, h = inputs
    t = t.replace(hour=hour, minute=minute, second=59)
    row['evaluated_at'] = row['quote']['stamp'] = t.isoformat()
    a = overview({'rows': [row]}, [h], t)
    assert a['buy']['valid_until'] == (t + timedelta(seconds=1)).isoformat()
    assert a['sell']['alerts'][0]['valid_until'] == (t + timedelta(seconds=1)).isoformat()

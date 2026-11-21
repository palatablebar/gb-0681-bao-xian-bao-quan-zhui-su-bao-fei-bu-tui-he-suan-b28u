import csv, io, json
import pytest
from app.workflow import run
from app.errors import DomainError
from app.modules.m7_archive.export_out import bundle

CSV = '\n'.join([
    'id,policy_id,version,kind,effective,start,end,annual_premium,paid,ref,cover,note',
    'A01,R001,1,issue,2026-01-01,2026-01-01,2027-01-01,3650,3650,,基础险,原件',
    'A02,R001,2,change,2026-07-01,2026-01-01,2027-01-01,1825,0,,基础险,减保',
    'B01,S001,1,issue,2026-01-01,2026-01-01,2027-01-01,3650,3650,,基础险,原件',
    'B02,S001,2,change,2026-07-01,2026-01-01,2027-01-01,7300,0,,基础险,加保',
]).encode()

def make(params=None):
    return run(CSV, params or {})

def refund_ins(state):
    return [i for i in state['ledger']['instructions'] if i['kind'] == '退费']

def recovery_ins(state):
    return [i for i in state['ledger']['instructions'] if i['kind'] == '收回']

def test_refund_instruction_from_recalculation():
    s = make()
    rf = refund_ins(s)
    assert len(rf) == 1 and rf[0]['policy_id'] == 'R001'
    assert rf[0]['amount'] == '920.00' and rf[0]['status'] == '已发出' and rf[0]['no'].startswith('RF-')
    assert recovery_ins(s) == []
    assert s['ledger']['summary'] == {'refunded': '920.00', 'bounced': '0.00', 'offset': '0.00',
                                      'recoverable': '0.00', 'unit': 'CNY'}

def test_bounce_after_delivery_full_recovery():
    s = make({'bounces': [{'event_id': 'E1', 'policy_id': 'R001', 'amount': '3650', 'refund_delivered': True}]})
    summary = s['ledger']['summary']
    assert summary['bounced'] == '3650.00' and summary['offset'] == '0.00' and summary['recoverable'] == '3650.00'
    rc = recovery_ins(s)
    assert len(rc) == 1 and rc[0]['no'] == 'RC-E1' and rc[0]['status'] == '待收回'
    assert rc[0]['ref'] == refund_ins(s)[0]['no']
    assert refund_ins(s)[0]['status'] == '已送达'
    assert s['ledger']['execution'][0]['status'] == '退费已送达·待收回'
    assert s['ledger']['next_deduction'] == [{'policy_id': 'R001', 'amount': '3650.00', 'basis': '退票收回并入下期扣款'}]

def test_bounce_before_delivery_offsets():
    s = make({'bounces': [{'event_id': 'E1', 'policy_id': 'R001', 'amount': '3650'}]})
    summary = s['ledger']['summary']
    assert summary['bounced'] == '3650.00' and summary['offset'] == '920.00' and summary['recoverable'] == '2730.00'
    assert refund_ins(s)[0]['status'] == '已发出'
    assert s['ledger']['execution'][0]['status'] == '抵销后待收回'
    assert s['ledger']['next_deduction'][0]['amount'] == '2730.00'

def test_offset_exact_closes_case():
    s = make({'bounces': [{'event_id': 'E1', 'policy_id': 'R001', 'amount': '920'}]})
    assert s['ledger']['summary']['recoverable'] == '0.00' and s['ledger']['summary']['offset'] == '920.00'
    assert s['ledger']['execution'][0]['status'] == '抵销结案'
    assert recovery_ins(s)[0]['status'] == '已抵销'
    assert s['ledger']['next_deduction'] == []

def test_bounced_equals_offset_plus_recoverable():
    from decimal import Decimal
    for params in ({}, {'bounces': [{'event_id': 'E1', 'policy_id': 'R001', 'amount': '3650'}]},
                   {'bounces': [{'event_id': 'E2', 'policy_id': 'R001', 'amount': '3650', 'refund_delivered': True}]},
                   {'bounces': [{'event_id': 'E3', 'policy_id': 'S001', 'amount': '100'}]}):
        summary = make(params)['ledger']['summary']
        assert Decimal(summary['bounced']) == Decimal(summary['offset']) + Decimal(summary['recoverable'])

def test_duplicate_event_idempotent():
    event = {'event_id': 'E1', 'policy_id': 'R001', 'amount': '100'}
    s = make({'bounces': [event, dict(event)]})
    assert len(recovery_ins(s)) == 1 and s['ledger']['duplicates'] == 1
    assert s['ledger']['summary']['bounced'] == '100.00'

def test_rerun_same_bounce_reuses_run():
    params = {'bounces': [{'event_id': 'E1', 'policy_id': 'R001', 'amount': '3650'}]}
    a, b = make(params), make(params)
    assert a['run_id'] == b['run_id']
    assert a['ledger']['instructions'] == b['ledger']['instructions']

def test_refund_no_stable_across_bounce():
    plain = refund_ins(make())[0]['no']
    bounced = refund_ins(make({'bounces': [{'event_id': 'E1', 'policy_id': 'R001', 'amount': '3650'}]}))[0]['no']
    assert plain == bounced

def test_bounce_on_supplement_policy():
    s = make({'bounces': [{'event_id': 'E1', 'policy_id': 'S001', 'amount': '100'}]})
    assert s['ledger']['summary']['refunded'] == '920.00'
    assert s['ledger']['summary']['recoverable'] == '100.00'
    assert s['ledger']['execution'][0]['status'] == '收回挂账'
    assert recovery_ins(s)[0]['ref'] == ''

def test_unknown_policy_rejected():
    with pytest.raises(DomainError) as e:
        make({'bounces': [{'event_id': 'E9', 'policy_id': 'X999', 'amount': '1'}]})
    assert e.value.code == 'BOUNCE'

def test_overpaid_bounce_rejected():
    with pytest.raises(DomainError) as e:
        make({'bounces': [{'event_id': 'E1', 'policy_id': 'R001', 'amount': '2000'},
                          {'event_id': 'E2', 'policy_id': 'R001', 'amount': '2000'}]})
    assert e.value.code == 'BOUNCE'

@pytest.mark.parametrize('amount', ['abc', '-5', '0', None])
def test_invalid_amount_rejected(amount):
    with pytest.raises(DomainError) as e:
        make({'bounces': [{'event_id': 'E1', 'policy_id': 'R001', 'amount': amount}]})
    assert e.value.code == 'BOUNCE'

def test_bounces_must_be_list():
    with pytest.raises(DomainError) as e:
        make({'bounces': {'event_id': 'E1'}})
    assert e.value.code == 'BOUNCE'

def test_export_same_caliber():
    s = make({'bounces': [{'event_id': 'E1', 'policy_id': 'R001', 'amount': '3650', 'refund_delivered': True}]})
    rows = list(csv.reader(io.StringIO(bundle(s).lstrip('﻿'))))
    ins_rows = [r for r in rows if r[0] == '指令']
    assert len(ins_rows) == 2
    summary_row = [r for r in rows if r[0] == '指令汇总'][0]
    exported = json.loads(summary_row[5])
    assert exported['summary'] == s['ledger']['summary']
    assert exported['next_deduction'] == s['ledger']['next_deduction']

def test_voucher_never_overwritten(tmp_path, monkeypatch):
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'r.db'))
    from app import store
    s = make({'bounces': [{'event_id': 'E1', 'policy_id': 'R001', 'amount': '3650'}]})
    store.save(s['run_id'], s)
    tampered = dict(s)
    tampered['ledger'] = {'instructions': [], 'summary': {}}
    store.save(s['run_id'], tampered)
    kept = store.get(s['run_id'])
    assert kept['ledger']['instructions'] == s['ledger']['instructions']
    assert len(store.history()) == 1

def test_review_pages_show_three_calibers(client):
    raw = CSV.decode()
    r = client.post('/api/runs', json={'csv': raw, 'parameters': {'bounces': [
        {'event_id': 'E1', 'policy_id': 'R001', 'amount': '3650', 'refund_delivered': True}]}})
    assert r.status_code == 200
    run_id = r.get_json()['run_id']
    page = client.get('/m6_reconcile').data.decode()
    assert '已退' in page and '被退回' in page and '待收回' in page and 'RC-E1' in page
    archive_page = client.get('/m7_archive').data.decode()
    assert '指令笔数' in archive_page
    exported = client.get('/api/archive/' + run_id + '/export.csv').data.decode('utf-8-sig')
    assert '指令汇总' in exported and '3650.00' in exported

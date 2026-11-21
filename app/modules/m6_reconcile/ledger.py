import hashlib
from decimal import Decimal, InvalidOperation
from app.config import RULE_VERSION
from app.errors import DomainError

CENT = Decimal('.01')
ZERO = Decimal('0.00')

def _money(value):
    try:
        return Decimal(str(value)).quantize(CENT)
    except (InvalidOperation, ValueError, TypeError):
        raise DomainError('BOUNCE', '退票金额无效')

def refund_no(policy_id, difference):
    # 退费号只依赖保单、退费额与规则版本，与退票参数无关，退票前后保持稳定。
    key = '|'.join([str(policy_id), str(difference), RULE_VERSION])
    return 'RF-' + hashlib.sha256(key.encode()).hexdigest()[:10].upper()

def _events(parameters, known):
    raw_events = parameters.get('bounces')
    if raw_events is None:
        return [], 0
    if not isinstance(raw_events, list):
        raise DomainError('BOUNCE', 'bounces 必须是数组')
    events, seen, duplicates = [], set(), 0
    for raw in raw_events:
        if not isinstance(raw, dict):
            raise DomainError('BOUNCE', '退票事件必须是对象')
        event_id = str(raw.get('event_id', '')).strip()
        policy_id = str(raw.get('policy_id', '')).strip()
        if not event_id or 'amount' not in raw:
            raise DomainError('BOUNCE', '退票事件缺少事件号或金额')
        if policy_id not in known:
            raise DomainError('BOUNCE', '退票指向未知保单')
        amount = _money(raw['amount'])
        if amount <= 0:
            raise DomainError('BOUNCE', '退票金额必须为正')
        if event_id in seen:  # 同一退票重跑：幂等忽略，不新增指令
            duplicates += 1
            continue
        seen.add(event_id)
        events.append({'event_id': event_id, 'policy_id': policy_id, 'amount': amount,
                       'delivered': raw.get('refund_delivered') is True})
    totals = {}
    for e in events:
        totals[e['policy_id']] = totals.get(e['policy_id'], ZERO) + e['amount']
    for pid, total in totals.items():
        if total > Decimal(str(known[pid]['paid'])):
            raise DomainError('BOUNCE', '退票累计金额超过该保单已扣金额')
    return events, duplicates

def _status(refund, direct, offsettable):
    if refund == 0:
        return '收回挂账'
    if direct > 0:
        return '退费已送达·待收回'
    if offsettable > refund:
        return '抵销后待收回'
    if offsettable == refund:
        return '抵销结案'
    return '抵销后净退'

def build(state):
    known = {c['id']: c for c in state.get('calculations', [])}
    refunds = {}
    for c in state.get('calculations', []):
        diff = Decimal(str(c['difference']))
        if diff < 0:
            refunds[c['id']] = {'no': refund_no(c['id'], c['difference']), 'amount': -diff}
    events, duplicates = _events(state.get('parameters', {}), known)
    delivered_pids = {e['policy_id'] for e in events if e['delivered']}
    instructions = []
    for pid in sorted(refunds):
        instructions.append({'no': refunds[pid]['no'], 'kind': '退费', 'policy_id': pid,
                             'amount': str(refunds[pid]['amount']),
                             'status': '已送达' if pid in delivered_pids else '已发出',
                             'ref': '', 'event_id': ''})
    per_policy = {}
    for e in events:
        per_policy.setdefault(e['policy_id'], []).append(e)
        instructions.append({'no': 'RC-' + e['event_id'], 'kind': '收回', 'policy_id': e['policy_id'],
                             'amount': str(e['amount']), 'status': '待收回',
                             'ref': refunds.get(e['policy_id'], {}).get('no', ''), 'event_id': e['event_id']})
    execution, next_deduction = [], []
    offset_total = recoverable_total = ZERO
    for pid in sorted(per_policy):
        evs = per_policy[pid]
        refund = refunds.get(pid, {}).get('amount', ZERO)
        direct = sum((e['amount'] for e in evs if e['delivered']), ZERO)
        offsettable = sum((e['amount'] for e in evs if not e['delivered']), ZERO)
        offset = min(offsettable, refund)
        recoverable = direct + max(offsettable - refund, ZERO)
        net_refund = max(refund - offsettable, ZERO)
        if direct == 0 and offset == offsettable:  # 未送达且全额抵销，收回指令不再挂账
            for ins in instructions:
                if ins['event_id'] in {e['event_id'] for e in evs}:
                    ins['status'] = '已抵销'
        offset_total += offset
        recoverable_total += recoverable
        execution.append({'policy_id': pid, 'status': _status(refund, direct, offsettable),
                          'offset': str(offset), 'recoverable': str(recoverable), 'net_refund': str(net_refund)})
        if recoverable > 0:
            next_deduction.append({'policy_id': pid, 'amount': str(recoverable), 'basis': '退票收回并入下期扣款'})
    refunded_total = sum((r['amount'] for r in refunds.values()), ZERO)
    bounced_total = sum((e['amount'] for e in events), ZERO)
    return {'instructions': instructions, 'execution': execution, 'next_deduction': next_deduction,
            'duplicates': duplicates,
            'summary': {'refunded': str(refunded_total), 'bounced': str(bounced_total),
                        'offset': str(offset_total), 'recoverable': str(recoverable_total), 'unit': 'CNY'}}

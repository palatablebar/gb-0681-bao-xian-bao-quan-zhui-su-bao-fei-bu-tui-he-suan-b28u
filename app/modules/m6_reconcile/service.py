from app.modules.m6_reconcile.compare import balance
from app.modules.m6_reconcile.ledger import build

def execute(state):
    state['reconcile']=balance(state)
    state['ledger']=build(state)
    return state

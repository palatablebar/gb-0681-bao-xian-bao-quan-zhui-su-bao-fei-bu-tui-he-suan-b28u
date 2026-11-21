import io,csv,json,hashlib

def bundle(state):
    out=io.StringIO(newline='');writer=csv.writer(out)
    writer.writerow(['记录类型','批次','来源行','业务ID','原因码','内容'])
    rid=state.get('run_id','preview')
    writer.writerow(['运行清单',rid,'','','',json.dumps(state.get('manifest',{}),ensure_ascii=False,sort_keys=True)])
    writer.writerow(['对账',rid,'','','',json.dumps(state.get('reconcile',{}),ensure_ascii=False,sort_keys=True)])
    for row in state.get('calculations',[]):writer.writerow(['计算',rid,row.get('line',''),row.get('id',''),row.get('code',''),json.dumps(row,ensure_ascii=False,sort_keys=True)])
    for row in state['quarantine']:writer.writerow(['隔离',rid,row['line'],row.get('id',''),row['code'],json.dumps(row.get('raw',{}),ensure_ascii=False,sort_keys=True)])
    ledger=state.get('ledger',{})
    for ins in ledger.get('instructions',[]):writer.writerow(['指令',rid,'',ins.get('policy_id',''),ins.get('kind',''),json.dumps(ins,ensure_ascii=False,sort_keys=True)])
    if ledger:writer.writerow(['指令汇总',rid,'','','',json.dumps({'summary':ledger.get('summary',{}),'execution':ledger.get('execution',[]),'next_deduction':ledger.get('next_deduction',[]),'duplicates':ledger.get('duplicates',0)},ensure_ascii=False,sort_keys=True)])
    return '\ufeff'+out.getvalue()

def checksum(state):
    return hashlib.sha256(bundle(state).encode('utf-8')).hexdigest()

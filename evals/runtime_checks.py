"""Separate safe partial evidence from a successfully completed expert task."""


def execution_checks(trace, expect=None):
    expect=expect or {}
    expected_failure=(expect.get('allow_execution_failure',False)
        or any(expect.get('statuses',{}).get(s,0)>0 for s in ('partial','failed'))
        or expect.get('failed_operations',0)>0)
    tasks=trace.get('task_execution',[])
    models=trace.get('models',[])
    failures=[{'task_id':t['task_id'],'kind':t['kind'],'failure_code':t.get('failure_code')}
              for t in tasks if t.get('failure_code')]
    model_failures=[{'role':m.get('role'),'error_type':m.get('error_type'),
                     'provider_status_code':m.get('provider_status_code')}
                    for m in models if m.get('success') is False]
    checks=[]
    def add(name,ok,detail):
        checks.append({'name':name,'passed':bool(ok),'detail':detail,'critical':False,'dimension':'runtime'})
    add('provider_protocol_success',not any(m.get('provider_status_code')==400 for m in model_failures),model_failures)
    if not expected_failure:
        add('no_unexpected_execution_failure',not failures and not model_failures,failures+model_failures)
        for t in tasks:
            if t['kind']=='product_search' and t['status'] not in {'awaiting_input','cancelled','awaiting_dependency'}:
                add('recommendation_completed',t['status'] in {'completed','empty'} and not t.get('failure_code'),t)
                specialist=[m for m in models if m.get('role')==t['agent']]
                verified_empty=(t['status']=='empty' and any(e.get('event')=='catalog_scope_checked' and e.get('task_id')==t['task_id'] and e.get('matched_count')==0 for e in trace.get('events',[])))
                add('recommendation_model_success',(bool(specialist) or verified_empty) and all(m.get('success') is True for m in specialist),
                    [{'role':m.get('role'),'success':m.get('success')} for m in specialist])
    return checks

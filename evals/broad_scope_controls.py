"""Explicit service-context controls for the otherwise ambiguous HTTP questions."""
from evals.broad_cases import turn,TECH

def scope_cases():
    def case(cid,turns):return {'id':cid,'category':'explicit_service_error','family':cid,'split':'dev','actor':'owner','smoke':False,'expected_refunds':0,'fixture_refunds':{},'manual_review_required':True,'turns':turns}
    return [case('EV_SCOPE403',[turn('在这个客服系统里，网页返回HTTP 403是什么意思？','解释本系统403访问拒绝，不凭此声称系统正在故障。',tools=TECH,routes=['tech_support'],error_code='403')]),
        case('EV_SCOPE404',[turn('你好。','正常问候，不查询业务。',tools=[],no_tools=True),
            turn('本客服系统刚才页面返回HTTP 404，这表示什么？','这是本系统排错语境，应解释404资源未找到。',tools=TECH,routes=['tech_support'],error_code='404')])]

def main():
    import evals.broad_runner as engine
    engine.broad_cases=scope_cases;engine.VERSION='broad-scope-controls-20261103-1'
    return engine.main()

if __name__=='__main__':raise SystemExit(main())

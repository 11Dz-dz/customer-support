"""Real provider smoke: two native tool cycles, with thinking enabled and tools present.

Only reads public sample products. No reasoning text or credentials enter the report.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true',required=True)
    args=parser.parse_args()
    root=Path(__file__).resolve().parents[1]
    sys.path.insert(0,str(root))
    import backend.agent.langgraph_supervisor_1 as project
    from langchain_core.messages import AIMessage,HumanMessage,SystemMessage
    from langgraph.prebuilt import ToolNode
    from langgraph.graph import StateGraph,START,END
    from typing import TypedDict
    from backend.agent.context_budget import expert_context
    from evals.observer import EvaluationObserver
    from evals.fixtures import source_fingerprint
    from backend.agent.request_deadline import invoke_model
    from backend.tasks.task_state import ConversationTask,TaskParameters
    from backend.agent.tool_execution import TurnPolicy,guarded_tool_call,tool_execution_scope
    from backend.agent.tool_result import business_payload
    before=source_fingerprint()
    hashes={p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in root.glob('backend/**/*.py')}
    observer=EvaluationObserver(set())
    config={'callbacks':[observer],'metadata':{'agent':'sales'}}
    # Explicitly exercise thinking even if provider defaults change later.
    model=project.create_deepseek_model(role='sales').model_copy(update={'extra_body':{'thinking':{'type':'enabled'}}})
    tools=[project.get_product_info]
    select=model.bind_tools(tools,tool_choice='auto')
    finish=model.bind_tools(tools,tool_choice='none')
    class ToolState(TypedDict):
        messages:list
    builder=StateGraph(ToolState)
    builder.add_node('tools',ToolNode(tools,wrap_tool_call=guarded_tool_call))
    builder.add_edge(START,'tools');builder.add_edge('tools',END)
    node=builder.compile()
    messages=[];records=[]
    report={'scope':'real-provider protocol regression; automatic tool choice, not routing accuracy or multiuser test',
            'cycles':records,'passed':False}
    try:
        for pid in (915,946):
            task=ConversationTask(kind='product_detail',agent='sales',parameters=TaskParameters(product_ids=[pid]))
            messages.append(HumanMessage(content=f'请调用get_product_info查询商品{pid}，然后简要介绍这件商品。'))
            with tool_execution_scope(TurnPolicy(task=task,timeout_seconds=90)):
                view,_=expert_context([SystemMessage(content='你是商品客服，遵守工具事实，回答使用中文。')],messages,task)
                called=invoke_model(select,view,config)
                assert isinstance(called,AIMessage) and called.tool_calls
                assert isinstance(called.additional_kwargs.get('reasoning_content'),str)
                assert all(c['name']=='get_product_info' and str(c['args'].get('product_name'))==str(pid) for c in called.tool_calls)
                messages.append(called)
                result=node.invoke({'messages':messages},config)
                returned=result['messages']
                assert returned and all(business_payload(m.content,m.name).get('success') is True for m in returned)
                messages.extend(returned)
                view,_=expert_context([SystemMessage(content='只根据工具结果介绍商品，不要重复查询。')],messages,task)
                answer=invoke_model(finish,view,config)
                assert answer.content and not answer.tool_calls
                # Mirror the main application's program-rendered public answer boundary.
                messages.append(AIMessage(content=f'已查询并介绍商品{pid}。'))
                records.append({'product_id':pid,'native_calls':len(called.tool_calls),
                                'reasoning_received':True,'tool_results_ok':True,'answer_completed':True})
        assert len(observer.models)==4 and all(m['success'] is True for m in observer.models)
        report['passed']=True
    except Exception as exc:
        report.update(error_type=type(exc).__name__,provider_status_code=getattr(exc,'status_code',None),
                      provider_message=str(exc).replace(project.DEEPSEEK_API_KEY,'[redacted]')[:600])
    report.update(models=observer.models,source_unchanged=before==source_fingerprint(),
                  code_unchanged=all(hashlib.sha256((root/n).read_bytes()).hexdigest()==h for n,h in hashes.items()))
    report['passed'] &= report['source_unchanged'] and report['code_unchanged']
    dest=root/'evals/reports'/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-deepseek-protocol')
    dest.mkdir(parents=True,mode=0o700)
    path=dest/'protocol_report.json';path.write_text(json.dumps(report,ensure_ascii=False,indent=2));path.chmod(0o600)
    print(json.dumps({'passed':report['passed'],'cycles':len(records),'report':str(path),
                      'error_type':report.get('error_type'),'provider_status_code':report.get('provider_status_code')},ensure_ascii=False),flush=True)
    return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(main())

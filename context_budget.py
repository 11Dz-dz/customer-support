"""Bound model message characters while preserving full stored history and valid tool-call pairs.

The limits cover serialized message contents/arguments, not provider tool schemas or billed tokens.
Only model-input copies are compacted. Numeric facts are copied, never summarized by another LLM.
"""
import json

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

EXPERT_CONTEXT_CHARS=16000
ROUTER_CONTEXT_CHARS=24000


class ContextBudgetError(ValueError):
    pass


def message_chars(messages):
    return sum(len(json.dumps({'content':m.content,'tool_calls':getattr(m,'tool_calls',[])},
                              ensure_ascii=False,default=str,separators=(',',':'))) for m in messages)


def compact_task_record(task):
    row={'task_id':task.task_id,'kind':task.kind,'agent':task.agent,'status':task.status,
         'parameters':task.parameters.model_dump(exclude_none=True),'focus':task.focus,
         'awaiting_fields':task.awaiting_fields,'depends_on':task.depends_on,
         'dependency_mode':task.dependency_mode,'retry_failed':task.retry_failed,
         'displayed_product_ids':task.displayed_product_ids[:20],
         'displayed_product_count':len(task.displayed_product_ids),
         'query':task.query[:300]}
    if len(task.query)>300:
        row['query_excerpt_only']=True
    row['operations']=[{'tool':o.tool,'status':o.status,'retryable':o.retryable,'attempts':o.attempts,
                        'outcome':o.outcome,'error_code':o.error_code}
                       for o in task.operations]
    return row


def bounded_public_dialogue(messages, max_chars, max_messages=8):
    public=[m for m in messages if isinstance(m,HumanMessage) or (isinstance(m,AIMessage) and not m.tool_calls)]
    if not public:
        return []
    latest_user=max((i for i,m in enumerate(public) if isinstance(m,HumanMessage)),default=-1)
    if latest_user<0:
        return []
    current=public[latest_user]
    if message_chars([current])>max_chars:
        raise ContextBudgetError('当前问题过长，请精简或拆成几次输入后再试。')
    chosen=[current]
    # Add earlier whole user/assistant turns from newest to oldest, never half a long response.
    groups=[]
    for m in public[:latest_user]:
        if isinstance(m,HumanMessage) or not groups:
            groups.append([])
        groups[-1].append(m)
    for group in reversed(groups):
        if len(chosen)+len(group)>max_messages:
            break
        if message_chars(group+chosen)>max_chars:
            break
        chosen=group+chosen
    return chosen


def _compact_value(value, task, depth=0):
    if depth>8:
        return {'_context_omitted':'nested value exceeds model context depth'}
    if isinstance(value,dict):
        result={}
        for key,item in value.items():
            if isinstance(item,list):
                relevant=item
                if key in {'items','products'} and task is not None and task.parameters.product_ids:
                    wanted=set(task.parameters.product_ids)
                    matches=[r for r in item if isinstance(r,dict) and r.get('product_id') in wanted]
                    if matches:
                        relevant=matches
                selected=relevant[:5]
                result[key]=[_compact_value(v,task,depth+1) for v in selected]
                if len(selected)<len(item):
                    result['_context_omitted_'+key]=len(item)-len(selected)
            elif isinstance(item,str) and len(item)>600:
                result[key]=item[:600]
                result['_context_excerpt_'+key]=True
            else:
                result[key]=_compact_value(item,task,depth+1)
        return result
    if isinstance(value,list):
        return [_compact_value(v,task,depth+1) for v in value[:5]]
    if isinstance(value,str) and len(value)>600:
        return value[:600]+'（模型上下文节选）'
    return value


def compact_tool_message(message, task):
    if message_chars([message])<=2600:
        return message
    try:
        value=json.loads(message.content)
    except (ValueError,TypeError):
        return message.model_copy(update={'content':str(message.content)[:1800]+'（模型上下文节选；完整记录已保存）'})
    copy=_compact_value(value,task)
    if isinstance(copy,dict):
        notes=copy['data'] if copy.get('schema_version')==1 and isinstance(copy.get('data'),dict) else copy
        notes['_context_note']='仅缩减模型输入，省略部分不代表完整数据为空；完整结果由程序保存和展示。'
    return message.model_copy(update={'content':json.dumps(copy,ensure_ascii=False,default=str)})


def _tool_groups(messages):
    results={m.tool_call_id:m for m in messages if isinstance(m,ToolMessage)}
    groups=[]
    for m in messages:
        if isinstance(m,AIMessage) and m.tool_calls:
            calls=[c for c in m.tool_calls if c['id'] in results]
            if calls:
                groups.append([m.model_copy(update={'tool_calls':calls}),*[results[c['id']] for c in calls]])
    return groups


def expert_context(prefix, messages, task=None, max_chars=EXPERT_CONTEXT_CHARS):
    before=message_chars(prefix+list(messages))
    available=max_chars-message_chars(prefix)
    if available<1000:
        raise ContextBudgetError('当前任务上下文过大，请分步处理。')
    public=bounded_public_dialogue(messages,min(available,5000),max_messages=8)
    groups=_tool_groups(messages)
    selected=[]
    compacted=0
    for group in reversed(groups):
        copy=[group[0],*[compact_tool_message(m,task) for m in group[1:]]]
        if message_chars(public+copy+selected)>available:
            continue
        compacted+=sum(a.content!=b.content for a,b in zip(group[1:],copy[1:]))
        selected=copy+selected
    public_ids={id(m) for m in public}
    copies={m.tool_call_id:m for m in selected if isinstance(m,ToolMessage)}
    call_ids=set(copies)
    ordered=[]
    for m in messages:
        if id(m) in public_ids:
            ordered.append(m)
        elif isinstance(m,AIMessage) and m.tool_calls:
            calls=[c for c in m.tool_calls if c['id'] in call_ids]
            if calls:
                ordered.append(m.model_copy(update={'tool_calls':calls}))
        elif isinstance(m,ToolMessage) and m.tool_call_id in copies:
            ordered.append(copies[m.tool_call_id])
    output=prefix+ordered
    # Preserve chronology and whole AI/tool pairs; never move old results after a new user request.
    assert message_chars(output)<=max_chars
    stats={'role':'specialist','input_messages':len(prefix)+len(messages),'output_messages':len(output),
           'input_chars':before,'output_chars':message_chars(output),'limit_chars':max_chars,
           'compacted_tool_results':compacted,'dropped_tool_groups':len(groups)-sum(isinstance(m,AIMessage) and bool(m.tool_calls) for m in selected)}
    return output,stats


def router_context(prefix, dialogue, max_chars=ROUTER_CONTEXT_CHARS):
    public=bounded_public_dialogue(dialogue,max_chars-message_chars(prefix),max_messages=12)
    result=prefix+public
    return result,{'role':'router','input_messages':len(prefix)+len(dialogue),'output_messages':len(result),
                   'input_chars':message_chars(prefix+list(dialogue)),'output_chars':message_chars(result),
                   'limit_chars':max_chars,'compacted_tool_results':0,'dropped_tool_groups':0}

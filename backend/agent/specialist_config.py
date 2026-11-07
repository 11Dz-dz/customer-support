"""Validated specialist composition; task permissions still belong to program guards."""
from dataclasses import dataclass
import json
from pathlib import Path
from types import MappingProxyType

_ALLOWED_TOOLS={
 'tech_support':frozenset({'check_system_status','search_error_code'}),
 'sales':frozenset({'query_products','search_products','get_product_info','check_promotion','calculate_price','query_product_variants'}),
 'billing':frozenset({'query_my_orders','query_order','query_invoice','check_payment_status','query_refund_status'}),
}

@dataclass(frozen=True)
class SpecialistSpec:
    name:str
    prompt:str
    tools:tuple
    skills:tuple


def load_specialist_specs():
    source=Path(__file__).resolve().parents[2]/'config/specialists.json'
    return validate_specialist_config(json.loads(source.read_text(encoding='utf-8')))


def validate_specialist_config(value):
    if not isinstance(value,dict) or set(value)!={'version','specialists'} or value['version']!=1:
        raise ValueError('Invalid specialist configuration version/fields')
    rows=value['specialists']
    if not isinstance(rows,list) or len(rows)!=3:raise ValueError('Exactly three specialists required')
    result={}
    for row in rows:
        if not isinstance(row,dict) or set(row)!={'name','prompt','tools','skills'}:raise ValueError('Invalid specialist fields')
        name=row['name']
        if not isinstance(name,str) or name not in _ALLOWED_TOOLS or name in result:raise ValueError('Invalid or duplicate specialist')
        if row['prompt']!=name:raise ValueError('Specialist prompt must match its role')
        tools=row['tools'];skills=row['skills']
        if not isinstance(tools,list) or not tools or any(not isinstance(t,str) for t in tools):raise ValueError('Invalid tool list')
        if len(set(tools))!=len(tools) or not set(tools)<=_ALLOWED_TOOLS[name]:raise ValueError('Tool outside specialist permission boundary')
        if not isinstance(skills,list) or any(not isinstance(s,str) for s in skills) or len(set(skills))!=len(skills):raise ValueError('Invalid skills')
        if skills and (name!='sales' or not set(skills)<={'product-recommendation','product-comparison'}):raise ValueError('Unknown or out-of-role skill')
        if 'product-comparison' in skills and 'product-recommendation' not in skills:raise ValueError('Comparison requires shared recommendation steps')
        result[name]=SpecialistSpec(name,name,tuple(tools),tuple(skills))
    return MappingProxyType(result)


def resolve_tools(spec,registry):
    try:resolved=[registry[name] for name in spec.tools]
    except KeyError:raise ValueError('Configured tool is not registered') from None
    if any(t.name!=name for t,name in zip(resolved,spec.tools)):raise ValueError('Tool registry name mismatch')
    return resolved


def build_specialist(spec,tools,model_factory):
    # Late imports keep the configuration and asset checks usable without an SDK.
    from langgraph.prebuilt import create_react_agent,ToolNode
    from backend.agent.tool_execution import guarded_model,guarded_prompt,guarded_tool_call
    from backend.agent.prompt_catalog import read_prompt
    from backend.agent.answer_contract import PLAN_INSTRUCTION
    prompt=read_prompt('scope')+read_prompt(spec.prompt)+(PLAN_INSTRUCTION if spec.name in {'sales','billing'} else '')
    return create_react_agent(
        model=guarded_model(model_factory(role=spec.name),tools),
        tools=ToolNode(tools,wrap_tool_call=guarded_tool_call),checkpointer=False,
        prompt=guarded_prompt(prompt,skills=spec.skills,tools=tools))

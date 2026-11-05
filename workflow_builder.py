"""Graph topology only. Node functions retain their task/permission checks."""
from langgraph.graph import StateGraph,START,END


def build_workflow(state_type,nodes,route_after_supervisor):
    workflow = StateGraph(state_type)
    workflow.add_node('identity_guard', nodes['identity_guard_node'])
    workflow.add_node('supervisor', nodes['supervisor_node'])
    workflow.add_node('scheduler', nodes['scheduler_node'])
    workflow.add_node('tech_support', nodes['tech_agent_node'])
    workflow.add_node('sales', nodes['sales_agent_node'])
    workflow.add_node('billing', nodes['billing_agent_node'])
    workflow.add_node('collect', nodes['collect_node'])
    workflow.add_node('answer', nodes['answer_node'])
    workflow.add_edge(START, 'identity_guard')
    workflow.add_edge('identity_guard', 'supervisor')
    workflow.add_edge('supervisor', 'scheduler')
    workflow.add_conditional_edges('scheduler', route_after_supervisor,
        {'tech_support': 'tech_support', 'sales': 'sales', 'billing': 'billing', '__end__': 'answer','REPLAN':'supervisor'})
    for specialist in ['tech_support', 'sales', 'billing']:
        workflow.add_edge(specialist, 'collect')
    workflow.add_edge('collect', 'scheduler')
    workflow.add_conditional_edges('answer',lambda state:'supervisor' if state.get('next')=='REPLAN' else END,
                                   {'supervisor':'supervisor',END:END})
    return workflow

"""Routine conversation intents; mixed business requests always go through the planner."""
import unicodedata

REPLIES={
 'out_of_scope':'我只支持商品咨询、订单账单、退款进度、本客服系统的技术支持和人工工单相关问题，不能处理无关请求。你可以问商品推荐、订单状态、退款审核进度，或本系统的连接和 API 报错。',
 'greeting':'你好！我是 River 客服，可以帮你了解商品、查询自己的订单和退款进度，也可以协助创建人工处理工单。有什么需要我帮你？',
 'thanks':'不客气！还有其他需要我帮你了解或查询的吗？',
 'goodbye':'再见！需要帮助时，随时回来找我。',
 'capabilities':'我可以帮你咨询商品、核算商品小计、查询当前账号的订单和退款审核进度、排查系统问题，或创建人工处理工单。申请退款请到「我的订单」页面确认提交。',
}
_PHRASES={
 'greeting':{'你好','您好','嗨','早上好','下午好','晚上好','早安','hi','hello','hey','goodmorning','goodevening'},
 'thanks':{'谢谢','多谢','感谢','辛苦了','thanks','thankyou'},
 'goodbye':{'再见','拜拜','bye','goodbye'},
 'capabilities':{'你能做什么','你可以做什么','你是谁'},
}


def routine_intent(text):
    normalized=''.join(c for c in text.casefold() if not c.isspace() and not unicodedata.category(c).startswith('P'))
    return next((kind for kind,values in _PHRASES.items() if normalized in values),None)


def conversation_reply(intent):return REPLIES.get(intent,REPLIES['capabilities'])

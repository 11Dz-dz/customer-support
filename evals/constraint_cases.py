"""Frozen authored constraint regression v1: planner acceptance != final-answer acceptance."""
BASE={'product_category':'bicycles','max_price':1000,'limit':1}

def case(cid,text,expected,old=True,**extra):
    return {'id':cid,'inputs':[text] if isinstance(text,str) else text,
            'old':dict(BASE) if old is True else old,'expected':expected,**extra}

CASES=[
 case('F01','推荐一款自行车，预算500以内',{'product_category':'bicycles','limit':1,'max_price':500},False),
 case('F02','预算500以内',{'product_category':'bicycles','limit':1,'max_price':500}),
 case('F03','预算提高到2000以内',{'product_category':'bicycles','limit':1,'max_price':2000}),
 case('F04','预算五百以内',{'product_category':'bicycles','limit':1,'max_price':500}),
 case('F05','取消预算限制',{'product_category':'bicycles','limit':1,'max_price':None,'min_price':None}),
 case('F06','不看自行车了，换成头盔',{'product_category':'accessories','max_price':1000,'limit':1}),
 case('F07','类别不限，预算500以内',{'product_category':'all','max_price':500,'limit':1}),
 case('F08','推荐一款自行车头盔，预算100以内',{'product_category':'accessories','max_price':100,'limit':1},False),
 case('F09','推荐一款自行车车把，预算100以内',{'product_category':'components','max_price':100,'limit':1},False),
 case('F10','改成两款',{'product_category':'bicycles','limit':2,'max_price':1000}),
 case('F11','预算500以内，每款买两件',{'product_category':'bicycles','limit':1,'max_price':500}),
 case('F12','改成红色',{'product_category':'bicycles','color':'Red','limit':1,'max_price':1000},old={**BASE,'color':'Black'}),
 case('F13','颜色不限',{'color':None,'product_category':'bicycles','max_price':1000},old={**BASE,'color':'Black'}),
 case('F14','尺寸改成44',{'size':'44','product_category':'bicycles','max_price':1000},old={**BASE,'size':'42'}),
 case('F15','不限尺寸',{'size':None,'product_category':'bicycles','max_price':1000},old={**BASE,'size':'42'}),
 case('F16','预算500以内',{'include_variants':True,'color':'Black','size':'42','product_category':'bicycles','max_price':500},old={**BASE,'include_variants':True,'color':'Black','size':'42'}),
 case('F17','也看同型号不同规格',{'include_variants':True,'product_category':'bicycles','max_price':1000}),
 case('F18','只看不同型号',{'include_variants':False,'product_category':'bicycles'},old={**BASE,'include_variants':True}),
 case('F19','价格500到1000元之间',{'min_price':500,'max_price':1000,'product_category':'bicycles'}),
 case('F20','最低价格600元',{'min_price':600,'max_price':1000,'product_category':'bicycles'}),
 case('F21','预算500以内',{},old={**BASE,'min_price':600},clarify=True),
 case('F22',['推荐一款自行车，预算1000以内','预算500以内'],{'product_category':'bicycles','max_price':500,'limit':1},False),
 case('F23',['推荐一款自行车','改成两款，预算1000以内'],{'product_category':'bicycles','max_price':1000,'limit':2},False),
 case('F24','改成三件',{'quantity':3,'product_ids':[776]},old={'product_ids':[776],'quantity':2},kind='quote'),
 case('F25','分别报价，每款买两件',{'quantity':2,'quote_grouping':'separate','product_ids':[776,777]},old={'product_ids':[776,777],'quantity':1},kind='quote'),
 case('F26','取消最低价格限制',{'min_price':None,'max_price':1000,'product_category':'bicycles'},old={**BASE,'min_price':600}),
 case('F27','按价格从高到低列出前3款',{'limit':3,'sort_by':'price','sort_order':'desc','max_price':1000},kind='catalog_query'),
 case('F28','列出不超过500的黑色商品',{'color':'Black','max_price':500,'operation':'list'},False,kind='catalog_query'),
 case('F29','推荐三款公路车，预算2000以内',{'product_category':'bicycles','max_price':2000,'limit':3},False),
 case('F30','另外新推荐一款头盔，不要沿用刚才预算',{'product_category':'accessories','limit':1,'max_price':None},new_request=True),
 case('F31','预算500以内，不要改变其他条件',{'product_category':'bicycles','limit':1,'max_price':500,'color':'Black'},old={**BASE,'color':'Black'}),
 case('F32','数量还是一款，预算500以内',{'product_category':'bicycles','limit':1,'max_price':500}),
]

# Full graph/model/tools/SQL/answer cases. IDs and expectations are fixed before execution.
END_TO_END=[
 {'id':'E01','turns':['推荐一款自行车，预算500以内'],'category':'bicycles','budget':500,'limit':1},
 {'id':'E02','turns':['推荐一款自行车，预算1000以内'],'category':'bicycles','budget':1000,'limit':1},
 {'id':'E03','turns':['推荐一款自行车，预算1000以内','预算500以内'],'category':'bicycles','budget':500,'limit':1},
 {'id':'E04','turns':['推荐一款自行车，预算500以内','预算提高到1000以内'],'category':'bicycles','budget':1000,'limit':1},
 {'id':'E05','turns':['推荐一款黑色自行车，预算1000以内'],'category':'bicycles','budget':1000,'limit':1,'color':'Black'},
 {'id':'E06','turns':['推荐一款自行车，预算1000以内','改成黑色'],'category':'bicycles','budget':1000,'limit':1,'color':'Black'},
 {'id':'E07','turns':['推荐一款自行车，预算1000以内','不看自行车了，换成头盔'],'category':'accessories','budget':1000,'limit':1},
 {'id':'E08','turns':['推荐一款自行车头盔，预算100以内'],'category':'accessories','budget':100,'limit':1},
 {'id':'E09','turns':['推荐两款自行车，预算1000以内'],'category':'bicycles','budget':1000,'limit':2},
 {'id':'E10','turns':['推荐一款自行车，预算1000以内','改成两款'],'category':'bicycles','budget':1000,'limit':2},
 {'id':'E11','turns':['推荐一款尺寸为99的自行车，预算1000以内'],'category':'bicycles','budget':1000,'limit':1,'size':'99'},
 {'id':'E12','turns':['推荐一款自行车，预算五百以内'],'category':'bicycles','budget':500,'limit':1},
]

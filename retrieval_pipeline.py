"""Bounded candidate fusion and compact evidence; never a substitute for suitability judgement."""
import json
import re
from decimal import Decimal,InvalidOperation

MAX_CANDIDATES=30
CARD_FIELDS=('product_id','name','product_number','product_model_id','product_subcategory_id',
             'color','size','list_price','description_zh','description_en')


def search_phrases(query,terms=None):
    if terms is not None and (not isinstance(terms,list) or len(terms)>8 or
        any(not isinstance(t,str) or not t.strip() or len(t)>80 for t in terms)):
        raise ValueError('Invalid search terms')
    phrases=list(dict.fromkeys(t.strip() for t in (terms or [query]) if t.strip()))
    return phrases[:8]


def lexical_expression(query,terms=None):
    """Phrase boosts plus bounded English term coverage; all values are SQL parameters.

    This applies only to recommendation search, not exact catalog filters.
    Equivalent phrases are alternatives, not mandatory extra words in one long sentence.
    """
    phrases=search_phrases(query,terms)
    words=list(dict.fromkeys(w.lower() for phrase in phrases for w in re.findall(r'[A-Za-z][A-Za-z-]{2,}',phrase)))[:12]
    words=[w for w in words if w not in {'the','for','and','with','within','recommend','products','product'}]
    pieces=[];args=[]
    for phrase in phrases:
        escaped=phrase.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')
        pieces.append("CASE WHEN concat_ws(' ',name,product_number,description_zh,description_en) ILIKE %s THEN 10 ELSE 0 END")
        args.append('%'+escaped+'%')
    for word in words:
        pieces.append("CASE WHEN to_tsvector('english',coalesce(name,'')||' '||coalesce(description_en,'')) @@ plainto_tsquery('english',%s) THEN 1 ELSE 0 END")
        args.append(word)
    return '('+' + '.join(pieces or ['0'])+')',args


def family_key(product):
    if product.get('product_model_id') is not None:return ('model',product['product_model_id'])
    # Older tool records may lack model metadata. Remove only recorded variant values.
    name=str(product.get('name') or product['product_id']).lower()
    for key in ('color','size'):
        value=str(product.get(key) or '').lower()
        if value:name=re.sub(r'(?<!\w)'+re.escape(value)+r'(?!\w)',' ',name)
    return ('name',re.sub(r'[\s,]+',' ',name).strip())


def candidate_pool(results,task=None,limit=MAX_CANDIDATES):
    """Fuse distinct search executions across this task, recheck scope, diversify.

    Returned ranking is a recall ordering. The specialist makes the semantic
    selection using descriptions; no score is advertised as a probability.
    """
    from catalog_filters import category_matches
    found={};scores={};seen_calls=set()
    p=getattr(task,'parameters',None)
    for tool,data in results:
        if tool not in {'search_products','get_product_info'} or data.get('success') is not True:continue
        rows=data.get('products',[])+([data['product']] if isinstance(data.get('product'),dict) else [])
        signature=json.dumps([tool,data.get('query'),data.get('price_filters'),[r.get('product_id') for r in rows]],sort_keys=True,default=str)
        if signature in seen_calls:continue
        seen_calls.add(signature);seen_ids=set()
        for rank,row in enumerate(rows,1):
            pid=row.get('product_id')
            if type(pid) is not int or pid in seen_ids:continue
            seen_ids.add(pid)
            from catalog_query import matches as query_matches
            if p is not None and not query_matches(row,p.catalog_filter):continue
            if not category_matches(row,getattr(p,'product_category',None)):continue
            from catalog_filters import facets_match
            if p is not None and not facets_match(row,p.color,p.size):continue
            if p is not None:
                try:
                    price=Decimal(str(row['list_price']))
                    if not price.is_finite() or price<=0:continue
                    if p.min_price is not None and price<Decimal(str(p.min_price)):continue
                    if p.max_price is not None and price>Decimal(str(p.max_price)):continue
                except (KeyError,InvalidOperation,ValueError):continue
            found[pid]={**found.get(pid,{}),**row}
            scores[pid]=scores.get(pid,0)+1/(60+rank)
    ranked=sorted(found,key=lambda pid:(-scores[pid],pid))
    # One representative per model first, then remaining variants. Explicit color
    # and size preferences affect the representative, not a fabricated product model.
    def variant_preference(pid):
        row=found[pid]
        return sum(getattr(p,key,None) is not None and str(getattr(p,key)).lower()==str(row.get(key,'')).lower()
                   for key in ('color','size')) if p else 0
    groups={}
    for pid in ranked:groups.setdefault(family_key(found[pid]),[]).append(pid)
    representatives=[max(ids,key=variant_preference) for ids in groups.values()]
    ordered=ranked if getattr(p,'include_variants',False) else representatives+[pid for pid in ranked if pid not in representatives]
    return [found[pid] for pid in ordered[:limit]]


def compact_cards(products):
    # Full original descriptions for the bounded candidate pool. Redundant
    # retrieved_content, weight and transport metadata do not enter the prompt.
    return [{k:p[k] for k in CARD_FIELDS if p.get(k) is not None} for p in products]


def diverse_selection(ids,products,limit,include_variants=False):
    lookup={p['product_id']:p for p in products};seen=set();selected=[]
    for pid in ids:
        if pid not in lookup:continue
        family=('id',pid) if include_variants else family_key(lookup[pid])
        if family in seen:continue
        selected.append(pid);seen.add(family)
        if len(selected)>=limit:break
    return selected

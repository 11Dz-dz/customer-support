"""Read-only, developer-maintained skill guidance; never expands tool permissions."""
from dataclasses import dataclass
from functools import lru_cache
import hashlib
from pathlib import Path
import re

_ROOT=Path(__file__).resolve().parents[2]/'skills'
_PATHS={name:name+'/SKILL.md' for name in ('product-recommendation','product-comparison')}

@dataclass(frozen=True)
class Skill:
    name:str
    version:str
    description:str
    instructions:str
    sha256:str

@lru_cache(maxsize=4)
def load_skill(name):
    if name not in _PATHS:raise ValueError('Unknown skill')
    path=(_ROOT/_PATHS[name]).resolve()
    if not path.is_relative_to(_ROOT.resolve()):raise ValueError('Skill path escapes skill directory')
    text=path.read_text(encoding='utf-8')
    if not text.startswith('---\n') or len(text)>3000:raise ValueError('Invalid or oversized skill document')
    header,separator,body=text[4:].partition('\n---\n')
    if not separator or not body.strip():raise ValueError('Skill has no instructions')
    metadata={}
    for line in header.splitlines():
        key,sep,value=line.partition(':')
        if not sep or key in metadata:raise ValueError('Invalid skill metadata')
        metadata[key]=value.strip()
    if set(metadata)!={'name','version','description'} or metadata['name']!=name:
        raise ValueError('Skill metadata mismatch')
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+',metadata['version']) or not metadata['description']:
        raise ValueError('Invalid skill version/description')
    return Skill(name,metadata['version'],metadata['description'],body.strip(),hashlib.sha256(text.encode()).hexdigest())


def selected_skills(task,approved,*,user_query=''):
    if not approved or getattr(task,'agent',None)!='sales' or getattr(task,'kind',None)!='product_search':return ()
    # A bounded guidance selector, not a task router. Exact-ID detail reads keep
    # their existing deterministic path and do not incur an extra model call.
    # A planner rewrite may replace "compare" with "recommend". The actual
    # current user input can request guidance, never a file or a permission.
    query=getattr(task,'query','')+'\n'+user_query
    comparison=bool(re.search(r'对比|比较|区别|差异|\bcompar(?:e|ison)\b',query,re.I))
    names=['product-recommendation']+(['product-comparison'] if comparison else [])
    # Selection/candidate rules are shared; comparison adds only its own steps.
    return tuple(load_skill(name) for name in names if name in approved)

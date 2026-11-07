"""Developer-owned prompt assets. No user paths, templates or remote downloads.

Rule documents use one source line per rule. Folded assets concatenate lines to
preserve the original model prompt exactly; answer_plan keeps its original newlines.
"""
from functools import lru_cache
from pathlib import Path

_ROOT=Path(__file__).resolve().parents[2]/'prompts'
_ASSETS=frozenset({'scope','scope_repair','router','schema_repair','constraint_repair','inflight',
                   'tech_support','sales','billing','execution','final','evidence','answer_plan'})

@lru_cache(maxsize=16)
def read_prompt(name):
    if name not in _ASSETS:raise ValueError('Unknown prompt asset')
    path=(_ROOT/(name+'.md')).resolve()
    if not path.is_relative_to(_ROOT.resolve()):raise ValueError('Prompt path escapes prompt directory')
    text=path.read_text(encoding='utf-8')
    if not text.strip() or len(text)>20000:raise ValueError('Empty or oversized prompt asset')
    return text if name=='answer_plan' else ''.join(text.splitlines())

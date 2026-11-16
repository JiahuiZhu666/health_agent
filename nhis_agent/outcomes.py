"""Canonical, source-documented binary outcomes from the analysis registry.

No survey rows, estimates or credentials are loaded here. Public accessors return
independent metadata copies so presentation and language layers cannot mutate
the registered event coding used by the statistical engine.
"""
import copy
from functools import lru_cache
import json
from pathlib import Path
import re

REGISTRY_PATH = Path(__file__).resolve().parents[1] / 'config' / 'registry.json'
ACCESS_OUTCOMES = ('insured','uninsured','delayed_care_cost','unmet_care_cost')


@lru_cache(maxsize=1)
def _registered():
    registry=json.loads(REGISTRY_PATH.read_text(encoding='utf-8'))
    values=registry.get('outcomes') if isinstance(registry,dict) else None
    if not isinstance(values,dict) or not values:
        raise ValueError('The registered outcome metadata are unavailable.')
    for name,value in values.items():
        if (not isinstance(name,str) or not re.fullmatch(r'[a-z][a-z0-9_]*',name)
                or not isinstance(value,dict) or not isinstance(value.get('variable'),str)
                or not re.fullmatch(r'[A-Z][A-Z0-9_]*',value['variable'])):
            raise ValueError('The registry contains invalid outcome metadata.')
        sets=[]
        for key in ('one','zero','missing'):
            codes=value.get(key)
            if (not isinstance(codes,list) or not codes or any(type(code) is not int for code in codes)
                    or len(codes)!=len(set(codes))):
                raise ValueError('Outcome coding must use nonempty lists of unique integer codes.')
            sets.append(set(codes))
        if any(sets[first]&sets[second] for first,second in ((0,1),(0,2),(1,2))):
            raise ValueError('Positive, negative and missing outcome codes must be disjoint.')
        for key in ('label','definition','event','time_window','universe','codebook_url','limitation'):
            if not isinstance(value.get(key),str) or not value[key].strip():
                raise ValueError('Registered outcomes require complete definitions and source metadata.')
        if type(value.get('codebook_page')) is not int or value['codebook_page'] < 1:
            raise ValueError('Registered outcomes require a positive codebook page.')
    return values


def allowed_outcomes():
    """Return registered identifiers in registry order, retaining fair_poor first."""
    return tuple(_registered())


def get_outcome(outcome='fair_poor'):
    """Return source metadata, event coding and limitations for one outcome."""
    if not isinstance(outcome,str) or outcome not in _registered():
        raise ValueError('Unknown registered outcome.')
    return dict(copy.deepcopy(_registered()[outcome]),id=outcome)


def outcome_label(outcome='fair_poor'):
    return get_outcome(outcome)['label']

import copy
import json
from types import SimpleNamespace

import pytest

from jianying_adapter.preset_simplification import simplify_preset_motion


def case(tmp_path):
    path=tmp_path/'state.json'
    path.write_text(json.dumps({'current_authorizations':[{'option':'visual_packaging_revision','enabled':True,'source':'user'}]}))
    ctx=SimpleNamespace(plan={'project_state':str(path)})
    seg={'id':'s','material_id':'text','target_timerange':{'start':1,'duration':4},'clip':{'scale':{'x':1,'y':1}},'extra_material_refs':['anim','glow','fade']}
    draft={'tracks':[{'name':'chosen','type':'text','segments':[seg]},{'name':'other','type':'text','segments':[copy.deepcopy(seg)]}],
           'materials':{'material_animations':[{'id':'anim','animations':[{'type':'in','name':'glow'}]}],
                        'effects':[{'id':'glow','type':'bloom','panel_id':'text_glow'}]}}
    spec={'authorization_source':'user','design_reason':'restrained finance','mode':'plain_text_no_entrance_or_glow','track_names':['chosen']}
    return draft,spec,ctx


def test_simplify_shared_animation_only_on_selected_track(tmp_path):
    d,s,c=case(tmp_path);before=copy.deepcopy(d);out,r=simplify_preset_motion(d,s,c)
    assert d==before
    assert out['tracks'][1]==before['tracks'][1]
    seg=out['tracks'][0]['segments'][0]
    assert seg['clip']==before['tracks'][0]['segments'][0]['clip']
    assert seg['target_timerange']==before['tracks'][0]['segments'][0]['target_timerange']
    assert 'glow' not in seg['extra_material_refs'] and 'fade' in seg['extra_material_refs']
    assert out['materials']['material_animations'][-1]['animations']==[]


@pytest.mark.parametrize('failure',['auth','loop','unknown_track'])
def test_reject_atomically(tmp_path,failure):
    d,s,c=case(tmp_path)
    if failure=='auth':s['authorization_source']='not_user'
    if failure=='loop':d['materials']['material_animations'][0]['animations'][0]['type']='loop'
    if failure=='unknown_track':s['track_names']=['missing']
    before=copy.deepcopy(d)
    with pytest.raises(ValueError):simplify_preset_motion(d,s,c)
    assert d==before

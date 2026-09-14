import copy
from types import SimpleNamespace

import pytest

from jianying_adapter.broll_motion import animate_broll_transform
from jianying_adapter.shared_operations import shared_operation_handlers
from jianying_adapter.visual_planning import TECHNIQUE_OPERATIONS
from test_aroll_motion import draft_and_spec


def fixture():
    draft, spec = draft_and_spec()
    track = draft['tracks'][0]
    track['name'] = 'JY_BROLL_bar'
    track['segments'][0]['source_timerange']['start'] = 900_000
    spec.update(kind='animate_broll_transform', node_id='bar', track_name=track['name'])
    spec['keyframes'] = [
        {'time_offset_us': t, 'scale_x': 1, 'scale_y': 1, 'position_x': x/540, 'position_y': -.5}
        for t, x in [(0, 80), (100_000, 55), (200_000, 25), (300_000, 0)]
    ]
    context = SimpleNamespace(plan={'brolls': [{'node_id': 'bar', 'track_name': track['name'],
        'start_us': 100_000, 'end_us': 1_100_000, 'segment_count': 1}]})
    return draft, spec, context


def test_registered_motion_changes_only_native_transform_fields():
    draft, spec, context = fixture()
    before = copy.deepcopy(draft)
    report = shared_operation_handlers()[spec['kind']](draft, spec, context)
    assert shared_operation_handlers()[spec['kind']] is animate_broll_transform
    assert spec['kind'] in TECHNIQUE_OPERATIONS['keyframes']
    segment = draft['tracks'][0]['segments'][0]
    assert segment['common_keyframes'][0] == before['tracks'][0]['segments'][0]['common_keyframes'][0]
    groups = {g['property_type']: g for g in segment['common_keyframes'][1:]}
    assert set(groups) == {'KFTypePositionX', 'KFTypePositionY', 'KFTypeScaleX', 'KFTypeScaleY'}
    for prop, group in groups.items():
        assert [k['time_offset'] for k in group['keyframe_list']] == [0, 100_000, 200_000, 300_000]
        assert all(k['curveType'] == 'Line' for k in group['keyframe_list'])
    assert [k['values'][0] for k in groups['KFTypePositionX']['keyframe_list']] == pytest.approx([80/540, 55/540, 25/540, 0])
    assert segment['clip']['transform'] == {'x': 80/540, 'y': -.5}
    assert segment['uniform_scale'] == {'on': False, 'value': 1.0}
    assert report['timing_and_audio_unchanged'] and report['native_visual_qa'] == 'pending'
    for key in ('clip', 'common_keyframes', 'uniform_scale'):
        segment[key] = before['tracks'][0]['segments'][0][key]
    assert draft == before  # Includes material identities, all timers, audio and unrelated tracks.


@pytest.mark.parametrize('case', [
    'undeclared', 'duplicate_declaration', 'declaration_timing', 'wrong_node', 'wrong_name',
    'duplicate_track', 'wrong_track_type', 'multiple_segments', 'speed', 'source_duration',
    'reverse', 'rotation', 'speed_material', 'curve_speed', 'native_animation', 'visual_keyframe',
    'motion_reference', 'uniform_scale', 'missing_axis', 'nonzero_first', 'duplicate_time',
    'late_time', 'nonlinear', 'bad_space', 'zero_scale', 'nan', 'bool_time',
])
def test_invalid_or_conflicting_broll_is_rejected_without_partial_mutation(case):
    draft, spec, context = fixture()
    segment = draft['tracks'][0]['segments'][0]
    if case == 'undeclared': context.plan['brolls'] = []
    elif case == 'duplicate_declaration': context.plan['brolls'] *= 2
    elif case == 'declaration_timing': context.plan['brolls'][0]['end_us'] += 1
    elif case == 'wrong_node': spec['node_id'] = 'other'
    elif case == 'wrong_name': spec['track_name'] = 'JY_ROUGH_CUT_VIDEO'
    elif case == 'duplicate_track': draft['tracks'].append(copy.deepcopy(draft['tracks'][0]))
    elif case == 'wrong_track_type': draft['tracks'][0]['type'] = 'text'
    elif case == 'multiple_segments': draft['tracks'][0]['segments'].append(copy.deepcopy(segment))
    elif case == 'speed': segment['speed'] = 1.1
    elif case == 'source_duration': segment['source_timerange']['duration'] -= 1
    elif case == 'reverse': segment['reverse'] = True
    elif case == 'rotation': segment['clip']['rotation'] = 1
    elif case == 'speed_material': draft['materials']['speeds'][0]['speed'] = 1.1
    elif case == 'curve_speed': draft['materials']['speeds'][0]['curve_speed'] = {'points': [1]}
    elif case == 'native_animation':
        segment['extra_material_refs'].append('animation')
        draft['materials']['material_animations'] = [{'id': 'animation', 'animations': [{'type': 'in'}]}]
    elif case == 'visual_keyframe': segment['common_keyframes'].append({'property_type': 'KFTypePositionX'})
    elif case == 'motion_reference': segment['keyframe_refs'] = ['unknown']
    elif case == 'uniform_scale': segment['uniform_scale']['value'] = 2
    elif case == 'missing_axis': del spec['keyframes'][-1]['position_y']
    elif case == 'nonzero_first': spec['keyframes'][0]['time_offset_us'] = 1
    elif case == 'duplicate_time': spec['keyframes'][-1]['time_offset_us'] = 100_000
    elif case == 'late_time': spec['keyframes'][-1]['time_offset_us'] = 1_000_001
    elif case == 'nonlinear': spec['interpolation'] = 'bezier'
    elif case == 'bad_space': spec['position_space'] = 'px'
    elif case == 'zero_scale': spec['keyframes'][-1]['scale_y'] = 0
    elif case == 'nan': spec['keyframes'][-1]['position_x'] = float('nan')
    elif case == 'bool_time': spec['keyframes'][-1]['time_offset_us'] = True
    before = copy.deepcopy(draft)
    with pytest.raises(ValueError):
        animate_broll_transform(draft, spec, context)
    assert draft == before

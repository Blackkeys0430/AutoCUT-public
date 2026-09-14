"""Complete native presets and explicit scene effects share the material bucket."""
import copy
import json
from pathlib import Path

import pytest

from jianying_adapter.shared_operations import add_preset_group
from jianying_adapter.video_effects import add_video_effect, validate_video_effects
from jianying_adapter.visual_planning import validate_required_visual_techniques
from test_five_template_trial import _candidate, _inner, _text_slot
from test_video_effects import case, write


def mixed_case(tmp_path, count=3, *, attached=False):
    base, template, context = case(tmp_path)
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'structural fixture, not native playback evidence')
    base['materials']['videos'][0].update(path=str(source), width=1080, height=1920)
    write(Path(context.plan['base_draft']), base)
    sample, _ = add_video_effect(base, template, context)
    inner = _inner('A', ['original title'], duration=1_000_000)
    inner['materials']['video_effects'] = []
    for index in range(2):
        material = copy.deepcopy(sample['materials']['video_effects'][0])
        material['id'] = f'authored-effect-{index}'
        # An authored face effect in this bucket is part of the preset, not an
        # unsupported explicit VideoSceneEffectType operation.
        material['type'] = 'face_effect' if index else 'video_effect'
        segment = copy.deepcopy(sample['tracks'][-1]['segments'][0])
        segment.update(id=f'authored-segment-{index}', material_id=material['id'])
        inner['materials']['video_effects'].append(material)
        if attached:
            inner['tracks'][0]['segments'][0].setdefault('extra_material_refs', []).append(material['id'])
        else:
            inner['tracks'].append({'id': f'authored-track-{index}', 'type': 'effect',
                                    'attribute': 0, 'flag': 0, 'segments': [segment]})
    candidate, payload = _candidate('A', 'preset.json', [inner],
        {'text': [_text_slot('title', 0, 0, 'A-TEXT-MAT-0')]})
    write(tmp_path / 'preset.json', payload)
    write(tmp_path / 'registry.json', {'candidates': [candidate]})
    preset = {'id': 'preset', 'kind': 'add_preset_group', 'template_id': 'A',
              'node_id': 'title', 'start_us': 0, 'end_us': 1_000_000, 'slots': ['new title']}
    context.plan.update(preset_registry=str(tmp_path / 'registry.json'), preset_root=str(tmp_path),
                        presets=[preset], operations=[preset], visual_events=[])
    for index in range(count):
        spec = {**template, 'id': f'explicit-{index}', 'visual_event_id': f'event-{index}',
                'start_us': index * 200_000, 'end_us': (index + 1) * 200_000}
        context.plan['operations'].append(spec)
        context.plan['visual_events'].append({'id': spec['visual_event_id'], 'start_us': spec['start_us'],
            'end_us': spec['end_us'], 'audience_need': 'count only explicit native effects',
            'composition': 'synthetic threshold interval',
            'techniques': [{'kind': 'video_effect', 'operation_ids': [spec['id']]}]})
    state_path = Path(context.plan['project_state'])
    state = json.loads(state_path.read_text('utf-8'))
    state['required_visual_techniques'] = {'video_effect': 3}
    write(state_path, state)
    draft, _ = add_preset_group(base, preset, context)
    for spec in context.plan['operations'][1:]:
        draft, _ = add_video_effect(draft, spec, context)
    return context, draft


@pytest.mark.parametrize('count', [1, 2, 3, 4])
@pytest.mark.parametrize('attached', [False, True])
def test_bound_authored_effects_coexist_without_satisfying_explicit_minimum(tmp_path, count, attached):
    context, draft = mixed_case(tmp_path, count, attached=attached)
    before = copy.deepcopy(draft)
    report = validate_video_effects(context.plan, draft, plan_path=context.plan_path)
    assert report['ok'], report['errors']
    assert len(report['effects']) == count
    assert report['inherited_preset_effects']['material_count'] == 2
    required = validate_required_visual_techniques(context.plan, draft=draft,
        plan_path=context.plan_path, effect_report=report)
    assert required['requirements']['video_effect']['count'] == count
    assert required['ok'] == (count >= 3)
    assert not required['native_visual_verified'] and not required['creative_quality_verified']
    assert draft == before


@pytest.mark.parametrize('defect', ['missing_material', 'missing_track', 'duplicate_material',
    'duplicate_track', 'unbound_material', 'forged_track_name', 'extra_attachment',
    'wrong_resource_id', 'changed_parameters', 'changed_time', 'hidden_track',
    'missing_resource', 'wrong_bucket', 'missing_declaration', 'source_changed', 'effect_order'])
def test_inherited_effect_allowance_requires_exact_current_source_and_binding(tmp_path, defect):
    context, draft = mixed_case(tmp_path)
    material = next(m for m in draft['materials']['video_effects'] if not m['id'].startswith('JY_VFX_'))
    track = next(t for t in draft['tracks'] if t['type'] == 'effect' and t['name'].startswith('JY_PRESET_'))
    if defect == 'missing_material': draft['materials']['video_effects'].remove(material)
    elif defect == 'missing_track': draft['tracks'].remove(track)
    elif defect == 'duplicate_material': draft['materials']['video_effects'].append(copy.deepcopy(material))
    elif defect == 'duplicate_track': draft['tracks'].append(copy.deepcopy(track))
    elif defect == 'unbound_material':
        draft['materials']['video_effects'].append({**material, 'id': 'unbound'})
    elif defect == 'forged_track_name': track['name'] = 'JY_PRESET_A__NODE__fake_AUX_EFFECT_01'
    elif defect == 'extra_attachment': draft['tracks'][0]['segments'][0]['extra_material_refs'].append(material['id'])
    elif defect == 'wrong_resource_id': material['resource_id'] = 'other'
    elif defect == 'changed_parameters': material['adjust_params'][0]['value'] = .9
    elif defect == 'changed_time': track['segments'][0]['target_timerange']['start'] += 1
    elif defect == 'hidden_track': track['visible'] = False
    elif defect == 'effect_order':
        indices = [i for i, t in enumerate(draft['tracks']) if t['type'] == 'effect' and t['name'].startswith('JY_PRESET_')]
        a, b = indices
        draft['tracks'][a], draft['tracks'][b] = draft['tracks'][b], draft['tracks'][a]
    elif defect == 'missing_resource': material['path'] = str(tmp_path / 'absent')
    elif defect == 'wrong_bucket':
        draft['materials']['video_effects'].remove(material)
        draft['materials']['effects'] = [material]
    elif defect == 'missing_declaration':
        context.plan['operations'] = context.plan['operations'][1:]
        context.plan['presets'] = []
    elif defect == 'source_changed':
        path = tmp_path / 'preset.json'
        payload = json.loads(path.read_text('utf-8'))
        payload['materials']['drafts'][0]['draft']['materials']['video_effects'][0]['adjust_params'][0]['value'] = .9
        write(path, payload)
    report = validate_video_effects(context.plan, draft, plan_path=context.plan_path)
    assert not report['ok'], defect
    assert any('original preset video effect' in error for error in report['errors']), report


def test_local_native_id_and_render_renumbering_preserves_effects(tmp_path):
    context, draft = mixed_case(tmp_path)
    mapping = {}
    for material in draft['materials']['video_effects']:
        if material['id'].startswith('JY_VFX_'): continue
        original = material['id']
        material['id'] = 'renamed-' + original
        mapping[original] = material['id']
    for track in draft['tracks']:
        if not track['name'].startswith('JY_PRESET_'): continue
        for segment in track['segments']:
            segment['material_id'] = mapping.get(segment['material_id'], segment['material_id'])
            segment['id'] = 'renamed-' + segment['id']
            segment.update(render_index=901, track_render_index=92)
    report = validate_video_effects(context.plan, draft, plan_path=context.plan_path)
    assert report['ok'], report['errors']

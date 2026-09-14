"""Required delivery uses current state and actual native output, not opt-in flags."""
import copy
import json
from pathlib import Path

import pytest

from jianying_adapter.candidate_plan import (
    CandidateAssembler, get_shared_operation_registry, validate_candidate_plan,
    validate_timeline_equivalence, validate_writer_contract,
)
from jianying_adapter.visual_planning import required_visual_minima, validate_required_visual_techniques
from jianying_adapter.video_mask import apply_video_mask
from jianying_adapter.video_effects import add_video_effect
from test_candidate_stages import _plan, _write
from test_video_mask import draft_and_spec
from test_video_effects import case


@pytest.mark.parametrize('bad', [None, {}, [], True, {'mask': 0}, {'mask': False},
                                {'mask': -1}, {'mask': 1.5}, {'mask': '1'}, {'text_animation': 1}])
def test_invalid_minimum_cannot_cancel_requirement(bad):
    with pytest.raises(ValueError):
        required_visual_minima({'required_visual_techniques': bad})


@pytest.mark.parametrize('mode', ['preview', 'seal', 'canonical'])
def test_plan_omission_and_options_cannot_override_state(tmp_path, monkeypatch, mode):
    from jianying_adapter import candidate_plan
    path = _plan(tmp_path)
    monkeypatch.setattr(candidate_plan, 'exact_process_identifier', lambda _: {'ok': True})
    plan = json.loads(path.read_text('utf-8'))
    state_path = Path(plan['project_state'])
    state = json.loads(state_path.read_text('utf-8'))
    state['required_visual_techniques'] = {'mask': 1, 'video_effect': 1}
    _write(state_path, state)
    plan['required_visual_techniques'] = {}  # A planner's copy is not authority.
    plan['options'].update(masks={'enabled': True, 'authorization_source': 'user'},
                           video_effects={'enabled': True, 'authorization_source': 'user'})
    result = validate_candidate_plan(plan, plan_path=path, mode=mode)
    assert not result.ok
    for kind in ('mask', 'video_effect'):
        assert any(f'必做画面 {kind}:' in error for error in result.errors)
    _write(path, plan)
    with pytest.raises(RuntimeError, match='必做画面'):
        CandidateAssembler(path, get_shared_operation_registry(),
                           final_validator=lambda *_: {'ok': True}).preview()
    assert not Path(plan['preview_output']).exists()
    writer = validate_writer_contract(plan, plan_path=path, preview_test=True, profile_kind='test')
    assert not writer['ok'] and any('必做画面' in error for error in writer['errors'])


def native_case(tmp_path, scope='segment'):
    draft, effect, ctx = case(tmp_path, scope)
    _, mask = draft_and_spec(tmp_path)
    media_path = tmp_path / 'source.mp4'
    media_path.write_bytes(b'synthetic source, not native playback evidence')
    draft['materials']['videos'][0].update(path=str(media_path), width=1080, height=1920)
    draft['materials']['common_mask'] = []
    segment = draft['tracks'][0]['segments'][0]
    segment['extra_material_refs'] = []
    mask.update(track_name=draft['tracks'][0]['name'], segment_id=segment['id'])
    state_path = Path(ctx.plan['project_state'])
    state = json.loads(state_path.read_text('utf-8'))
    state['required_visual_techniques'] = {'mask': 1, 'video_effect': 1}
    _write(state_path, state)
    ctx.plan['operations'].insert(0, mask)
    ctx.plan['visual_events'][0]['techniques'].append({'kind': 'mask', 'operation_ids': ['mask']})
    _write(Path(ctx.plan['base_draft']), draft)
    apply_video_mask(draft, mask, ctx)
    result, _ = add_video_effect(draft, effect, ctx)
    return ctx, result


@pytest.mark.parametrize('scope', ['global', 'segment'])
def test_required_native_mask_and_effect_pass_but_do_not_claim_visual_qa(tmp_path, scope):
    ctx, draft = native_case(tmp_path, scope)
    before = copy.deepcopy(draft)
    report = validate_timeline_equivalence(ctx.plan, draft, plan_path=ctx.plan_path)
    assert report['ok'], report['errors']
    required = report['required_visual_techniques']
    assert {k: v['count'] for k, v in required['requirements'].items()} == {'mask': 1, 'video_effect': 1}
    assert not required['native_visual_verified'] and not required['creative_quality_verified']
    assert before == draft


@pytest.mark.parametrize('defect', ['missing_mask', 'detached_mask', 'wrong_mask_resource', 'missing_effect',
                                  'hidden_video', 'zero_alpha', 'wrong_interval', 'text_only', 'duplicate_binding'])
def test_final_readback_cannot_count_labels_or_missing_hidden_output(tmp_path, defect):
    ctx, draft = native_case(tmp_path)
    segment = draft['tracks'][0]['segments'][0]
    if defect == 'missing_mask': draft['materials']['common_mask'] = []
    if defect == 'detached_mask': segment['extra_material_refs'].remove(draft['materials']['common_mask'][0]['id'])
    if defect == 'wrong_mask_resource': draft['materials']['common_mask'][0]['resource_id'] = 'other'
    if defect == 'missing_effect': draft['materials']['video_effects'] = []
    if defect == 'hidden_video': draft['tracks'][0]['visible'] = False
    if defect == 'zero_alpha': segment['clip']['alpha'] = 0
    if defect == 'wrong_interval': ctx.plan['visual_events'][0].update(start_us=2_000_000, end_us=3_000_000)
    if defect == 'text_only':
        ctx.plan['operations'] = [{'id': 'text', 'kind': 'apply_text_animation'}]
        ctx.plan['visual_events'][0]['techniques'] = [{'kind': 'text_animation', 'operation_ids': ['text']}]
    if defect == 'duplicate_binding':
        other = copy.deepcopy(ctx.plan['visual_events'][0]); other['id'] = 'duplicate'
        ctx.plan['visual_events'].append(other)
        state = json.loads(Path(ctx.plan['project_state']).read_text('utf-8'))
        state['required_visual_techniques']['mask'] = 2
        _write(Path(ctx.plan['project_state']), state)
    report = validate_timeline_equivalence(ctx.plan, draft, plan_path=ctx.plan_path)
    assert not report['ok']
    assert not report['required_visual_techniques']['ok'], report


def test_fade_in_counts_actual_positive_interval_not_initial_alpha(tmp_path):
    ctx, draft = native_case(tmp_path)
    segment = draft['tracks'][0]['segments'][0]
    segment['clip']['alpha'] = 0
    segment['common_keyframes'].append({'property_type': 'KFTypeAlpha', 'keyframe_list': [
        {'time_offset': 0, 'values': [0]}, {'time_offset': 200_000, 'values': [0]},
        {'time_offset': 400_000, 'values': [1]}]})
    result = validate_required_visual_techniques(ctx.plan, draft=draft, plan_path=ctx.plan_path)
    assert result['ok'], result['errors']
    assert result['requirements']['mask']['operations'][0]['intervals_us'] == [[200_000, 1_000_000]]


def test_unrequested_legacy_plan_has_no_new_quota():
    result = validate_required_visual_techniques({}, state={}, draft={})
    assert result['ok'] and result['requirements'] == {}


@pytest.mark.parametrize('declared, expected', [
    (None, {'mask': 1, 'video_effect': 3}),
    ({'mask': 1}, {'mask': 1, 'video_effect': 3}),
    ({'mask': 1, 'video_effect': 1}, {'mask': 1, 'video_effect': 3}),
    ({'mask': 2, 'video_effect': 5}, {'mask': 2, 'video_effect': 5}),
])
def test_new_v2_video_has_non_reducible_floor(declared, expected):
    state = {'visual_planning_min_version': 2}
    if declared is not None:
        state['required_visual_techniques'] = declared
    assert required_visual_minima(state) == expected
    result = validate_required_visual_techniques({}, state=state, draft={})
    assert not result['ok']  # Omitting the requirement/operations cannot bypass it.

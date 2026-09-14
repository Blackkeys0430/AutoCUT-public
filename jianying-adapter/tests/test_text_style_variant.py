import copy
import json
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from jianying_adapter.text_style_variant import apply_text_style_variant


def setup_case(tmp_path):
    state = tmp_path / 'state.json'
    state.write_text(json.dumps({'current_authorizations': [{'option': 'text_style_revision', 'enabled': True, 'source': 'user'}]}))
    ctx = SimpleNamespace(plan={'project_state': str(state), 'text_style_authorization_source': 'user'})
    draft = {'tracks': [{'name': 'heading', 'type': 'text', 'segments': [{'material_id': 'm', 'target_timerange': {'start': 20, 'duration': 80}, 'extra_material_refs': ['animation'], 'common_keyframes': [{'values': [1, 2]}]}]}],
             'materials': {'texts': [{'id': 'm', 'content': json.dumps({'text': '标题', 'styles': [{'size': 15, 'range': [0, 2], 'italic': True, 'shadows': [{'alpha': .8, 'distance': 5}]}]})}], 'material_animations': [{'id': 'animation', 'animations': [{'duration': 30}]}]}}
    spec = {'authorization_source': 'user', 'design_reason': 'readable heading', 'tracks': [{'track_name': 'heading', 'color': [.9, .8, .7], 'italic': False, 'shadow_alpha': .4}]}
    return draft, spec, ctx


def test_style_preserves_words_geometry_motion_and_source(tmp_path):
    draft, spec, ctx = setup_case(tmp_path)
    before = copy.deepcopy(draft)
    result, report = apply_text_style_variant(draft, spec, ctx)
    assert draft == before
    assert result['tracks'] == before['tracks']
    assert result['materials']['material_animations'] == before['materials']['material_animations']
    payload = json.loads(result['materials']['texts'][0]['content'])
    assert payload['text'] == '标题'
    assert payload['styles'][0]['size'] == 15
    assert payload['styles'][0]['range'] == [0, 2]
    assert payload['styles'][0]['italic'] is False
    assert payload['styles'][0]['shadows'][0] == {'alpha': .4, 'distance': 5}
    assert report['native_motion_modified'] is False


@pytest.mark.parametrize('alpha', [0, .4])
def test_shadow_edits_sync_existing_run_and_legacy_fields(tmp_path, alpha):
    draft, spec, ctx = setup_case(tmp_path)
    draft['materials']['texts'][0].update(has_shadow=True, shadow_alpha=.547, shadow_distance=10)
    spec['tracks'] = [{'track_name': 'heading', 'shadow_alpha': alpha, 'shadow_distance': 3}]
    before = copy.deepcopy(draft)
    result, _ = apply_text_style_variant(draft, spec, ctx)
    material = result['materials']['texts'][0]
    assert material['shadow_alpha'] == alpha
    assert material['shadow_distance'] == 3
    assert material['has_shadow'] is (alpha > 0)
    assert json.loads(material['content'])['styles'][0]['shadows'] == [{'alpha': alpha, 'distance': 3}]
    assert result['tracks'] == before['tracks'] and draft == before


def test_shadow_parameters_not_requested_remain_unchanged(tmp_path):
    draft, spec, ctx = setup_case(tmp_path)
    draft['materials']['texts'][0].update(has_shadow=True, shadow_alpha=.547, shadow_distance=10)
    spec['tracks'] = [{'track_name': 'heading', 'shadow_distance': 3}]
    result, _ = apply_text_style_variant(draft, spec, ctx)
    material = result['materials']['texts'][0]
    assert material['shadow_alpha'] == .547 and material['has_shadow'] is True
    assert json.loads(material['content'])['styles'][0]['shadows'][0]['alpha'] == .8
    spec['tracks'] = [{'track_name': 'heading', 'italic': False}]
    result, _ = apply_text_style_variant(draft, spec, ctx)
    material = result['materials']['texts'][0]
    assert (material['shadow_alpha'], material['shadow_distance'], material['has_shadow']) == (.547, 10, True)
    assert json.loads(material['content'])['styles'][0]['shadows'] == [{'alpha': .8, 'distance': 5}]


@pytest.mark.parametrize('legacy_fields', [False, True])
def test_shadow_edit_does_not_create_absent_shadow_effect(tmp_path, legacy_fields):
    draft, spec, ctx = setup_case(tmp_path)
    material = draft['materials']['texts'][0]
    payload = json.loads(material['content'])
    payload['styles'][0].pop('shadows')
    material['content'] = json.dumps(payload)
    if legacy_fields:
        material.update(has_shadow=False, shadow_alpha=0, shadow_distance=0)
    spec['tracks'] = [{'track_name': 'heading', 'shadow_alpha': .7, 'shadow_distance': 3}]
    result, _ = apply_text_style_variant(draft, spec, ctx)
    material = result['materials']['texts'][0]
    assert 'shadows' not in json.loads(material['content'])['styles'][0]
    if legacy_fields:
        assert material['has_shadow'] is False
    else:
        assert not any(key in material for key in ['has_shadow', 'shadow_alpha', 'shadow_distance'])


@pytest.mark.parametrize('width', [0, 18, 100])
def test_border_matches_sdk_and_survives_span_splitting_without_motion_changes(tmp_path, width):
    draft, spec, ctx = setup_case(tmp_path)
    payload = json.loads(draft['materials']['texts'][0]['content'])
    payload['styles'][0]['font'] = {'id': 'unchanged', 'path': 'unchanged-font.ttf'}
    draft['materials']['texts'][0]['content'] = json.dumps(payload)
    border = {'color': [0, 0, 0], 'alpha': 1, 'width': width}
    spec['tracks'] = [{'track_name': 'heading', 'border': border,
                       'spans': [{'range': [1, 2], 'text': '题', 'bold': True}]}]
    before = copy.deepcopy(draft)
    result, report = apply_text_style_variant(draft, spec, ctx)
    from pyJianYingDraft.text_segment import TextBorder
    expected = TextBorder(color=(0, 0, 0), alpha=1, width=width).export_json()
    actual = json.loads(result['materials']['texts'][0]['content'])
    assert actual['text'] == payload['text']
    assert len(actual['styles']) == 2
    assert all(run['strokes'] == [expected] for run in actual['styles'])
    assert all(run['font'] == payload['styles'][0]['font'] and run['size'] == 15 for run in actual['styles'])
    assert result['tracks'] == before['tracks']
    assert result['materials']['material_animations'] == before['materials']['material_animations']
    assert report['native_motion_modified'] is False and draft == before


@pytest.mark.parametrize('border', [None, {}, {'color': [0, 0, 0], 'alpha': 1, 'width': 18, 'extra': True},
    {'color': [0, 0], 'alpha': 1, 'width': 18}, {'color': [True, 0, 0], 'alpha': 1, 'width': 18},
    {'color': [float('nan'), 0, 0], 'alpha': 1, 'width': 18}, {'color': [2, 0, 0], 'alpha': 1, 'width': 18},
    *({'color': [0, 0, 0], 'alpha': 1, 'width': width} for width in [-1, 101, True, '18', float('inf')]),
    *({'color': [0, 0, 0], 'alpha': alpha, 'width': 18} for alpha in [-.1, 1.1, True, float('nan')])])
def test_invalid_border_rejected_without_mutation(tmp_path, border):
    draft, spec, ctx = setup_case(tmp_path)
    spec['tracks'][0]['border'] = border
    before = copy.deepcopy(draft)
    with pytest.raises(ValueError, match='border'):
        apply_text_style_variant(draft, spec, ctx)
    assert draft == before


def test_border_conflicts_with_removing_strokes(tmp_path):
    draft, spec, ctx = setup_case(tmp_path)
    spec['tracks'][0].update(border={'color': [0, 0, 0], 'alpha': 1, 'width': 18}, remove_strokes=True)
    before = copy.deepcopy(draft)
    with pytest.raises(ValueError, match='conflicts'):
        apply_text_style_variant(draft, spec, ctx)
    assert draft == before


def test_segment_style_changes_only_selected_sentence_in_shared_caption_track(tmp_path):
    draft, spec, ctx = setup_case(tmp_path)
    first = draft['tracks'][0]['segments'][0]
    first['id'] = 'caption-1'
    second = copy.deepcopy(first)
    second.update(id='caption-2', material_id='m2')
    draft['tracks'][0]['segments'].append(second)
    draft['materials']['texts'].append({'id': 'm2', 'content': json.dumps({
        'text': '40毫升', 'styles': [{'range': [0, 4], 'size': 15}]})})
    spec['tracks'] = [{'track_name': 'heading', 'segment_id': 'caption-2',
                       'spans': [{'range': [0, 2], 'text': '40', 'size_multiplier': 1.4}]}]
    before = copy.deepcopy(draft)
    result, _ = apply_text_style_variant(draft, spec, ctx)
    assert result['tracks'] == before['tracks']
    assert result['materials']['texts'][0] == before['materials']['texts'][0]
    payload = json.loads(result['materials']['texts'][1]['content'])
    assert payload['text'] == '40毫升'
    assert [r['size'] for r in payload['styles']] == [21, 15]
    assert draft == before


@pytest.mark.parametrize('bad_id', ['', None, 42, 'missing'])
def test_invalid_segment_target_rejects_atomically(tmp_path, bad_id):
    draft, spec, ctx = setup_case(tmp_path)
    spec['tracks'][0]['segment_id'] = bad_id
    before = copy.deepcopy(draft)
    with pytest.raises(ValueError, match='segment'):
        apply_text_style_variant(draft, spec, ctx)
    assert draft == before


def test_segment_style_rejects_shared_material_in_same_track(tmp_path):
    draft, spec, ctx = setup_case(tmp_path)
    draft['tracks'][0]['segments'][0]['id'] = 'caption-1'
    second = copy.deepcopy(draft['tracks'][0]['segments'][0])
    second['id'] = 'caption-2'
    draft['tracks'][0]['segments'].append(second)
    spec['tracks'][0]['segment_id'] = 'caption-1'
    before = copy.deepcopy(draft)
    with pytest.raises(ValueError, match='shared'):
        apply_text_style_variant(draft, spec, ctx)
    assert draft == before


def test_whole_track_and_segment_targets_cannot_overlap(tmp_path):
    draft, spec, ctx = setup_case(tmp_path)
    draft['tracks'][0]['segments'][0]['id'] = 'caption-1'
    spec['tracks'].append({'track_name': 'heading', 'segment_id': 'caption-1', 'italic': True})
    before = copy.deepcopy(draft)
    with pytest.raises(ValueError, match='nonoverlapping'):
        apply_text_style_variant(draft, spec, ctx)
    assert draft == before


@pytest.mark.parametrize('alignment', [0, 1, 2])
def test_explicit_native_alignment_preserves_text_and_motion(tmp_path, alignment):
    draft, spec, ctx = setup_case(tmp_path)
    draft['materials']['texts'][0]['alignment'] = 1
    before = copy.deepcopy(draft)
    spec['tracks'] = [{'track_name': 'heading', 'alignment': alignment}]
    result, _ = apply_text_style_variant(draft, spec, ctx)
    expected = copy.deepcopy(before)
    expected['materials']['texts'][0]['alignment'] = alignment
    assert json.loads(result['materials']['texts'][0]['content']) == json.loads(before['materials']['texts'][0]['content'])
    expected['materials']['texts'][0]['content'] = result['materials']['texts'][0]['content']
    assert result == expected
    assert draft == before


@pytest.mark.parametrize('alignment', [-1, 3, 0.0, True, 'left', None])
def test_invalid_alignment_rejects_without_mutation(tmp_path, alignment):
    draft, spec, ctx = setup_case(tmp_path)
    before = copy.deepcopy(draft)
    spec['tracks'][0]['alignment'] = alignment
    with pytest.raises(ValueError, match='alignment'):
        apply_text_style_variant(draft, spec, ctx)
    assert draft == before


@pytest.mark.parametrize('change', [{'color': [float('nan'), 0, 0]}, {'font': {'path': 'missing', 'id': 'x'}}, {'track_name': 'absent'}, {'size': 2}])
def test_rejects_unsafe_or_unsupported_styles_without_mutation(tmp_path, change):
    draft, spec, ctx = setup_case(tmp_path)
    before = copy.deepcopy(draft)
    spec['tracks'][0].update(change)
    with pytest.raises(ValueError):
        apply_text_style_variant(draft, spec, ctx)
    assert draft == before


def test_rejects_shared_material_and_missing_authorization(tmp_path):
    draft, spec, ctx = setup_case(tmp_path)
    draft['tracks'].append({'name': 'other', 'segments': [{'material_id': 'm'}]})
    with pytest.raises(ValueError, match='shared'):
        apply_text_style_variant(draft, spec, ctx)
    spec['authorization_source'] = 'invented'
    with pytest.raises(ValueError, match='mismatch'):
        apply_text_style_variant(draft, spec, ctx)


def test_span_hierarchy_preserves_sentence_and_native_motion(tmp_path):
    draft, spec, ctx = setup_case(tmp_path)
    spec['tracks'][0]['spans'] = [{'range': [1, 2], 'text': '题', 'size_multiplier': 1.5, 'color': [.7, .8, .9], 'bold': True}]
    before = copy.deepcopy(draft)
    result, _ = apply_text_style_variant(draft, spec, ctx)
    payload = json.loads(result['materials']['texts'][0]['content'])
    assert payload['text'] == '标题'
    assert [(s['range'], s['size']) for s in payload['styles']] == [([0, 1], 15), ([1, 2], 22.5)]
    assert payload['styles'][1]['fill']['content']['solid']['color'] == [.7, .8, .9]
    assert result['tracks'] == before['tracks']
    assert result['materials']['material_animations'] == before['materials']['material_animations']
    assert draft == before


@pytest.mark.parametrize('spans', [
    [{'range': [0, 2], 'text': '错误'}],
    [{'range': [0, 3], 'text': '标题'}],
    [{'range': [0, 1], 'text': '标'}, {'range': [0, 2], 'text': '标题'}],
    [{'range': [0, 1], 'text': '标', 'size_multiplier': float('nan')}],
    [{'range': [0, 1], 'text': '标', 'font': {'path': 'missing', 'id': 'x'}}],
])
def test_invalid_span_rejects_atomically(tmp_path, spans):
    draft, spec, ctx = setup_case(tmp_path)
    spec['tracks'][0]['spans'] = spans
    before = copy.deepcopy(draft)
    with pytest.raises(ValueError):
        apply_text_style_variant(draft, spec, ctx)
    assert draft == before


def test_span_crosses_existing_run_boundary(tmp_path):
    draft, spec, ctx = setup_case(tmp_path)
    payload = json.loads(draft['materials']['texts'][0]['content'])
    first = payload['styles'][0]
    second = copy.deepcopy(first)
    first['range'], second['range'], second['size'] = [0, 1], [1, 2], 10
    payload['styles'].append(second)
    draft['materials']['texts'][0]['content'] = json.dumps(payload)
    spec['tracks'][0]['spans'] = [{'range': [0, 2], 'text': '标题', 'size_multiplier': 1.2}]
    result, _ = apply_text_style_variant(draft, spec, ctx)
    assert [s['size'] for s in json.loads(result['materials']['texts'][0]['content'])['styles']] == [18, 12]


def test_reset_native_tracking_preserves_other_materials_and_motion(tmp_path):
    draft, spec, ctx = setup_case(tmp_path)
    draft['materials']['texts'][0]['letter_spacing'] = .2
    draft['materials']['texts'].append({'id': 'unused', 'letter_spacing': .5})
    before = copy.deepcopy(draft)
    spec['tracks'][0] = {'track_name': 'heading', 'letter_spacing': 0}
    result, _ = apply_text_style_variant(draft, spec, ctx)
    expected = copy.deepcopy(before)
    expected['materials']['texts'][0]['letter_spacing'] = 0.0
    expected['materials']['texts'][0]['content'] = json.dumps(
        json.loads(expected['materials']['texts'][0]['content']), ensure_ascii=False)
    assert result == expected
    assert draft == before


@pytest.mark.parametrize('spacing', [True, None, '0', float('nan'), float('inf'), -.1, .2])
def test_reject_unverified_tracking_values_atomically(tmp_path, spacing):
    draft, spec, ctx = setup_case(tmp_path)
    before = copy.deepcopy(draft)
    spec['tracks'][0]['letter_spacing'] = spacing
    with pytest.raises(ValueError, match='letter_spacing'):
        apply_text_style_variant(draft, spec, ctx)
    assert draft == before


@pytest.mark.parametrize('font_id', ['', 'selected-resource'])
def test_font_replacement_removes_previous_resource_identity(tmp_path, font_id):
    font_path = Path('C:/Windows/Fonts/arial.ttf')
    if not font_path.is_file():
        pytest.skip('Windows TTF fixture font unavailable')
    draft, spec, ctx = setup_case(tmp_path)
    material = draft['materials']['texts'][0]
    payload = json.loads(material['content'])
    payload['text'] = 'AB'
    payload['styles'][0]['font'] = {'id': 'old-id', 'path': 'old-path'}
    material.update(content=json.dumps(payload), font_id='old-id', font_path='old-path',
                    font_resource_id='old-resource', fonts=[{'resource_id': 'old-resource'}],
                    font_name='Old Font', font_third_resource_id='old-third')
    spec['tracks'][0] = {'track_name': 'heading', 'font': {
        'id': font_id, 'path': str(font_path), 'sha256': hashlib.sha256(font_path.read_bytes()).hexdigest()}}
    before = copy.deepcopy(draft)
    result, _ = apply_text_style_variant(draft, spec, ctx)
    actual = result['materials']['texts'][0]
    assert actual['font_id'] == actual['font_resource_id'] == font_id
    assert actual['font_path'] == str(font_path)
    assert actual['fonts'] == []
    assert actual['font_name'] == actual['font_third_resource_id'] == ''
    assert json.loads(actual['content'])['styles'][0]['font'] == {'id': font_id, 'path': str(font_path)}
    assert result['tracks'] == before['tracks']
    assert result['materials']['material_animations'] == before['materials']['material_animations']
    assert draft == before

import copy
import json
from pathlib import Path

import pytest

from jianying_adapter.final_layout import validate_final_layout, validate_final_text, _simultaneous_overlap
from jianying_adapter.preset_preflight import preflight_selection


def case(canvas=(1080, 1920)):
    font = Path('C:/Windows/Fonts/msyh.ttc')
    if not font.exists():
        pytest.skip('Windows layout fixture font unavailable')
    plan = {'project_format': {'visual_planning_version': 1}, 'ordinary_caption_track_name': 'caption',
            'target': {'width': canvas[0], 'height': canvas[1]}}
    material = {'id': 'm', 'content': json.dumps({'text': '清楚', 'styles': [{'range': [0, 2], 'size': 14, 'font': {'path': str(font)}}]})}
    segment = {'id': 's', 'material_id': 'm', 'target_timerange': {'start': 0, 'duration': 1000000},
               'clip': {'scale': {'x': 1, 'y': 1}, 'transform': {'x': 0, 'y': -.5}}}
    draft = {'canvas_config': dict(plan['target']), 'materials': {'texts': [material]}, 'tracks': [{'name': 'caption', 'type': 'text', 'segments': [segment]}]}
    return draft, plan


@pytest.mark.parametrize('version', [1, 2])
def test_self_declared_eighty_char_limit_cannot_prove_final_capacity(version):
    draft, plan = case()
    plan['project_format']['visual_planning_version'] = version
    text = '容量测试'*20
    payload = json.loads(draft['materials']['texts'][0]['content'])
    payload['text'], payload['styles'][0]['range'] = text, [0, len(text)]
    draft['materials']['texts'][0]['content'] = json.dumps(payload)
    before = copy.deepcopy(draft)
    result = validate_final_layout(draft, plan)
    assert not result['ok']
    assert any('outside safe area' in x for x in result['errors'])
    assert draft == before


@pytest.mark.parametrize('version', [1, 2])
def test_collision_between_different_text_tracks_is_checked(version, tmp_path):
    draft, plan = case()
    add_platform_reference(plan, tmp_path)
    plan['project_format']['visual_planning_version'] = version
    second_text(draft)
    other = draft['tracks'][-1]
    other['name'] = 'other_preset'
    result = validate_final_layout(draft, plan)
    assert not result['ok'] and result['checked_visible_pairs'] == 1
    other['segments'][0]['target_timerange']['start'] = 1000000
    assert validate_final_layout(draft, plan)['ok']


def add_platform_reference(plan, tmp_path, zones=None):
    from PIL import Image
    image = tmp_path / 'fixture-player.png'
    Image.new('RGB', (540, 960), 'gray').save(image)
    plan.setdefault('layout_checks', {})['platform_ui'] = {
        'platform': 'test fixture', 'basis': 'synthetic player coordinates for unit tests',
        'image_path': str(image), 'video_rect_px': [0, 0, 540, 960],
        'regions': zones or [{'id': 'top', 'bbox_px': [0, 0, 540, 25]},
                            {'id': 'right', 'bbox_px': [510, 350, 540, 850]},
                            {'id': 'bottom', 'bbox_px': [0, 935, 540, 960]}]}


@pytest.mark.parametrize('layout', [{}, {'platform_ui': None}])
def test_v2_requires_actual_platform_reference_instead_of_guessed_pixel_defaults(layout):
    draft, plan = case()
    plan['project_format']['visual_planning_version'] = 2
    plan['layout_checks'] = layout
    report = validate_final_layout(draft, plan)
    assert not report['ok']
    assert report['platform_ui_check']['status'] == 'pending_platform_ui_reference'


def test_platform_ui_detects_crossing_between_safe_endpoints_and_cannot_be_waived(tmp_path):
    draft, plan = case()
    plan['project_format']['visual_planning_version'] = 2
    add_platform_reference(plan, tmp_path, [{'id': 'button', 'bbox_px': [260, 650, 280, 800]}])
    segment = draft['tracks'][0]['segments'][0]
    segment['common_keyframes'] = [motion('KFTypePositionX', [(0, -.7), (1_000_000, .7)])]
    plan['layout_checks']['allowed_overlaps'] = [{'tracks': ['caption', 'platform:button'], 'reason': 'must not waive platform UI'}]
    report = validate_final_layout(draft, plan)
    assert not report['ok']
    assert report['platform_ui_check']['collisions'] == [['caption', 'platform:button']]
    assert report['platform_ui_check']['regions'][0]['bbox'] == [520, 1300, 560, 1600]


@pytest.mark.parametrize('decoration', ['stroke', 'shadow', 'background'])
def test_visible_edges_are_checked_beyond_the_text_center_and_glyphs(tmp_path, decoration):
    draft, plan = case()
    baseline = validate_final_layout(draft, plan)['text_segments'][0]['bbox']
    edge = baseline[2] + 2
    add_platform_reference(plan, tmp_path, [{'id': 'edge', 'bbox_px': [edge/2, 650, (edge+10)/2, 800]}])
    assert validate_final_layout(draft, plan)['ok']
    material = draft['materials']['texts'][0]
    content = json.loads(material['content'])
    if decoration == 'stroke':
        content['styles'][0]['strokes'] = [{'width': .2}]
    elif decoration == 'shadow':
        content['styles'][0]['shadows'] = [{'alpha': 1, 'distance': 5, 'diffuse': .02}]
    else:
        material.update(background_color='#000000', background_alpha=1)
        report = validate_final_layout(draft, plan)
        assert report['platform_ui_check']['status'] == 'partial_geometry'
        plan['layout_checks']['visible_bounds'] = [{'track_name': 'caption', 'segment_id': 's',
            'bbox': [baseline[0]-20, baseline[1]-10, baseline[2]+20, baseline[3]+10],
            'basis': 'fixture full background envelope'}]
    material['content'] = json.dumps(content)
    assert not validate_final_layout(draft, plan)['ok']


def test_subject_motion_regions_are_checked_against_platform_ui(tmp_path):
    draft, plan = case()
    add_platform_reference(plan, tmp_path, [{'id': 'button', 'bbox_px': [450, 150, 520, 300]}])
    plan['layout_checks']['protected_regions'] = [{'id': 'raised-product', 'basis': 'fixture product extent',
        'start_us': 0, 'end_us': 1_000_000, 'geometry_keyframes': [
            {'time_us': 0, 'bbox': [400, 350, 500, 450]},
            {'time_us': 500_000, 'bbox': [920, 350, 1020, 450]},
            {'time_us': 1_000_000, 'bbox': [400, 350, 500, 450]}]}]
    report = validate_final_layout(draft, plan)
    assert not report['ok']
    assert ['protected:raised-product', 'platform:button'] in report['platform_ui_check']['collisions']


def test_native_animation_without_visible_envelope_is_reported_unchecked(tmp_path):
    draft, plan = case()
    add_platform_reference(plan, tmp_path)
    draft['materials']['material_animations'] = [{'id': 'native', 'animations': [{'type': 'in', 'duration': 100000}]}]
    draft['tracks'][0]['segments'][0]['extra_material_refs'] = ['native']
    report = validate_final_layout(draft, plan)
    assert report['ok']
    assert report['platform_ui_check']['status'] == 'partial_geometry'
    assert report['platform_ui_check']['unchecked'][0]['part'] == 'native_animation_envelope'


@pytest.mark.parametrize('plan', [
    {}, {'project_format': {}},
    {'project_format': {'visual_planning_version': None}},
    {'project_format': {'visual_planning_version': 0}},
])
def test_legacy_layout_contract_remains_inspectable_without_geometry(plan):
    assert validate_final_layout({}, plan) == {
        'ok': True, 'status': 'not_enabled_legacy_contract', 'native_visual_verified': False}


@pytest.mark.parametrize('version', [True, False, 1.0, 2.0, '1', '2', -1, 3])
def test_invalid_visual_planning_version_is_not_a_geometry_bypass(version):
    report = validate_final_layout({}, {'project_format': {'visual_planning_version': version}})
    assert not report['ok']
    assert 'visual_planning_version' in report['errors'][0]
    assert report['native_visual_verified'] is False


def test_explicit_text_unit_preserves_default_and_detects_previously_missed_overlap():
    draft, plan = case((1920, 1080))
    second = second_text(draft)
    second['clip']['transform']['x'] = .2  # 192px apart; old 140px boxes fit.
    before = copy.deepcopy(draft)
    default = validate_final_layout(draft, plan)
    assert default['ok'] and default['text_unit_to_px'] == 5.0
    plan['layout_checks'] = {'text_unit_to_px': 5.0}
    assert validate_final_layout(draft, plan) == default
    plan['layout_checks']['text_unit_to_px'] = 10.8
    calibrated = validate_final_layout(draft, plan)
    assert not calibrated['ok'] and calibrated['collisions'] == [['caption', 'second']]
    assert calibrated['text_unit_to_px'] == 10.8 and calibrated['native_visual_verified'] is False
    assert draft == before


@pytest.mark.parametrize('value', [0, -1, True, '10.8', None, float('nan'), float('inf')])
def test_invalid_text_unit_rejected_even_without_text(value):
    draft, plan = case()
    draft['tracks'] = []
    plan['layout_checks'] = {'text_unit_to_px': value}
    before = copy.deepcopy(draft)
    report = validate_final_layout(draft, plan)
    assert not report['ok'] and 'text_unit_to_px' in report['errors'][0]
    assert draft == before


@pytest.mark.parametrize('version', [1, 2])
def test_broll_and_protected_face_regions_checked(version):
    draft, plan = case()
    plan['project_format']['visual_planning_version'] = version
    draft['materials']['videos'] = [{'id': 'v', 'width': 1920, 'height': 1080}]
    segment = copy.deepcopy(draft['tracks'][0]['segments'][0])
    segment['material_id'] = 'v'
    draft['tracks'].append({'name': 'JY_BROLL_example', 'type': 'video', 'segments': [segment]})
    result = validate_final_layout(draft, plan)
    assert not result['ok'] and ['caption', 'JY_BROLL_example'] in result['collisions']
    draft['tracks'].pop()
    plan['layout_checks'] = {'protected_regions': [{'id': 'face', 'bbox': [0, 1300, 1080, 1600], 'start_us': 0, 'end_us': 1000000, 'basis': 'fixture'}]}
    assert not validate_final_layout(draft, plan)['ok']


def test_position_keyframe_cannot_move_text_offscreen_unnoticed():
    draft, plan = case()
    draft['tracks'][0]['segments'][0]['common_keyframes'] = [{'property_type': 'KFTypePositionX', 'keyframe_list': [{'time_offset': 0, 'values': [0]}, {'time_offset': 900000, 'values': [1.5]}]}]
    assert not validate_final_layout(draft, plan)['ok']


def test_missing_font_is_not_exact_measurement_success():
    draft, plan = case()
    payload = json.loads(draft['materials']['texts'][0]['content'])
    payload['styles'][0]['font']['path'] = 'missing.otf'
    draft['materials']['texts'][0]['content'] = json.dumps(payload)
    assert not validate_final_layout(draft, plan)['ok']


@pytest.mark.parametrize('hidden', [False, True])
def test_picture_in_picture_cannot_cover_protected_mouth_without_text(hidden):
    draft, plan = case()
    draft['tracks'] = [{'name': 'JY_BROLL_pip', 'type': 'video', 'visible': not hidden, 'segments': [{
        'id': 'pip', 'material_id': 'v', 'target_timerange': {'start': 0, 'duration': 1_000_000},
        'clip': {'scale': {'x': 1, 'y': 1}, 'transform': {'x': 0, 'y': 0}}}]}]
    draft['materials']['videos'] = [{'id': 'v', 'width': 1080, 'height': 1920, 'path': 'fixture'}]
    plan['layout_checks'] = {'protected_regions': [{'id': 'mouth', 'basis': 'fixture mouth observation',
        'bbox': [440, 660, 640, 760], 'start_us': 0, 'end_us': 1_000_000}]}
    result = validate_final_layout(draft, plan)
    assert result['ok'] is hidden, result['errors']
    if not hidden:
        assert ['JY_BROLL_pip', 'protected:mouth'] in result['collisions']
        plan['layout_checks']['allowed_overlaps'] = [{'tracks': ['JY_BROLL_pip', 'protected:mouth'],
                                                    'reason': 'fixture explicit planned overlay'}]
        assert validate_final_layout(draft, plan)['ok']


def test_native_colour_keyframes_preserved_without_hiding_position_failure():
    draft, plan = case()
    segment = draft['tracks'][0]['segments'][0]
    segment['common_keyframes'] = [{'property_type': 'KFTypeTextColor', 'keyframe_list': [
        {'time_offset': 0, 'values': [1, 1, 1, 1]},
        {'time_offset': 500000, 'values': [1, 0, 0, 1]}]}]
    before = copy.deepcopy(draft)
    assert validate_final_layout(draft, plan)['ok']
    assert draft == before
    segment['common_keyframes'].append(motion('KFTypePositionX', [(0, 0), (900000, 1.5)]))
    assert not validate_final_layout(draft, plan)['ok']


def test_preview_preflight_is_not_current_video_ready():
    selection = {'template_id': 't', 'category': 'standard_caption', 'slots': ['标题'], 'node_duration_us': 1000000}
    registry = {'records': [{'template_id': 't', 'primary_category': 'standard_caption', 'content_contract': {'editable_text_slot_count': 1, 'conservative_max_chars_per_slot': [2]}}]}
    report = preflight_selection(selection, registry, target_canvas=(1080, 1920), require_current_video_evidence=False)
    assert report.ok and not report.current_video_ready
    assert report.checks['declared_text_limit']
    assert 'text_capacity' not in report.checks


@pytest.mark.parametrize('rotation', [0, 30, -90])
def test_constant_rotation_keyframes_equal_fixed_geometry_and_preserve_source(rotation):
    from jianying_adapter.final_layout import _linear_geometry
    segment = {'target_timerange': {'start': 100, 'duration': 1000},
               'clip': {'rotation': rotation}}
    expected = _linear_geometry(segment, 300, 100)
    segment['common_keyframes'] = [motion('KFTypeRotation', [(0, rotation), (1000, rotation)])]
    before = copy.deepcopy(segment)
    assert _linear_geometry(segment, 300, 100) == expected
    assert segment == before
    if rotation:
        with pytest.raises(ValueError, match='does not support rotation'):
            _linear_geometry(segment, 300, 100, kind='B-roll')


@pytest.mark.parametrize('points', [
    [(0, 0), (1000, 1)], [(0, 1), (1000, 1)],
    [(-1, 0), (1000, 0)], [(0, 0), (1001, 0)],
    [(1000, 0), (0, 0)], [(0, 0), (0, 0)], [(0, float('nan'))],
])
def test_rotation_keyframes_reject_changes_mismatch_or_invalid_time(points):
    from jianying_adapter.final_layout import _linear_geometry
    segment = {'target_timerange': {'start': 0, 'duration': 1000},
               'clip': {'rotation': 0}, 'common_keyframes': [motion('KFTypeRotation', points)]}
    with pytest.raises(ValueError):
        _linear_geometry(segment, 300, 100)


def test_constant_rotation_does_not_accept_other_unknown_geometry():
    from jianying_adapter.final_layout import _linear_geometry
    segment = {'target_timerange': {'start': 0, 'duration': 1000},
               'clip': {'rotation': 0}, 'common_keyframes': [
                   motion('KFTypeRotation', [(0, 0), (1000, 0)]),
                   motion('KFTypeUnknown', [(0, 0), (1000, 0)])]}
    with pytest.raises(ValueError, match='unsupported'):
        _linear_geometry(segment, 300, 100)


def motion(kind, points):
    return {'property_type': kind, 'keyframe_list': [
        {'curveType': 'Line', 'time_offset': time, 'values': [value]} for time, value in points]}


def second_text(draft):
    other = copy.deepcopy(draft['tracks'][0])
    other['name'] = 'second'
    other['segments'][0]['id'] = 'other'
    material = copy.deepcopy(draft['materials']['texts'][0])
    material['id'] = 'other_material'
    payload = json.loads(material['content'])
    payload['text'] = '选择'
    payload['styles'][0]['size'] = 20
    material['content'] = json.dumps(payload)
    draft['materials']['texts'].append(material)
    other['segments'][0]['material_id'] = material['id']
    draft['tracks'].append(other)
    return other['segments'][0]


def reading_case(text='重点信息', size=20):
    draft, plan = case()
    segment = second_text(draft)
    track = draft['tracks'][-1]
    track['name'] = 'JY_PRESET_DEMO__NODE__n_01'
    payload = json.loads(draft['materials']['texts'][-1]['content'])
    payload['text'] = text
    payload['styles'][0].update(size=size, range=[0, len(text)])
    draft['materials']['texts'][-1]['content'] = json.dumps(payload)
    segment['clip']['transform']['y'] = .5
    plan['presets'] = [{'template_id': 'DEMO', 'node_id': 'n', 'actual_text_tracks': [
        {'track_name': track['name'], 'text': text, 'role': 'supporting'}]}]
    return draft, plan, segment


@pytest.mark.parametrize('role', ['main_emphasis', 'supporting', 'decorative', 'fixed_symbol'])
@pytest.mark.parametrize('preview', [False, True])
def test_small_special_words_fail_without_optional_fit_or_role_bypass(role, preview, tmp_path):
    draft, plan, _ = reading_case(size=12.6)
    plan['project_format']['visual_planning_version'] = 2
    add_platform_reference(plan, tmp_path)
    plan['presets'][0]['actual_text_tracks'][0]['role'] = role
    result = validate_final_layout(draft, plan, preview=preview)
    assert not result['ok']
    assert any('special text hierarchy below 1.3' in error for error in result['errors'])


def test_caption_yield_does_not_remove_whole_film_reading_baseline():
    draft, plan, segment = reading_case(size=14)
    segment['target_timerange']['start'] = 2_000_000  # no simultaneous ordinary caption
    assert not validate_final_text(draft, plan)['ok']
    draft['tracks'][0]['segments'] = []
    assert any('ordinary_caption_baseline' in error for error in validate_final_text(draft, plan)['errors'])
    plan['layout_checks'] = {'ordinary_caption_baseline': {'material_id': 'm', 'scale_y': 1}}
    assert any('hierarchy' in error for error in validate_final_text(draft, plan)['errors'])
    payload = json.loads(draft['materials']['texts'][-1]['content'])
    payload['styles'][0]['size'] = 20
    draft['materials']['texts'][-1]['content'] = json.dumps(payload)
    assert validate_final_text(draft, plan)['ok']


@pytest.mark.parametrize('defect', ['overshoot', 'multiline_shadow', 'small_qualifier'])
def test_readability_cannot_borrow_overshoot_line_count_or_larger_style(defect):
    draft, plan, segment = reading_case(size=20)
    material = draft['materials']['texts'][-1]
    payload = json.loads(material['content'])
    if defect == 'overshoot':
        payload['styles'][0]['size'] = 14
        segment['clip']['scale']['y'] = 2
        segment['common_keyframes'] = [motion('KFTypeScaleY', [(0, 2), (100_000, 1), (1_000_000, 1)])]
    elif defect == 'multiline_shadow':
        payload['text'] = '重点\n信息'
        payload['styles'][0].update(size=8, range=[0, 5], strokes=[{'width': 3}], shadows=[{'distance': 80}])
        segment['clip']['rotation'] = 90
    else:
        payload['styles'][0]['range'] = [0, 2]
        payload['styles'].append({**copy.deepcopy(payload['styles'][0]), 'range': [2, 4], 'size': 8})
    material['content'] = json.dumps(payload)
    result = validate_final_text(draft, plan)
    assert not result['ok']
    assert any('hierarchy' in error or 'readable glyph height' in error for error in result['errors'])


@pytest.mark.parametrize('setting,value', [('minimum_emphasis_to_caption_ratio', 0),
    ('minimum_emphasis_to_caption_ratio', 1), ('minimum_emphasis_to_caption_ratio', float('nan')),
    ('minimum_readable_text_height_px', 1), ('minimum_readable_text_height_px', True)])
def test_reading_thresholds_cannot_be_disabled(setting, value):
    draft, plan, _ = reading_case()
    plan['layout_checks'] = {setting: value}
    assert not validate_final_text(draft, plan)['ok']


def test_keyword_duplicate_is_checked_across_positions_and_geometry_exemptions():
    draft, plan, segment = reading_case(text='清楚')
    names = [track['name'] for track in draft['tracks']]
    plan['layout_checks'] = {'allowed_overlaps': [{'tracks': names, 'reason': 'must not exempt duplicate text'}]}
    before = copy.deepcopy(draft)
    report = validate_final_layout(draft, plan)
    assert report['collisions'] == [] and not report['ok']
    assert report['text_quality']['duplicates'][0]['text'] == '清楚'
    assert draft == before
    segment['target_timerange']['start'] = 1_000_000
    assert validate_final_text(draft, plan)['ok']  # repeat in a later shot is permitted


def test_native_shadow_stack_is_one_reading_location_but_second_caption_is_not():
    draft, plan, segment = reading_case()
    shadow = copy.deepcopy(draft['tracks'][-1])
    shadow['name'] = 'JY_PRESET_DEMO__NODE__n_02'
    shadow['segments'][0]['id'] = 'shadow'
    shadow['segments'][0]['clip']['transform']['x'] = .005
    draft['tracks'].append(shadow)
    plan['presets'][0]['actual_text_tracks'].append({'track_name': shadow['name'], 'text': '重点信息', 'role': 'decorative'})
    assert validate_final_text(draft, plan)['ok']
    shadow['segments'][0]['clip']['transform']['y'] = -.2
    assert not validate_final_text(draft, plan)['ok']


@pytest.mark.parametrize('variant', ['清\n楚', '「清楚」', '清楚一点'])
def test_linebreak_punctuation_and_contained_keywords_do_not_hide_duplicates(variant):
    draft, plan, _ = reading_case(text=variant)
    assert validate_final_text(draft, plan)['duplicates']


def test_parallel_moving_titles_do_not_collide_by_swept_box_alone():
    draft, plan = case()
    first = draft['tracks'][0]['segments'][0]
    second = second_text(draft)
    first['common_keyframes'] = [motion('KFTypePositionX', [(0, -.6), (1000000, .2)])]
    second['common_keyframes'] = [motion('KFTypePositionX', [(0, -.2), (1000000, .6)])]
    before = copy.deepcopy(draft)
    result = validate_final_layout(draft, plan)
    assert result['ok'] and result['checked_visible_pairs'] == 1
    assert draft == before


def test_content_appearing_after_title_moved_does_not_hit_previous_position():
    draft, plan = case()
    first = draft['tracks'][0]['segments'][0]
    second = second_text(draft)
    first['common_keyframes'] = [motion('KFTypePositionX', [(0, -.5), (500000, .5)])]
    second['clip']['transform']['x'] = -.5
    second['target_timerange'] = {'start': 600000, 'duration': 400000}
    assert validate_final_layout(draft, plan)['ok']


def test_crossing_titles_collide_between_nonoverlapping_endpoints():
    draft, plan = case()
    first = draft['tracks'][0]['segments'][0]
    second = second_text(draft)
    first['common_keyframes'] = [motion('KFTypePositionX', [(0, -.5), (1000000, .5)])]
    second['common_keyframes'] = [motion('KFTypePositionX', [(0, .5), (1000000, -.5)])]
    result = validate_final_layout(draft, plan)
    assert not result['ok'] and result['collisions'] == [['caption', 'second']]


def test_prior_maximum_scale_does_not_expand_later_shared_interval():
    draft, plan = case()
    first = draft['tracks'][0]['segments'][0]
    second = second_text(draft)
    first['common_keyframes'] = [motion(kind, [(0, 2), (500000, .25)])
                                 for kind in ('KFTypeScaleX', 'KFTypeScaleY')]
    second['clip']['transform']['x'] = .3
    second['target_timerange'] = {'start': 600000, 'duration': 400000}
    result = validate_final_layout(draft, plan)
    assert result['collisions'] == []  # old position/scale still cannot cause a collision
    assert not result['ok']  # the actual held quarter-size text is now correctly unreadable
    assert any('readable glyph height' in error for error in result['errors'])


def test_protected_region_collision_uses_its_active_interval():
    draft, plan = case()
    draft['tracks'][0]['segments'][0]['common_keyframes'] = [
        motion('KFTypePositionX', [(0, -.5), (500000, .5)])]
    plan['layout_checks'] = {'protected_regions': [{'id': 'face', 'bbox': [150, 1300, 350, 1600],
        'start_us': 600000, 'end_us': 1000000, 'basis': 'fixture'}]}
    assert validate_final_layout(draft, plan)['ok']


def test_non_linear_motion_cannot_be_accepted_as_linear():
    draft, plan = case()
    curve = motion('KFTypePositionX', [(0, -.5), (1000000, .5)])
    curve['keyframe_list'][0]['curveType'] = 'FreeCurve'
    draft['tracks'][0]['segments'][0]['common_keyframes'] = [curve]
    result = validate_final_layout(draft, plan)
    assert not result['ok'] and any('linear' in error for error in result['errors'])


def test_continuous_solver_catches_narrow_collision_without_sampling():
    stationary = {'start_us': 0, 'end_us': 1000000, 'bbox': [123.4, 0, 123.5, 1]}
    moving = {'start_us': 0, 'end_us': 1000000, 'geometry_keyframes': [
        {'time_us': 0, 'bbox': [0, 0, .1, 1]},
        {'time_us': 1000000, 'bbox': [1000, 0, 1000.1, 1]}]}
    assert _simultaneous_overlap(stationary, moving)


def test_axes_overlapping_at_different_times_is_not_a_collision():
    stationary = {'start_us': 0, 'end_us': 10, 'bbox': [0, 0, 1, 1]}
    moving = {'start_us': 0, 'end_us': 10, 'geometry_keyframes': [
        {'time_us': 0, 'bbox': [0, -10, 1, -9]},
        {'time_us': 10, 'bbox': [10, 0, 11, 1]}]}
    assert not _simultaneous_overlap(stationary, moving)


def test_touching_only_at_visibility_end_is_not_a_collision():
    stationary = {'start_us': 0, 'end_us': 10, 'bbox': [0, 0, 1, 1]}
    moving = {'start_us': 0, 'end_us': 10, 'geometry_keyframes': [
        {'time_us': 0, 'bbox': [2, 0, 3, 1]},
        {'time_us': 10, 'bbox': [1, 0, 2, 1]}]}
    assert not _simultaneous_overlap(stationary, moving)


def broll(draft, points):
    draft['materials']['videos'] = [{'id': 'video', 'width': 1080, 'height': 1080}]
    segment = {'id': 'bar', 'material_id': 'video', 'target_timerange': {'start': 0, 'duration': 1_000_000},
               'clip': {'scale': {'x': .1, 'y': .1}, 'transform': {'x': 0, 'y': -.5}, 'rotation': 0},
               'common_keyframes': [motion('KFTypePositionX', points)]}
    draft['tracks'].append({'name': 'JY_BROLL_bar', 'type': 'video', 'segments': [segment]})
    return segment


@pytest.mark.parametrize('canvas', [(1080, 1920), (1920, 1080)])
def test_parallel_text_and_broll_use_simultaneous_boxes_not_swept_union(canvas):
    draft, plan = case(canvas)
    draft['tracks'][0]['segments'][0]['common_keyframes'] = [motion('KFTypePositionX', [(0, -.6), (1_000_000, .2)])]
    broll(draft, [(0, -.2), (1_000_000, .6)])
    before = copy.deepcopy(draft)
    report = validate_final_layout(draft, plan)
    assert report['ok'] and report['checked_visible_pairs'] == 1
    assert report['media_and_protected_regions'][0]['geometry_keyframes']
    assert draft == before


@pytest.mark.parametrize('canvas', [(1080, 1920), (1920, 1080)])
def test_text_and_broll_crossing_between_clear_endpoints_is_rejected(canvas):
    draft, plan = case(canvas)
    draft['tracks'][0]['segments'][0]['common_keyframes'] = [motion('KFTypePositionX', [(0, -.5), (1_000_000, .5)])]
    broll(draft, [(0, .5), (1_000_000, -.5)])
    report = validate_final_layout(draft, plan)
    assert not report['ok'] and report['collisions'] == [['caption', 'JY_BROLL_bar']]


@pytest.mark.parametrize('canvas', [(1080, 1920), (1920, 1080)])
def test_late_text_does_not_hit_broll_previous_position_or_scale(canvas):
    draft, plan = case(canvas)
    text = draft['tracks'][0]['segments'][0]
    text['target_timerange'] = {'start': 600_000, 'duration': 400_000}
    text['clip']['transform']['x'] = -.5
    media = broll(draft, [(0, -.5), (500_000, .5)])
    media['common_keyframes'].extend(motion(prop, [(0, 1), (500_000, .1)])
                                     for prop in ('KFTypeScaleX', 'KFTypeScaleY'))
    assert validate_final_layout(draft, plan)['ok']


@pytest.mark.parametrize('scenario', ['rotation', 'bezier', 'negative_scale', 'late_keyframe', 'alpha', 'native_animation', 'uniform_scale'])
def test_unsupported_broll_geometry_cannot_be_reported_as_linear(scenario):
    draft, plan = case()
    segment = broll(draft, [(0, .5), (1_000_000, .6)])
    if scenario == 'rotation': segment['clip']['rotation'] = 5
    elif scenario == 'bezier': segment['common_keyframes'][0]['keyframe_list'][0]['curveType'] = 'FreeCurve'
    elif scenario == 'negative_scale': segment['common_keyframes'].append(motion('KFTypeScaleX', [(0, -.1)]))
    elif scenario == 'late_keyframe': segment['common_keyframes'][0]['keyframe_list'][-1]['time_offset'] = 1_000_001
    elif scenario == 'alpha': segment['common_keyframes'].append(motion('KFTypeAlpha', [(0, 0), (500_000, 1)]))
    elif scenario == 'native_animation':
        segment['extra_material_refs'] = ['animation']
        draft['materials']['material_animations'] = [{'id': 'animation', 'animations': [{'type': 'in'}]}]
    elif scenario == 'uniform_scale': segment['uniform_scale'] = {'on': True, 'value': 2}
    report = validate_final_layout(draft, plan)
    assert not report['ok'] and any('final B-roll measurement failed' in e for e in report['errors'])


def test_landscape_lower_caption_uses_actual_canvas_and_rejects_offscreen_motion():
    draft, plan = case((1920, 1080))
    segment = draft['tracks'][0]['segments'][0]
    segment['clip']['transform']['y'] = -.7
    result = validate_final_layout(draft, plan)
    assert result['ok'] and result['safe_area_px'] == [0, 0, 1920, 1080]
    box = result['text_segments'][0]['bbox']
    assert (box[0]+box[2])/2 == pytest.approx(960)
    assert (box[1]+box[3])/2 == pytest.approx(918)
    assert 800 < box[1] < box[3] < 1080
    segment['common_keyframes'] = [motion('KFTypePositionX', [(0, 0), (900000, 1.2)])]
    rejected = validate_final_layout(draft, plan)
    assert not rejected['ok'] and any('outside safe area' in e for e in rejected['errors'])
    # A later portrait call must not inherit the landscape canvas.
    portrait, portrait_plan = case()
    portrait_box = validate_final_layout(portrait, portrait_plan)['text_segments'][0]['bbox']
    assert (portrait_box[0]+portrait_box[2])/2 == pytest.approx(540)
    assert (portrait_box[1]+portrait_box[3])/2 == pytest.approx(1440)


def test_landscape_broll_fit_and_caption_collision_use_same_pixel_space():
    draft, plan = case((1920, 1080))
    segment = broll(draft, [(0, .5), (1_000_000, .5)])
    draft['materials']['videos'][0].update(width=3840, height=2160)
    segment['clip'].update(scale={'x': .25, 'y': .25}, transform={'x': .5, 'y': .4})
    result = validate_final_layout(draft, plan)
    assert result['ok']
    assert result['media_and_protected_regions'][0]['bbox'] == pytest.approx([1200, 189, 1680, 459])
    segment['common_keyframes'] = []
    segment['clip']['transform'] = {'x': 0, 'y': -.5}
    result = validate_final_layout(draft, plan)
    assert not result['ok'] and result['collisions'] == [['caption', 'JY_BROLL_bar']]


@pytest.mark.parametrize('size', [(1080, 608), (1280, 720)])
def test_final_layout_rejects_unadded_canvas_sizes(size):
    draft, plan = case(size)
    result = validate_final_layout(draft, plan)
    assert not result['ok'] and 'requires 1080x1920 or 1920x1080' in result['errors'][0]


def test_final_layout_rejects_canvas_mismatch_and_landscape_safe_area_overflow():
    draft, plan = case((1920, 1080))
    draft['canvas_config'] = {'width': 1080, 'height': 1920}
    assert not validate_final_layout(draft, plan)['ok']
    draft['canvas_config'] = dict(plan['target'])
    plan['layout_checks'] = {'safe_area_px': [0, 0, 1920, 1920]}
    result = validate_final_layout(draft, plan)
    assert not result['ok'] and result['errors'] == ['invalid final layout safe_area_px']


def test_v2_preview_defers_only_missing_platform_reference():
    draft, plan = case()
    plan['project_format']['visual_planning_version'] = 2
    assert not validate_final_layout(draft, plan)['ok']
    report = validate_final_layout(draft, plan, preview=True)
    assert report['ok']
    platform = report['platform_ui_check']
    assert platform['status'] == 'pending_platform_ui_reference'
    assert platform['deferred_to_user_review'] is True
    assert {'part': 'platform_ui_reference_missing'} in platform['unchecked']
    assert report['native_visual_verified'] is False
    # A supplied but invalid reference and an actual layout error still fail.
    plan['layout_checks'] = {'platform_ui': {'platform': 'douyin'}}
    assert not validate_final_layout(draft, plan, preview=True)['ok']
    plan['layout_checks'] = {}
    draft['tracks'][0]['segments'][0]['clip']['transform']['x'] = 2
    assert not validate_final_layout(draft, plan, preview=True)['ok']

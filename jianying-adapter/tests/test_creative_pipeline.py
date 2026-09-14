"""Regression paths from intent through resources, native assembly and readback."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from jianying_adapter.cli import main
from jianying_adapter.creative_plan import validate_creative_plan
from jianying_adapter.media_crop import fit_region, native_crop
from jianying_adapter.material_library import material_handoff, attach_material_handoff, validate_plan_materials, register_material
from jianying_adapter.preset_registry import (attach_preset_selection, preset_request_from_plan,
    select_template, validate_preset_selections, describe_visual_form, compare_expression_features)
from jianying_adapter.visual_planning import summarize_visual_sequence, validate_visual_execution
from test_material_library import setup, usage_for, write_json


BRIEF = {'viewer_takeaway': '店铺里的经营者需要把精力放在顾客身上',
         'progression': '先提出忙不过来的问题，再展示店内接待情境，最后说明分工',
         'visual_strategy': '店内情境为主，字幕承接台词；用场景交接引导注意',
         'sound_strategy': '对白为主体，选定成套预设时保留原配声画时序'}


def choice_case(tmp_path):
    candidates = []
    for name in ('first', 'second'):
        source = write_json(tmp_path / (name + '.json'), {'tracks': [], 'materials': {}})
        candidates.append({'template_id': name, 'semantic_tags': ['question_hook'], 'source_path': str(source)})
    plan = {'creative_brief': deepcopy(BRIEF), 'operations': [], 'visual_events': [
        {'id': eid, 'start_us': i*1_000_000, 'end_us': (i+1)*1_000_000,
         'audience_need': '测试一个明确的问题', 'expression_role': 'question', 'composition': '人物旁的问题',
         'preset_request': {'intent': 'question', 'requirement_id': 'text_'+eid}, 'techniques': [],
         'visual_requirements': [{'id': 'text_'+eid, 'subject': '当前问题', 'observable': '观众读到问题文字', 'delivery': 'text', 'operation_ids': []}],
         'transition': {'intent': 'hold', 'reason': '连续提出两个问题'}} for i, eid in enumerate(('a', 'b'))]}
    return plan, {'candidates': candidates}


def decision(query, eid, tid):
    candidate = next(row for row in query['top_k'] if row['template_id'] == tid)
    return {'event_id': eid, 'node_id': eid, 'template_id': tid, 'reason': '测试保存真实的比较取舍',
            'compared_with': [{'template_id': row['template_id'], 'reason': '测试比较来源与当前用途'}
                              for row in query['top_k'] if row['template_id'] != tid],
            'reviewed': {'scope': 'source_structure', 'source_path': candidate['source']['source_path'],
                         'observation': '测试查看源结构；未进行原生播放'}}


def query_for(plan, registry, eid):
    return select_template(registry, preset_request_from_plan(plan, eid))


def test_planning_review_lists_unbound_work_without_claiming_delivery(tmp_path, capsys):
    plan, _ = choice_case(tmp_path)
    path = write_json(tmp_path / 'planning.json', plan)
    before = path.read_bytes()
    assert main(['creative-review', str(path)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['ok'] is False
    assert any('文字需求须连接' in error for error in report['creative_plan']['errors'])
    assert report['preset_choices']['ok'] is False
    assert '规划缺口清单' in report['scope']
    assert 'visual_execution' not in report
    assert path.read_bytes() == before


@pytest.mark.parametrize('exists', [False, True])
def test_planning_review_still_reports_input_errors(tmp_path, capsys, exists):
    path = tmp_path / 'invalid.json'
    if exists:
        path.write_text('{invalid', encoding='utf8')
    assert main(['creative-review', str(path)]) == 2
    report = json.loads(capsys.readouterr().out)
    assert not report['ok'] and report['error']


def test_real_cli_saves_choice_before_next_query_and_rejects_stale_query(tmp_path, capsys):
    plan, registry = choice_case(tmp_path)
    stale_second = query_for(plan, registry, 'b')
    original = write_json(tmp_path / 'plan.json', plan)
    reg = write_json(tmp_path / 'registry.json', registry)
    query_path, choice_path, next_plan = [tmp_path / name for name in ('query.json', 'choice.json', 'selected.json')]
    assert main(['preset-select', str(original), '--event', 'a', '--registry', str(reg), '-o', str(query_path)]) == 0
    query = json.loads(query_path.read_text('utf8'))
    write_json(choice_path, decision(query, 'a', 'first'))
    assert main(['preset-use', str(original), str(query_path), str(choice_path), '-o', str(next_plan)]) == 0
    selected = json.loads(next_plan.read_text('utf8'))
    assert selected['operations'] == []  # Choice exists before native operations are authored.
    assert json.loads(original.read_text('utf8')) == plan
    with pytest.raises(ValueError, match='上下文已变化'):
        attach_preset_selection(selected, stale_second, decision(stale_second, 'b', 'second'))
    current_second = query_for(selected, registry, 'b')
    assert current_second['request']['sequence_context']['prior_events'] == [{'event_id': 'a', 'template_ids': ['first']}]
    assert not current_second['request']['sequence_context']['unresolved_prior_events']
    selected = attach_preset_selection(selected, current_second, decision(current_second, 'b', 'second'))
    for eid, tid in [('a', 'first'), ('b', 'second')]:
        selected['operations'].append({'id': eid, 'kind': 'add_preset_group', 'template_id': tid, 'node_id': eid})
        next(e for e in selected['visual_events'] if e['id'] == eid)['techniques'] = [{'kind': 'text_preset', 'operation_ids': [eid]}]
        next(e for e in selected['visual_events'] if e['id'] == eid)['visual_requirements'][0]['operation_ids'] = [eid]
    report = validate_preset_selections(selected, require_choices=True)
    assert report['ok'], report['errors']  # Future choices do not circularly invalidate the first choice.
    assert report['sequence_changes']
    selected['visual_events'][0]['preset_selections'][0]['template_id'] = 'second'
    selected['operations'][0]['template_id'] = 'second'
    assert any('之前的预设选择已变化' in e for e in validate_preset_selections(selected, require_choices=True)['errors'])
    capsys.readouterr()


def test_cannot_save_later_preset_with_unfinished_previous_choice(tmp_path):
    plan, registry = choice_case(tmp_path)
    query = query_for(plan, registry, 'b')
    with pytest.raises(ValueError, match='前面需要预设'):
        attach_preset_selection(plan, query, decision(query, 'b', 'second'))


def test_same_event_multiple_nodes_cannot_reuse_query_from_before_first_choice(tmp_path):
    plan, registry = choice_case(tmp_path)
    query = query_for(plan, registry, 'a')
    first = attach_preset_selection(plan, query, decision(query, 'a', 'first'))
    second = decision(query, 'a', 'second')
    second['node_id'] = 'a-detail'
    with pytest.raises(ValueError, match='上下文已变化'):
        attach_preset_selection(first, query, second)
    new_query = query_for(first, registry, 'a')
    assert new_query['request']['sequence_context']['current_event_selections'][0]['node_id'] == 'a'
    assert len(attach_preset_selection(first, new_query, second)['visual_events'][0]['preset_selections']) == 2


def test_font_color_and_audio_identity_are_read_from_actual_sources(tmp_path):
    # Hash equality proves identical bytes only; playback is deliberately not asserted.
    source = tmp_path / 'typing.wav'
    source.write_bytes(b'audio fixture bytes')
    alias = tmp_path / 'renamed.wav'
    alias.write_bytes(source.read_bytes())
    def features(audio):
        return describe_visual_form({'materials': {
            'texts': [{'id': 't', 'font_name': 'fixture-font', 'font_path': str(tmp_path/'missing-font.ttf'),
                'content': json.dumps({'text': '测试', 'styles': [{'fill': {'content': {'solid': {'color': [1, .5, 0]}}}}]})}],
            'audios': [{'id': 'a', 'path': str(audio)}]}, 'tracks': [
            {'type': 'text', 'segments': [{'material_id': 't', 'target_timerange': {'start': 0, 'duration': 1_000_000}}]},
            {'type': 'audio', 'segments': [{'material_id': 'a', 'target_timerange': {'start': 0, 'duration': 200_000}}]}]})
    first, second = features(source), features(alias)
    comparison = compare_expression_features(first, second)
    assert comparison['shared_font_names'] == ['fixture-font']
    assert comparison['shared_text_colors'] == ['#ff8000']
    assert len(comparison['same_audio_content']) == 1
    assert not comparison['shared_audio_references'] and not comparison['listened']
    missing = features(tmp_path/'absent.wav')
    assert not compare_expression_features(missing, missing)['same_audio_content']
    assert compare_expression_features(missing, missing)['shared_audio_references']


def framed_handoff(setup):
    folder, config, request, selection = setup
    request.update(visual_requirement_id='owner', subject='店主与柜台', observable='看清店主接待顾客的空间关系', time_behavior='still')
    asset = register_material(request, selection, config)
    usage = usage_for(asset, selection)
    crop, framing = [.1, 0, .9, 1], {'canvas_size': [1080, 1920], 'target_rect': [100, 150, 900, 800],
                                        'content_region': [.2, .1, .8, .9]}
    solved = fit_region([320, 180], framing['canvas_size'], framing['target_rect'], source_crop=crop, content_region=framing['content_region'])
    usage.update(source_crop=crop, framing=framing, clip=solved['clip'])
    usage['visual_review'].update(source_crop=crop, clip=deepcopy(solved['clip']))
    req = write_json(folder / 'request.json', request)
    handoff = material_handoff(req, usage, config)
    need = {key: request[key] for key in ('subject', 'observable', 'time_behavior')}
    need.update(id='owner', delivery='media', request_path=str(req), operation_ids=[])
    plan = {'creative_brief': deepcopy(BRIEF), 'operations': [], 'visual_events': [{
        'id': 'choice', 'start_us': 0, 'end_us': 1_000_000, 'speech_ids': ['s1'],
        'expression_role': 'context', 'audience_need': request['purpose'], 'primary_visual': 'media',
        'composition': request['composition'], 'handbook_refs': ['handbook/broll'], 'review_focus': '观察店主和字幕的关系',
        'visual_requirements': [need], 'techniques': [],
        'supporting_visual': {'status': 'needs_asset', 'purpose': request['purpose'], 'request_paths': [str(req)], 'operation_ids': []}}]}
    return attach_material_handoff(plan, folder/'plan.json', handoff), handoff, usage, req


@pytest.mark.parametrize('defect', [None, 'source_crop', 'clip', 'request_changed', 'request_text_changed', 'removed_observation'])
def test_material_framing_is_preserved_and_checked_against_actual_native_readback(setup, defect):
    from types import SimpleNamespace
    from jianying_adapter.shared_operations import add_broll
    folder, config, request, _ = setup
    plan, handoff, usage, req = framed_handoff(setup)
    path = folder/'plan.json'
    assert handoff['operation']['source_crop'] == handoff['broll']['source_crop'] == [.1, 0, .9, 1]
    assert plan['visual_events'][0]['visual_requirements'][0]['operation_ids'] == [handoff['operation']['id']]
    context = SimpleNamespace(plan_path=path, plan=plan)
    draft, _ = add_broll({'materials': {}, 'tracks': []}, handoff['operation'], context)
    assert validate_creative_plan(plan, draft=draft, plan_path=path)['ok']
    if defect == 'source_crop': draft['materials']['videos'][0]['crop'] = native_crop([0, 0, 1, 1])
    elif defect == 'clip': draft['tracks'][0]['segments'][0]['clip']['scale']['x'] /= 2
    elif defect in ('request_changed', 'request_text_changed'):
        changed = json.loads(req.read_text('utf8'))
        changed['style' if defect == 'request_changed' else 'text'] = '用途改为另一套配色' if defect == 'request_changed' else '改变素材中的确切文字'
        write_json(req, changed)
    elif defect == 'removed_observation':
        plan['operations'][0]['media_asset']['visual_review'].pop('source_crop')
    report = validate_plan_materials(plan, path, draft)
    assert report['ok'] is (defect is None), report['errors']


def test_framing_preserves_proportions_and_cannot_cut_required_content(setup, capsys):
    folder, config, _, _ = setup
    plan, handoff, usage, req = framed_handoff(setup)
    config_path = write_json(folder/'config.json', config)
    usage_path = write_json(folder/'usage.json', usage)
    assert main(['material-frame', str(req), str(usage_path), '--config', str(config_path)]) == 0
    calculation = json.loads(capsys.readouterr().out)
    assert calculation['clip'] == usage['clip'] and not calculation['native_visual_verified']
    solved = fit_region([320, 180], [1080, 1920], [100, 150, 900, 800], source_crop=[.1, 0, .9, 1])
    from jianying_adapter.final_layout import _broll_geometry
    material = {'width': 320, 'height': 180, 'crop': native_crop(solved['source_crop'])}
    segment = {'target_timerange': {'start': 0, 'duration': 1_000_000}, 'clip': solved['clip']}
    geometry = _broll_geometry(segment, material, {}, canvas=(1080, 1920))
    assert geometry[0]['bbox'] == pytest.approx(solved['placed_rect'])
    plan['target'] = {'width': 1920, 'height': 1080}
    assert any('画布' in e for e in validate_plan_materials(plan, folder/'plan.json')['errors'])
    usage['framing']['content_region'] = [0, 0, .8, 1]
    with pytest.raises(ValueError, match='content_region'):
        material_handoff(req, usage, config)
    request = json.loads(req.read_text('utf8'))
    request.update(time_behavior='continuous_action', media_type='image')
    write_json(req, request)
    with pytest.raises(ValueError, match='连续动作需求'):
        material_handoff(req, usage, config)


@pytest.mark.parametrize('defect', [None, 'wrong_source', 'unseen_range', 'coverage_gap', 'fake_motion'])
def test_original_picture_must_deliver_the_observed_source_and_required_interval(tmp_path, defect):
    source = tmp_path/'original.mp4'
    source.write_bytes(b'source-observation fixture; decoder is tested separately')
    need = {'id': 'hands', 'subject': '双手操作', 'observable': '看清双手把产品展开', 'delivery': 'aroll',
            'time_behavior': 'continuous_action', 'track_name': 'JY_ROUGH_CUT_VIDEO',
            'source_observation': {'source_path': str(source), 'start_us': 4_000_000, 'end_us': 7_000_000,
                                   'scope': 'video_frames', 'observation': '测试中的源采样记录，不冒充连续播放'}}
    plan = {'creative_brief': BRIEF, 'visual_events': [{'id': 'action', 'start_us': 0, 'end_us': 3_000_000,
        'expression_role': 'demonstration', 'visual_requirements': [need]}], 'operations': []}
    segment = {'id': 's', 'material_id': 'v', 'target_timerange': {'start': 0, 'duration': 3_000_000},
               'source_timerange': {'start': 4_000_000, 'duration': 3_000_000}}
    material = {'id': 'v', 'path': str(source)}
    draft = {'materials': {'videos': [material]}, 'tracks': [{'type': 'video', 'name': 'JY_ROUGH_CUT_VIDEO', 'segments': [segment]}]}
    if defect == 'wrong_source': material['path'] += '.other'
    elif defect == 'unseen_range': need['source_observation']['end_us'] -= 1
    elif defect == 'coverage_gap': segment['target_timerange']['duration'] -= 1
    elif defect == 'fake_motion': need['time_behavior'] = 'graphic_motion'
    report = validate_creative_plan(plan, draft=draft, require=True)
    assert report['ok'] is (defect is None), report['errors']
    if defect is None:
        assert report['requirements'][0]['source_observation_scope'] == 'video_frames'
        assert not report['native_visual_verified']


@pytest.mark.parametrize('moving,hidden', [(False, False), (True, False), (True, True)])
def test_static_transform_is_not_motion_and_muted_video_remains_visible(tmp_path, moving, hidden):
    from test_visual_planning import v2_plan, support_draft
    p, draft = v2_plan(), support_draft()
    source = tmp_path/'source.mp4'
    source.write_bytes(b'visibility fixture; decoding is covered in media tests')
    p['operations'].append({'id': 'move', 'kind': 'animate_aroll_transform', 'track_name': 'JY_ROUGH_CUT_VIDEO', 'segment_id': 's'})
    p['visual_events'][0]['techniques'] = [{'kind': 'keyframes', 'operation_ids': ['move']}]
    p['design_decisions']['motion'] = {'decision': 'use', 'event_ids': ['comparison'], 'reason': '观察实际推近'}
    draft['materials']['videos'].append({'id': 'v', 'path': str(source)})
    draft['tracks'].append({'type': 'video', 'name': 'JY_ROUGH_CUT_VIDEO', 'attribute': 1, 'visible': not hidden,
        'segments': [{'id': 's', 'material_id': 'v', 'target_timerange': {'start': 0, 'duration': 3_000_000},
                     'common_keyframes': [{'property_type': 'KFTypeScaleX', 'keyframe_list': [
                         {'time_offset': 0, 'values': [1.0]}, {'time_offset': 3_000_000, 'values': [1.1 if moving else 1.0]}]}]}]})
    report = validate_visual_execution(p, draft)
    assert report['ok'] is (moving and not hidden), report['errors']
    row = report['sequence_summary']['motion_operations'][0]
    assert row['actual_motion'] is (moving and not hidden)


@pytest.mark.parametrize('defect', [None, 'cut_region', 'source_changed', 'no_digest', 'unseen_range'])
def test_source_region_reuse_is_bound_to_source_but_checked_against_current_crop(tmp_path, defect):
    import copy
    from jianying_adapter.media_ledger import file_sha256
    source = tmp_path/'original.mp4'
    source.write_bytes(b'synthetic source identity; no visual result is claimed')
    observation = {'source_path': str(source), 'source_sha256': file_sha256(source),
                   'start_us': 4_000_000, 'end_us': 7_000_000, 'scope': 'video_frames',
                   'observation': 'synthetic fixture region', 'content_region': [.3, .2, .7, .8]}
    original_observation = copy.deepcopy(observation)
    need = {'id': 'clasp', 'subject': '夹扣', 'observable': '保留夹扣区域', 'delivery': 'aroll',
            'track_name': 'JY_ROUGH_CUT_VIDEO', 'source_observation': observation}
    plan = {'creative_brief': BRIEF, 'operations': [], 'visual_events': [{
        'id': 'detail', 'start_us': 0, 'end_us': 3_000_000, 'expression_role': 'detail',
        'visual_requirements': [need]}]}
    material = {'id': 'v', 'path': str(source), 'crop': native_crop([.1, 0, .9, 1])}
    segment = {'id': 's', 'material_id': 'v', 'target_timerange': {'start': 0, 'duration': 3_000_000},
               'source_timerange': {'start': 4_000_000, 'duration': 3_000_000}}
    draft = {'materials': {'videos': [material]}, 'tracks': [{
        'type': 'video', 'name': 'JY_ROUGH_CUT_VIDEO', 'segments': [segment]}]}
    if defect == 'cut_region': material['crop'] = native_crop([.5, 0, 1, 1])
    elif defect == 'source_changed': source.write_bytes(b'new source at the same path')
    elif defect == 'no_digest': observation.pop('source_sha256')
    elif defect == 'unseen_range': observation['end_us'] -= 1
    report = validate_creative_plan(plan, draft=draft, require=True)
    assert report['ok'] is (defect is None), report
    if defect is None:
        region = report['requirements'][0]['source_content_region']
        solved = fit_region([320, 180], [1080, 1920], [100, 200, 900, 900],
                            source_crop=[.2, .1, .8, .9], content_region=region)
        material['crop'] = native_crop(solved['source_crop'])
        segment['clip'] = solved['clip']
        assert validate_creative_plan(plan, draft=draft, require=True)['ok']
        assert observation == original_observation
        assert not report['native_visual_verified']


def test_intent_to_real_candidate_assembler_and_readback(setup, capsys):
    from test_candidate_plan import make_plan
    from jianying_adapter.candidate_plan import validate_candidate_plan
    folder, _, _, _ = setup
    path = make_plan(folder)
    plan = json.loads(path.read_text('utf8'))
    creative, handoff, _, _ = framed_handoff(setup)
    plan.update(creative)
    plan['project_format']['visual_planning_version'] = 2
    state_path = Path(plan['project_state'])
    state = json.loads(state_path.read_text('utf8'))
    state.update(status='planning', visual_planning_min_version=2)
    write_json(state_path, state)
    base = json.loads(Path(plan['base_draft']).read_text('utf8'))
    font = Path('C:/Windows/Fonts/msyh.ttc')  # Read installed font; nothing is downloaded to C.
    if not font.is_file(): pytest.skip('Windows font fixture unavailable')
    base['materials']['texts'][0]['content'] = json.dumps({'text': '完整语音', 'styles': [
        {'range': [0, 4], 'size': 18, 'font': {'path': str(font)}}]}, ensure_ascii=False)
    base['tracks'][0]['segments'][0]['clip'] = {'scale': {'x': 1, 'y': 1}, 'transform': {'x': 0, 'y': -.7}}
    write_json(Path(plan['base_draft']), base)
    plan['operations'].insert(0, {'id': 'captions', 'kind': 'reflow_ordinary_captions'})
    write_json(path, plan)
    report = validate_candidate_plan(plan, plan_path=path, mode='preview')
    assert report.ok, report.errors
    missing_brief = deepcopy(plan)
    missing_brief.pop('creative_brief')
    assert any('creative_brief' in e for e in validate_candidate_plan(missing_brief, plan_path=path, mode='preview').errors)
    missing_delivery = deepcopy(plan)
    missing_delivery['visual_events'][0]['visual_requirements'][0]['operation_ids'] = []
    assert any('尚未落实 add_broll' in e for e in validate_candidate_plan(missing_delivery, plan_path=path, mode='preview').errors)
    assert main(['candidate-preview', str(path)]) == 0
    candidate = json.loads(Path(plan['preview_output']).read_text('utf8'))
    manifest = json.loads(Path(plan['preview_manifest']).read_text('utf8'))
    assert manifest['writer_allowed'] is False
    assert validate_creative_plan(plan, draft=candidate, plan_path=path)['ok']
    assert validate_plan_materials(plan, path, candidate)['ok']
    assert main(['creative-review', str(path), '--draft', plan['preview_output'], '-o', str(folder/'review.json')]) == 0
    review = json.loads((folder/'review.json').read_text('utf8'))
    assert review['sequence_summary']['supporting_media_duration_us'] == 1_000_000
    assert not review['sequence_summary']['native_visual_verified']
    # The planning CLI's success must not hide a missing layer in draft review.
    missing_picture = deepcopy(candidate)
    missing_picture['tracks'] = [track for track in missing_picture['tracks'] if track.get('type') != 'video']
    broken_path = write_json(folder/'missing_picture.json', missing_picture)
    broken_report = folder/'missing_picture_review.json'
    assert main(['creative-review', str(path), '--draft', str(broken_path), '-o', str(broken_report)]) == 1
    assert json.loads(broken_report.read_text('utf8'))['ok'] is False
    assert json.loads(state_path.read_text('utf8')) == state
    capsys.readouterr()

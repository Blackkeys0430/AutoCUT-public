from copy import deepcopy
from pathlib import Path
import pytest

from jianying_adapter.visual_planning import validate_visual_events
from jianying_adapter.visual_planning import summarize_visual_sequence
from jianying_adapter.candidate_plan import validate_visual_rules


def plan():
    return {
        "project_format": {"visual_planning_version": 1},
        "visual_events": [{
            "id": "comparison", "start_us": 0, "end_us": 3000000,
            "audience_need": "看清两个选择", "primary_visual": "relationship",
            "composition": "人物卡片辅助，两栏比较为主",
            "techniques": [
                {"kind": "mask", "operation_ids": ["person_card"]},
                {"kind": "keyframes", "operation_ids": ["move_person"]},
            ],
        }],
        "operations": [
            {"id": "person_card", "kind": "apply_video_mask"},
            {"id": "move_person", "kind": "animate_aroll_transform"},
        ],
    }


def test_declared_mask_cannot_be_silently_replaced_with_text():
    p = plan()
    p["operations"][0]["kind"] = "add_preset_group"
    result = validate_visual_rules(p, require_native_clearance=False)
    assert not result["ok"]
    assert any("mask 未连接匹配操作" in error for error in result["errors"])


def test_deleted_keyframe_operation_is_rejected():
    p = plan()
    p["operations"].pop()
    assert any("keyframes 未连接" in error for error in validate_visual_events(p)["errors"])


def test_no_forced_effect_count_and_no_visual_success_claim():
    p = plan()
    p["visual_events"][0].update(primary_visual="person", composition="正常人物讲述", techniques=[])
    p["operations"] = []
    before = deepcopy(p)
    r = validate_visual_events(p)
    assert r["ok"] and r["native_visual_verified"] is False
    assert p == before


def test_intended_technique_requires_a_real_binding():
    p = plan()
    p["visual_events"][0]["techniques"][0]["operation_ids"] = []
    assert not validate_visual_events(p)["ok"]


def test_unknown_capability_is_explicit_not_a_fallback():
    p = plan()
    p["visual_events"][0]["techniques"][0]["kind"] = "automatic_matting"
    assert any("先接通共享入口" in error for error in validate_visual_events(p)["errors"])


def test_new_plan_requires_intent_old_plan_remains_readable():
    assert validate_visual_events({"operations": []})["ok"]
    assert not validate_visual_events({"project_format": {"visual_planning_version": 1}})["ok"]


def test_bound_techniques_pass_without_implying_native_acceptance():
    r = validate_visual_events(plan())
    assert r["ok"] and r["native_visual_verified"] is False


def test_final_sequence_counts_visible_intervals_not_declared_operations(tmp_path):
    source = tmp_path / 'media.png'
    source.write_bytes(b'timeline visibility fixture; decode is covered by material library tests')
    p, draft = {'operations': [], 'visual_events': []}, {'tracks': [], 'materials': {'texts': [], 'videos': [], 'material_animations': []}}
    for i, effect in enumerate(['slide', 'slide', 'fade']):
        tid, node = f'T{i}', f'n{i}'
        p['operations'].append({'id': node, 'kind': 'add_preset_group', 'template_id': tid, 'node_id': node})
        p['visual_events'].append({'id': node, 'start_us': i * 2_000_000, 'end_us': (i + 1) * 2_000_000,
            'primary_visual': 'person', 'audience_need': '测试节点', 'transition': {'intent': 'change'}})
        draft['materials']['texts'].append({'id': node, 'content': '{}'})
        draft['materials']['material_animations'].append({'id': f'a{i}', 'animations': [
            {'type': 'in', 'start': 0, 'duration': 200_000, 'resource_id': effect}]})
        draft['tracks'].append({'type': 'text', 'name': f'JY_PRESET_{tid}__NODE__{node}_01', 'segments': [
            {'material_id': node, 'extra_material_refs': [f'a{i}'],
             'target_timerange': {'start': i * 2_000_000, 'duration': 2_000_000}}]})
    for i in range(3):
        name = f'B{i}'
        p['operations'].append({'id': name, 'kind': 'add_broll', 'source_path': str(source), 'track_name': name})
        draft['materials']['videos'].append({'id': name, 'path': str(source)})
        draft['tracks'].append({'type': 'video', 'name': name, 'attribute': 1, 'segments': [
            {'material_id': name, 'clip': {'alpha': 0 if i == 2 else 1},
             'target_timerange': {'start': 1_000_000 if i < 2 else 4_000_000, 'duration': 2_000_000}}]})
    p['operations'].append({'id': 'unassembled', 'kind': 'add_preset_group', 'template_id': 'missing', 'node_id': 'x'})
    before = deepcopy((p, draft))
    result = summarize_visual_sequence(p, draft)
    assert result['preset_instance_count'] == result['distinct_template_count'] == 3
    assert result['supporting_media_intervals'] == [[1_000_000, 3_000_000]]
    assert result['supporting_media_duration_us'] == 2_000_000
    assert result['without_supporting_media_intervals'] == [[0, 1_000_000], [3_000_000, 6_000_000]]
    assert [r['event_ids'] for r in result['adjacent_repeated_preset_forms']] == [['n0', 'n1']]
    assert not result['native_visual_verified'] and not result['creative_quality_verified']
    assert before == (p, draft)


def v2_plan():
    p = plan()
    p["project_format"]["visual_planning_version"] = 2
    p["visual_events"][0].update(
        primary_visual="person", composition="全程保留人物脸和佩戴动作", techniques=[],
        speech_ids=["s1", "s2"], handbook_refs=["handbook/composition#subject"],
        review_focus="待观察佩戴时手指是否遮挡耳塞以及字幕是否挡住手部")
    p["operations"] = []
    p["content_gate"] = {"final_retained_speech": [
        {"id": "s1", "start": 0, "end": 1.5, "text": "先把耳塞搓细"},
        {"speech_id": "s2", "start_us": 1500000, "end_us": 3000000, "text": "再放进耳朵"},
    ]}
    p["design_decisions"] = {
        domain: {"decision": "omit", "reason": "保留完整人物动作已有足够信息，不添加装饰", "event_ids": []}
        for domain in ("typography", "composition", "motion", "broll", "sound")
    }
    p["design_decisions"]["typography"] = {
        "decision": "use", "reason": "普通字幕承接完整口述", "event_ids": ["comparison"]}
    p['visual_events'][0]['supporting_visual'] = {
        'status': 'not_needed', 'purpose': '本段保留完整操作动作供观察', 'operation_ids': []}
    p['visual_events'].append({
        'id': 'support_frame', 'start_us': 3_000_000, 'end_us': 4_000_000,
        'speech_ids': [], 'audience_need': '查看产品与人物的主次关系', 'primary_visual': 'relationship',
        'composition': '人物蒙版为产品信息让位', 'handbook_refs': ['handbook/composition#support'],
        'review_focus': '人物与资料能否看清', 'techniques': [{'kind': 'mask', 'operation_ids': ['support_mask']}],
        'supporting_visual': {'status': 'ready', 'purpose': '人物窗口给产品信息留出阅读区', 'operation_ids': ['support_mask']}})
    # File-presence fixture only; native mask generation/resources have separate tests.
    p['operations'].append({'id': 'support_mask', 'kind': 'apply_video_mask', 'track_name': 'support_person',
        'segment_id': 'support_segment', 'shape': 'rectangle', 'native_reference': {'path': str(Path(__file__).resolve())}})
    p['design_decisions']['composition'] = {'decision': 'use', 'reason': '人物为资料让位', 'event_ids': ['support_frame']}
    return p


def test_v2_whole_film_aroll_and_captions_remains_incomplete_despite_reason():
    p = v2_plan()
    p['visual_events'].pop()
    p['operations'] = []
    p['design_decisions']['composition'].update(decision='omit', event_ids=[])
    before = deepcopy(p)
    r = validate_visual_events(p)
    assert not r['ok'] and any('完整包装缺少' in error for error in r['errors'])
    assert r["native_visual_verified"] is False and r["creative_quality_verified"] is False
    assert p == before


def test_v2_rejects_retained_speech_without_an_event_reference():
    p = v2_plan()
    # Timeline still spans all speech; a missing explicit second-sentence
    # design cannot be concealed by the first event's broad time interval.
    p["visual_events"][0]["speech_ids"] = ["s1"]
    assert any("s2" in error and "未完整覆盖" in error for error in validate_visual_events(p)["errors"])


def test_v2_rejects_unknown_speech_reference():
    p = v2_plan()
    p["visual_events"][0]["speech_ids"].append("invented")
    assert any("未知 speech_id" in error for error in validate_visual_events(p)["errors"])


@pytest.mark.parametrize("defect", ["head", "tail", "gap", None])
def test_v2_speech_can_cross_events_but_union_must_cover_every_microsecond(defect):
    p = v2_plan()
    first = p["visual_events"][0]
    second = deepcopy(first)
    first.update(end_us=1000000, speech_ids=["s1"])
    second.update(id="wearing", start_us=1000000, speech_ids=["s1", "s2"])
    p["visual_events"].append(second)
    if defect == "head": first["start_us"] = 1
    if defect == "tail": second["end_us"] -= 1
    if defect == "gap": second["start_us"] += 1
    result = validate_visual_events(p)
    assert result["ok"] is (defect is None), result["errors"]


def test_v2_uses_existing_index_fallback_and_parsed_content_gate_without_io():
    p = v2_plan()
    gate = p.pop("content_gate")
    gate["final_retained_speech"][0].pop("id")
    p["visual_events"][0]["speech_ids"] = [0, "s2"]
    assert validate_visual_events(p, content_gate=gate)["ok"]
    assert not validate_visual_events(p)["ok"]


@pytest.mark.parametrize("field,value", [("handbook_refs", []), ("handbook_refs", [""]),
                                         ("review_focus", " "), ("speech_ids", None)])
def test_v2_requires_review_intent_and_handbook_references(field, value):
    p = v2_plan()
    p["visual_events"][0][field] = value
    assert not validate_visual_events(p)["ok"]


@pytest.mark.parametrize("domain", ["typography", "composition", "motion", "broll", "sound"])
def test_v2_each_design_domain_requires_a_decision(domain):
    p = v2_plan()
    del p["design_decisions"][domain]
    assert any(domain in error for error in validate_visual_events(p)["errors"])


@pytest.mark.parametrize("domain", ["motion", "broll", "sound"])
def test_v2_use_requires_corresponding_technique_on_referenced_event(domain):
    p = v2_plan()
    p["design_decisions"][domain].update(decision="use", event_ids=["comparison"])
    assert any(domain in error and "对应技术" in error for error in validate_visual_events(p)["errors"])


def test_stable_composition_is_a_design_without_artificial_effect_operations():
    p = v2_plan()
    p['design_decisions']['composition'].update(decision='use', event_ids=['comparison'],
                                               reason='人物半身与字幕共同安排，完整保留佩戴动作')
    assert validate_visual_events(p)['ok']
    assert p['visual_events'][0]['techniques'] == []


@pytest.mark.parametrize('sound,motion', [(False, False), (True, False), (False, True), (True, True)])
def test_native_preset_effects_are_decided_from_real_compiled_tracks(sound, motion, tmp_path):
    from jianying_adapter.visual_planning import validate_visual_execution
    p = v2_plan()
    p['operations'] += [{'id': 'preset', 'kind': 'add_preset_group', 'template_id': 'A', 'node_id': 'q'}]
    p['visual_events'][0]['techniques'] = [{'kind': 'text_preset', 'operation_ids': ['preset']}]
    for domain in ('sound', 'motion'):
        p['design_decisions'][domain].update(decision='use', event_ids=['comparison'])
    assert validate_visual_events(p)['ok']  # native bundle evidence is deferred to assembly
    draft = {'tracks': [{'name': 'JY_PRESET_A__NODE__q_01', 'type': 'text', 'segments': [
        {'target_timerange': {'start': 0, 'duration': 3_000_000}, 'extra_material_refs': ['intro']}]}],
        'materials': {'material_animations': [{'id': 'intro', 'animations': []}], 'audios': []}}
    support = support_draft()
    draft['tracks'].extend(support['tracks'])
    draft['materials'].update(support['materials'])
    if motion:
        draft['materials']['material_animations'][0]['animations'].append(
            {'type': 'in', 'start': 0, 'duration': 300_000, 'resource_id': 'native-original'})
    if sound:
        import wave
        audio_path = tmp_path / 'bound-local-sound.wav'
        with wave.open(str(audio_path), 'wb') as handle:
            handle.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
            handle.writeframes(b'\0\0' * 1600)
        draft['materials']['audios'].append({'id': 'sfx', 'path': str(audio_path)})
        draft['tracks'].append({'name': 'JY_PRESET_A__NODE__q_AUX_AUDIO_01', 'type': 'audio', 'segments': [
            {'material_id': 'sfx', 'target_timerange': {'start': 0, 'duration': 200_000}, 'volume': .5}]})
    result = validate_visual_execution(p, draft)
    assert result['ok'] is (sound and motion)
    assert ('sound' in result['preset_effects_by_event']['comparison']) is sound
    assert ('motion' in result['preset_effects_by_event']['comparison']) is motion
    if sound or motion:
        for domain in ('sound', 'motion'):
            p['design_decisions'][domain].update(decision='omit', event_ids=[])
        p['visual_events'][0]['techniques'] = []
        assert not validate_visual_execution(p, draft)['ok']
    assert result['creative_quality_verified'] is False


def test_native_loop_motion_interval_covers_the_text_span_not_one_cycle():
    from jianying_adapter.visual_planning import _preset_effect_intervals
    draft = {'tracks': [{'name': 'JY_PRESET_T__NODE__loop_01', 'type': 'text', 'segments': [
        {'target_timerange': {'start': 1000000, 'duration': 5000000}, 'extra_material_refs': ['loop']}]}],
        'materials': {'material_animations': [{'id': 'loop', 'animations': [
            {'type': 'loop', 'start': 0, 'duration': 500000, 'resource_id': 'native-ring'}]}]}}
    assert _preset_effect_intervals(draft, 'JY_PRESET_T__NODE__loop_')['motion'] == [(1000000, 6000000)]
    draft['tracks'][0]['segments'][0]['visible'] = False
    assert not _preset_effect_intervals(draft, 'JY_PRESET_T__NODE__loop_')['motion']


def test_text_animation_supports_motion_without_extra_keyframes():
    p = v2_plan()
    p['operations'] += [{'id': 'intro', 'kind': 'apply_text_animation', 'visual_event_id': 'comparison'}]
    p['visual_events'][0]['techniques'] = [{'kind': 'text_animation', 'operation_ids': ['intro']}]
    p['design_decisions']['motion'].update(decision='use', event_ids=['comparison'])
    assert validate_visual_events(p)['ok']


def test_v2_a_declared_technique_still_needs_its_matching_operation():
    p = v2_plan()
    p["design_decisions"]["sound"].update(decision="use", event_ids=["comparison"])
    p["visual_events"][0]["techniques"] = [{"kind": "sound", "operation_ids": ["missing_mix"]}]
    assert any("未连接匹配操作" in error for error in validate_visual_events(p)["errors"])
    p["operations"] += [{"id": "missing_mix", "kind": "mix_action_audio"}]
    assert validate_visual_events(p)["ok"]


@pytest.mark.parametrize("technique,operation,domains", [
    ("media", "add_broll", ["composition", "broll"]),
    ("keyframes", "animate_aroll_transform", ["composition", "motion"]),
    ("sound", "mix_action_audio", ["sound"]),
    ("text_preset", "add_preset_group", ["typography"]),
    ("mask", "apply_video_mask", ["composition"]),
])
def test_v2_selected_technique_cannot_be_marked_omit(technique, operation, domains):
    p = v2_plan()
    for domain in domains:
        p["design_decisions"][domain].update(decision="omit", event_ids=[])
    p["operations"] = [{"id": "chosen", "kind": operation, "visual_event_id": "comparison"}]
    p["visual_events"][0]["techniques"] = [{"kind": technique, "operation_ids": ["chosen"]}]
    errors = validate_visual_events(p)["errors"]
    for domain in domains:
        assert any(domain in error and "omit 矛盾" in error for error in errors)
    # Removing the event's technique cannot hide an executable choice either.
    p["visual_events"][0]["techniques"] = []
    assert any("omit 矛盾" in error for error in validate_visual_events(p)["errors"])


@pytest.mark.parametrize("change", ["unknown_event", "no_event", "no_reason"])
def test_v2_design_use_requires_valid_event_and_reason(change):
    p = v2_plan()
    decision = p["design_decisions"]["typography"]
    if change == "unknown_event": decision["event_ids"] = ["invented"]
    if change == "no_event": decision["event_ids"] = []
    if change == "no_reason": decision["reason"] = " "
    assert not validate_visual_events(p)["ok"]


def test_v2_static_video_effect_does_not_force_motion_use():
    p = v2_plan()
    p["visual_events"][0]["techniques"] = [{"kind": "video_effect", "operation_ids": ["static_blur"]}]
    p["operations"] += [{"id": "static_blur", "kind": "add_video_effect", "visual_event_id": "comparison"}]
    p["design_decisions"]["motion"].update(reason="静态模糊背景不产生运动，保留人物原始动作")
    assert validate_visual_events(p)["ok"]
    # The validator also accepts an explicitly planned effect-based motion;
    # native review, not the effect kind alone, determines the actual behavior.
    p["design_decisions"]["motion"].update(decision="use", event_ids=["comparison"], reason="使用已选择效果，待原生观察实际运动")
    assert validate_visual_events(p)["ok"]


def test_v2_silent_event_can_have_explicit_empty_speech_ids_but_not_omit_field():
    p = v2_plan()
    silent = deepcopy(p["visual_events"][0])
    silent.update(id="silent_evidence", start_us=3000000, end_us=4000000,
                  speech_ids=[], primary_visual="person", composition="保留无口述产品展示",
                  review_focus="待观察无口述停留是否足以看清耳塞外形")
    p["visual_events"].append(silent)
    assert validate_visual_events(p)["ok"]
    del silent["speech_ids"]
    assert any("speech_ids" in error for error in validate_visual_events(p)["errors"])


def support_draft():
    """Native-shaped visibility fixture; media probing belongs to add_broll tests."""
    return {'tracks': [{'type': 'video', 'name': 'support_person', 'segments': [{
        'id': 'support_segment', 'material_id': 'source', 'extra_material_refs': ['mask'],
        'target_timerange': {'start': 3_000_000, 'duration': 1_000_000},
        'clip': {'alpha': 1, 'scale': {'x': 1, 'y': 1}}, 'enable_adjust_mask': False}]}],
        'materials': {'videos': [{'id': 'source', 'path': str(Path(__file__).resolve())}],
            'common_mask': [{'id': 'mask', 'type': 'mask', 'resource_type': 'rectangle', 'resource_id': 'native-fixture',
                'path': str(Path(__file__).parent), 'config': {'width': .5, 'height': .5}}]}}


@pytest.mark.parametrize('defect', ['missing_decision', 'missing_purpose', 'empty_operations', 'wrong_operation',
    'missing_file', 'needs_asset', 'false_not_needed'])
def test_supporting_visual_work_cannot_be_omitted_or_declared_ready_without_resources(defect):
    p = v2_plan()
    event = p['visual_events'][-1]
    support = event['supporting_visual']
    if defect == 'missing_decision': del event['supporting_visual']
    elif defect == 'missing_purpose': support['purpose'] = ''
    elif defect == 'empty_operations': support['operation_ids'] = []
    elif defect == 'wrong_operation': p['operations'][0]['kind'] = 'animate_aroll_transform'
    elif defect == 'missing_file': p['operations'][0]['native_reference']['path'] += '.missing'
    elif defect == 'needs_asset':
        support.update(status='needs_asset', asset_search={'queries': ['本地产品近景', '公开产品演示'], 'next_action': '查找可用素材或制作准确示意画面'})
    else: support.update(status='not_needed', operation_ids=[])
    report = validate_visual_events(p)
    assert not report['ok']
    assert any('辅助画面' in error for error in report['errors'])


@pytest.mark.parametrize('defect', [None, 'missing_track', 'wrong_segment', 'missing_mask', 'missing_resource',
    'hidden_track', 'hidden_segment', 'transparent', 'short_interval', 'missing_media', 'disabled_mask'])
def test_supporting_visual_execution_and_writer_readback_require_actual_visible_content(defect):
    from jianying_adapter.visual_planning import validate_visual_execution
    p, draft = v2_plan(), support_draft()
    segment = draft['tracks'][0]['segments'][0]
    if defect == 'missing_track': draft['tracks'] = []
    elif defect == 'wrong_segment': segment['id'] = 'wrong'
    elif defect == 'missing_mask': segment['extra_material_refs'] = []
    elif defect == 'missing_resource': draft['materials']['common_mask'][0]['path'] = ''
    elif defect == 'hidden_track': draft['tracks'][0]['visible'] = False
    elif defect == 'hidden_segment': segment['visible'] = False
    elif defect == 'transparent': segment['clip']['alpha'] = 0
    elif defect == 'short_interval': segment['target_timerange']['duration'] -= 100_000
    elif defect == 'missing_media': draft['materials']['videos'][0]['path'] += '.missing'
    elif defect == 'disabled_mask': segment['enable_video_mask'] = False
    report = validate_visual_execution(p, draft)
    assert report['ok'] is (defect is None), report['errors']
    assert report['native_visual_verified'] is False and report['creative_quality_verified'] is False


@pytest.mark.parametrize('media_type', ['photo', 'video'])
@pytest.mark.parametrize('defect', [None, 'transparent', 'missing_source', 'hidden'])
def test_silent_broll_is_visible_but_still_requires_visible_existing_media(tmp_path, media_type, defect):
    from jianying_adapter.visual_planning import validate_visual_execution
    p, draft = v2_plan(), support_draft()
    source = tmp_path / ('cover.png' if media_type == 'photo' else 'clip.mp4')
    source.write_bytes(b'visibility fixture; native probing is tested by add_broll')
    event = p['visual_events'][-1]
    operation_id = event['supporting_visual']['operation_ids'][0]
    p['operations'] = [{'id': operation_id, 'kind': 'add_broll', 'track_name': 'support_person',
                        'segment_id': 'support_segment', 'source_path': str(source)}]
    event['techniques'] = [{'kind': 'media', 'operation_ids': [operation_id]}]
    track = draft['tracks'][0]
    track['attribute'] = 1  # Actual Track(..., mute=True).export_json() convention.
    segment = track['segments'][0]
    segment.update(volume=0, extra_material_refs=[])
    draft['materials']['videos'][0].update(type=media_type, path=str(source))
    if defect == 'transparent': segment['clip']['alpha'] = 0
    elif defect == 'missing_source': draft['materials']['videos'][0]['path'] += '.missing'
    elif defect == 'hidden': segment['visible'] = False
    result = validate_visual_execution(p, draft)
    assert result['ok'] is (defect is None), result['errors']
    assert result['native_visual_verified'] is False

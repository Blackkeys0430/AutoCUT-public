import copy
import hashlib
import json
import wave
from types import SimpleNamespace

import pytest

from jianying_adapter.action_audio import _native, mix_action_audio, validate_action_audio
from jianying_adapter.candidate_plan import validate_timeline_equivalence
from jianying_adapter.preset_audio import apply_preset_gain, audio_rows, is_preset_audio, validate_preset_audio
from jianying_adapter.preset_timing import retime_preset_text_tracks
from jianying_adapter.shared_operations import add_preset_group, place_preset_group
from test_five_template_trial import _base, _candidate, _inner, _text_slot


def case(tmp_path, *, audio_only_inner=False, first_duration_us=800_000):
    path = tmp_path / 'author.wav'
    with wave.open(str(path), 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(48000)
        stream.writeframes(b'\x10\x00' * 48000)
    native = _native()
    inner = _inner('A', ['第一', '第二'], duration=2_000_000)
    inner['tracks'][0]['segments'][0]['target_timerange']['duration'] = first_duration_us
    inner['tracks'][1]['segments'][0]['target_timerange'] = {'start': 800_000, 'duration': 800_000}
    inner['materials'].update(speeds=[], audio_fades=[])
    audio_inner = _inner('S', [], duration=2_000_000) if audio_only_inner else inner
    audio_inner['materials'].setdefault('speeds', [])
    for i, (start, source_start, volume) in enumerate([(0, 100_000, .42), (800_000, 200_000, .6)]):
        material = native.AudioMaterial(str(path))
        segment = native.AudioSegment(material, native.trange(start, 200_000),
                                      source_timerange=native.trange(source_start, 200_000), volume=volume)
        audio_inner['materials']['audios'].append(material.export_json())
        audio_inner['materials']['speeds'].append(segment.speed.export_json())
        audio_inner['tracks'].append({'id': f'audio-{i}', 'name': 'source', 'type': 'audio',
                                      'attribute': 0, 'flag': 0, 'segments': [segment.export_json()]})
    inners = [inner, audio_inner] if audio_only_inner else [inner]
    candidate, payload = _candidate('A', 'preset.json', inners, {'text': [
        _text_slot('first', 0, 0, 'A-TEXT-MAT-0'), _text_slot('second', 0, 1, 'A-TEXT-MAT-1')]})
    (tmp_path / 'preset.json').write_text(json.dumps(payload), encoding='utf-8')
    registry = tmp_path / 'registry.json'
    registry.write_text(json.dumps({'candidates': [candidate]}), encoding='utf-8')
    state = tmp_path / 'state.json'
    state.write_text(json.dumps({'current_authorizations': [
        {'option': 'visual_packaging_revision', 'enabled': True, 'source': 'current'},
        {'option': 'action_sfx', 'enabled': True, 'source': 'current'}]}), encoding='utf-8')
    spec = {'id': 'add', 'kind': 'add_preset_group', 'template_id': 'A', 'node_id': 'list',
            'start_us': 500_000, 'end_us': 3_500_000, 'slots': ['一', '二']}
    selection = {**spec, 'actual_text_tracks': [
        {'track_name': 'JY_PRESET_A__NODE__list_01', 'start_us': 500_000, 'end_us': 500_000 + first_duration_us, 'segment_count': 1},
        {'track_name': 'JY_PRESET_A__NODE__list_02', 'start_us': 1_300_000, 'end_us': 2_100_000, 'segment_count': 1}]}
    plan = {'preset_registry': str(registry), 'preset_root': str(tmp_path), 'presets': [selection],
            'operations': [spec], 'project_state': str(state), 'audio_authorization_source': 'current'}
    return _base(4_000_000), spec, SimpleNamespace(plan=plan, plan_path=tmp_path / 'plan.json'), path


@pytest.mark.parametrize('audio_only_inner', [False, True])
def test_shared_import_keeps_authored_audio_tracks_source_slices_and_relative_times(tmp_path, audio_only_inner):
    base, spec, context, path = case(tmp_path, audio_only_inner=audio_only_inner)
    before = copy.deepcopy(base)
    result, report = add_preset_group(base, spec, context)
    audio = [t for t in result['tracks'] if is_preset_audio(t)]
    assert len(audio) == 2 and len({t['name'] for t in audio}) == 2
    assert [t['segments'][0]['target_timerange'] for t in audio] == [
        {'start': 500_000, 'duration': 200_000}, {'start': 1_300_000, 'duration': 200_000}]
    assert [t['segments'][0]['source_timerange']['start'] for t in audio] == [100_000, 200_000]
    assert [t['segments'][0]['volume'] for t in audio] == [.42, .6]
    assert report['original_audio_segment_count'] == 2
    assert validate_preset_audio(context.plan, result, plan_path=context.plan_path)['ok']
    assert base == before
    with pytest.raises(ValueError, match='already exists'):
        add_preset_group(result, spec, context)


@pytest.mark.parametrize('explicit_binding', [False, True])
def test_retiming_one_word_moves_only_its_original_cue_and_writer_detects_drift(tmp_path, explicit_binding):
    base, spec, context, path = case(tmp_path, first_duration_us=800_000 if explicit_binding else 1_000_000)
    result, _ = add_preset_group(base, spec, context)
    row = {'track_name': 'JY_PRESET_A__NODE__list_02', 'start_us': 1_600_000, 'end_us': 2_700_000}
    context.plan['presets'][0]['actual_text_tracks'][1] = {**row, 'segment_count': 1}
    timing = {'id': 'timing', 'kind': 'retime_preset_text_tracks', 'template_id': 'A', 'node_id': 'list',
              'authorization_source': 'current', 'design_reason': '第二项对齐原声', 'tracks': [row],
              'audio_bindings': [{'audio_track': 'JY_PRESET_A__NODE__list_AUX_AUDIO_02',
                                  'segment_index': 0, 'text_track': row['track_name'], 'anchor': 'start'}]}
    if not explicit_binding:
        timing.pop('audio_bindings')
    context.plan['operations'].append(timing)
    changed, report = retime_preset_text_tracks(result, timing, context)
    audio = [t for t in changed['tracks'] if is_preset_audio(t)]
    assert [t['segments'][0]['target_timerange']['start'] for t in audio] == [500_000, 1_600_000]
    assert [t['segments'][0]['source_timerange']['start'] for t in audio] == [100_000, 200_000]
    assert report['original_audio'][1]['after_start_us'] == 1_600_000
    assert validate_preset_audio(context.plan, changed, plan_path=context.plan_path)['ok']
    audio[1]['segments'][0]['target_timerange']['start'] -= 1
    assert not validate_preset_audio(context.plan, changed, plan_path=context.plan_path)['ok']


def test_ambiguous_shared_visual_anchor_requires_binding_without_mutating_input(tmp_path):
    base, spec, context, _ = case(tmp_path)
    result, _ = add_preset_group(base, spec, context)
    first = next(t for t in result['tracks'] if t['name'] == 'JY_PRESET_A__NODE__list_01')
    first['segments'][0]['target_timerange']['duration'] = 800_000
    before = copy.deepcopy(result)
    timing = {'id': 'timing', 'kind': 'retime_preset_text_tracks', 'template_id': 'A', 'node_id': 'list',
              'authorization_source': 'current', 'design_reason': 'second word moves independently',
              'tracks': [{'track_name': 'JY_PRESET_A__NODE__list_02', 'start_us': 1_600_000, 'end_us': 2_700_000}]}
    context.plan['presets'][0]['actual_text_tracks'][1] = {**timing['tracks'][0], 'segment_count': 1}
    with pytest.raises(ValueError, match='ambiguous authored cue'):
        retime_preset_text_tracks(result, timing, context)
    assert result == before


def test_different_nodes_can_reuse_author_sound_but_extra_event_cannot_double_same_cue(tmp_path):
    base, spec, context, path = case(tmp_path)
    base['duration'] = 8_000_000
    result, _ = add_preset_group(base, spec, context)
    another = {**spec, 'id': 'second', 'node_id': 'another', 'start_us': 4_500_000, 'end_us': 7_500_000}
    context.plan['presets'].append(dict(another))
    context.plan['operations'].append(another)
    result, _ = add_preset_group(result, another, context)
    report = validate_preset_audio(context.plan, result, plan_path=context.plan_path)
    assert report['ok'] and report['segment_count'] == 4
    result['tracks'].insert(0, {'name': 'JY_ROUGH_CUT_VIDEO', 'type': 'video', 'segments': [
        {'volume': 1., 'target_timerange': {'start': 0, 'duration': 8_000_000}, 'common_keyframes': []}]})
    context.plan['visual_events'] = [{'id': 'event', 'start_us': 500_000, 'end_us': 3_500_000,
                                    'techniques': [{'kind': 'text_preset', 'operation_ids': ['add']}]}]
    event = {'id': 'duplicate', 'source_path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
             'source_start_us': 100_000, 'start_us': 500_000, 'duration_us': 200_000, 'gain_db': -6,
             'fade_in_us': 0, 'fade_out_us': 0, 'visual_event_id': 'event', 'action_operation_id': 'add',
             'action_time_us': 500_000, 'anchor_offset_us': 0, 'reason': 'same source and onset as original cue'}
    mix = {'id': 'mix', 'kind': 'mix_action_audio', 'authorization_source': 'current',
           'dialogue_gain_db': 0, 'events': [event]}
    before = copy.deepcopy(result)
    with pytest.raises(ValueError, match='duplicates an original preset cue'):
        mix_action_audio(result, mix, context)
    assert result == before


def test_candidate_timeline_and_visual_execution_include_original_audio(tmp_path):
    from test_visual_planning import v2_plan, support_draft
    base, spec, context, _ = case(tmp_path)
    spec['end_us'] = context.plan['presets'][0]['end_us'] = 2_100_000
    for row, text in zip(context.plan['presets'][0]['actual_text_tracks'], ['一', '二']):
        row['text'] = text
    context.plan.update(project_format={'visual_planning_version': 2},
                        visual_events=[{'id': 'event', 'start_us': 500_000, 'end_us': 2_100_000,
                                        'techniques': [{'kind': 'text_preset', 'operation_ids': ['add']}]}],
                        design_decisions={'motion': {'decision': 'omit'},
                                          'sound': {'decision': 'use', 'event_ids': ['event']}})
    result, _ = add_preset_group(base, spec, context)
    design, supporting = v2_plan(), support_draft()
    context.plan['visual_events'][0]['supporting_visual'] = {
        'status': 'not_needed', 'purpose': '本段由当前文字与原配音频承接', 'operation_ids': []}
    context.plan['visual_events'].append(design['visual_events'][-1])
    context.plan['operations'].extend(design['operations'])
    result['tracks'].extend(supporting['tracks'])
    for bucket, entries in supporting['materials'].items():
        result['materials'].setdefault(bucket, []).extend(entries)
    report = validate_timeline_equivalence(context.plan, result, plan_path=context.plan_path)
    assert report['ok'], report['errors']
    assert report['preset_audio']['segment_count'] == 2
    assert report['visual_execution']['preset_effects_by_event']['event'] == ['sound']
    result['tracks'].remove(next(t for t in result['tracks'] if is_preset_audio(t)))
    rejected = validate_timeline_equivalence(context.plan, result, plan_path=context.plan_path)
    assert not rejected['ok'] and not rejected['preset_audio']['ok']


@pytest.mark.parametrize('mutation', ['missing_track', 'duplicate_track', 'source_slice', 'volume', 'muted', 'file_missing'])
def test_original_audio_readback_rejects_loss_duplication_and_source_changes(tmp_path, mutation):
    base, spec, context, path = case(tmp_path)
    result, _ = add_preset_group(base, spec, context)
    audio = next(t for t in result['tracks'] if is_preset_audio(t))
    if mutation == 'missing_track': result['tracks'].remove(audio)
    elif mutation == 'duplicate_track': result['tracks'].append(copy.deepcopy(audio))
    elif mutation == 'source_slice': audio['segments'][0]['source_timerange']['start'] += 1
    elif mutation == 'volume': audio['segments'][0]['volume'] = 1
    elif mutation == 'muted': audio['attribute'] = 1
    else: path.rename(path.with_suffix('.missing'))
    assert not validate_preset_audio(context.plan, result, plan_path=context.plan_path)['ok']


def test_group_placement_and_dialogue_mix_preserve_original_audio(tmp_path):
    base, spec, context, path = case(tmp_path)
    context.plan['target'] = {'width': 1080, 'height': 1920}
    result, _ = add_preset_group(base, spec, context)
    original = audio_rows(result)
    place_preset_group(result, {'template_id': 'A', 'node_id': 'list',
                               'final_position': {'x': 540, 'y': 500}, 'final_group_scale': .7}, context)
    assert audio_rows(result) == original
    result['tracks'].insert(0, {'name': 'JY_ROUGH_CUT_VIDEO', 'type': 'video', 'segments': [
        {'volume': 1., 'target_timerange': {'start': 0, 'duration': 4_000_000}, 'common_keyframes': []}]})
    mix = {'id': 'mix', 'kind': 'mix_action_audio', 'authorization_source': 'current',
           'dialogue_gain_db': 3, 'events': []}
    context.plan['operations'].append(mix)
    mixed, report = mix_action_audio(result, mix, context)
    assert audio_rows(mixed) == original
    assert report['events_written'] == 0 and report['original_preset_audio_preserved'] == 2
    assert mixed['tracks'][0]['segments'][0]['volume'] == pytest.approx(10 ** (3/20))
    assert validate_action_audio(context.plan, mixed)['ok']
    assert validate_preset_audio(context.plan, mixed, plan_path=context.plan_path)['ok']


def test_missing_local_original_sound_fails_instead_of_silently_importing_text_only(tmp_path):
    base, spec, context, path = case(tmp_path)
    path.rename(path.with_suffix('.missing'))
    before = copy.deepcopy(base)
    with pytest.raises(ValueError):
        add_preset_group(base, spec, context)
    assert base == before


def test_authored_audio_cannot_be_trimmed_to_fit_a_short_node(tmp_path):
    base, spec, context, path = case(tmp_path)
    spec['end_us'] = 1_400_000
    with pytest.raises(ValueError, match='原配音频'):
        add_preset_group(base, spec, context)


def test_non_audio_bytes_with_audio_extension_are_rejected(tmp_path):
    base, spec, context, path = case(tmp_path)
    path.write_text('{"type":"animation prefab, not sound"}', encoding='utf-8')
    with pytest.raises(ValueError):
        add_preset_group(base, spec, context)


@pytest.fixture
def existing_four_item_preset(tmp_path):
    """Import actual author audio once per test from the frozen local library."""
    import shutil
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    registry = root / 'jianying-adapter/preset_catalog/all_auto_candidates_v1.json'
    inventory = root / 'jianying-adapter/repair_workspaces/phase3_remote_resource_rehydration_20260903/resource_inventory_phase3.json'
    if not registry.is_file() or not inventory.is_file() or not shutil.which('ffmpeg'):
        pytest.skip('local preset library/audio resources unavailable')
    candidate = next(c for c in json.loads(registry.read_text('utf-8-sig'))['candidates']
                     if c['template_id'] == 'JIANYING-25-01')
    preset_root = root / 'jianying_presets/剪映1000个高级感字幕预设'
    source = preset_root / candidate['source_path']
    if not source.is_file():
        pytest.skip('original local four-item preset unavailable')
    inner = json.loads(source.read_text('utf-8-sig'))['materials']['drafts'][0]['draft']
    lookup = {r['original_path']: r.get('resolution', {}).get('frozen_path')
              for r in json.loads(inventory.read_text('utf-8-sig'))['records']}
    paths = {m['path']: lookup.get(m['path']) for m in inner['materials']['audios']}
    if any(not path or not Path(path).is_file() for path in paths.values()):
        pytest.skip('original local sound cache unavailable')
    spec = {'id': 'list', 'kind': 'add_preset_group', 'template_id': candidate['template_id'],
            'node_id': 'native_audio', 'start_us': 1_000_000, 'end_us': 6_000_000,
            'slots': ['1.字幕', '2.音效', '3.画面', '4.节奏']}
    plan = {'preset_registry': str(registry), 'preset_root': str(preset_root), 'audio_path_map': paths,
            'presets': [dict(spec)], 'operations': [spec]}
    context = SimpleNamespace(plan=plan, plan_path=tmp_path/'plan.json')
    result, report = add_preset_group(_base(7_000_000), spec, context)
    return plan, result, context, report


def test_existing_four_item_preset_import_and_offline_audio_decode(tmp_path, existing_four_item_preset):
    """Exercise the actual local library when its frozen resources are present."""
    import importlib.util
    import shutil
    from pathlib import Path
    plan, result, context, report = existing_four_item_preset
    root = Path(__file__).resolve().parents[2]
    assert report['original_audio_track_count'] == 2 and report['original_audio_segment_count'] == 4
    audio = [s for t in result['tracks'] if is_preset_audio(t) for s in t['segments']]
    assert sorted(s['target_timerange']['start'] for s in audio) == [1_000_000, 1_366_666, 1_866_666, 2_433_333]
    assert validate_preset_audio(plan, result, plan_path=context.plan_path)['ok']
    renderer = root / 'jianying-adapter/scripts/render_candidate_audio.py'
    module_spec = importlib.util.spec_from_file_location('original_audio_renderer', renderer)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    preview = tmp_path/'native_audio_preview.wav'
    report = module.render(plan, result, shutil.which('ffmpeg'), preview, plan_path=context.plan_path)
    assert preview.stat().st_size > 1000 and report['audio_sources'] == 4
    assert report['original_preset_audio']['segment_count'] == 4
    assert report['native_listening_verified'] is False


def test_declared_preset_attenuation_preserves_sources_and_detects_tampering(tmp_path):
    base, spec, context, path = case(tmp_path)
    result, _ = add_preset_group(base, spec, context)
    original = audio_rows(result)
    result['tracks'].insert(0, {'name': 'JY_ROUGH_CUT_VIDEO', 'type': 'video', 'segments': [
        {'volume': 1., 'target_timerange': {'start': 0, 'duration': 4_000_000}, 'common_keyframes': []}]})
    mix = {'id': 'mix', 'kind': 'mix_action_audio', 'authorization_source': 'current',
           'dialogue_gain_db': 3, 'preset_gain_db': -12, 'events': []}
    context.plan['operations'].append(mix)
    mixed, report = mix_action_audio(result, mix, context)
    adjusted = audio_rows(mixed)
    for name in original:
        for before, after in zip(original[name]['segments'], adjusted[name]['segments']):
            assert after['volume'] == pytest.approx(before['volume'] * 10 ** (-12/20))
            after['volume'] = before['volume']
    assert adjusted == original
    assert validate_preset_audio(context.plan, mixed, plan_path=context.plan_path)['ok']
    cue = next(t for t in mixed['tracks'] if is_preset_audio(t))['segments'][0]
    cue['volume'] *= 2
    assert not validate_preset_audio(context.plan, mixed, plan_path=context.plan_path)['ok']
    mix['preset_gain_db'] = 1
    with pytest.raises(ValueError):
        mix_action_audio(result, mix, context)


def test_per_track_gain_overrides_default_and_writer_rebuilds_same_mix(tmp_path):
    base, spec, context, _ = case(tmp_path)
    result, _ = add_preset_group(base, spec, context)
    original = audio_rows(result)
    names = list(original)
    result['tracks'].insert(0, {'name': 'JY_ROUGH_CUT_VIDEO', 'type': 'video', 'segments': [
        {'volume': 1., 'target_timerange': {'start': 0, 'duration': 4_000_000}, 'common_keyframes': []}]})
    before = copy.deepcopy(result)
    overrides = {names[0]: -3}
    mix = {'id': 'mix', 'kind': 'mix_action_audio', 'authorization_source': 'current',
           'dialogue_gain_db': 0, 'preset_gain_db': -12, 'preset_track_gains_db': overrides, 'events': []}
    context.plan['operations'].append(mix)
    mixed, report = mix_action_audio(result, mix, context)
    assert result == before
    assert report['preset_track_gains_db'] == overrides
    adjusted = audio_rows(mixed)
    for name in names:
        db = -3 if name == names[0] else -12
        for authored, actual in zip(original[name]['segments'], adjusted[name]['segments']):
            assert actual['volume'] == pytest.approx(authored['volume'] * 10 ** (db / 20))
            actual['volume'] = authored['volume']
    assert adjusted == original  # Source, timing, speed, fades and metadata are unchanged.
    assert validate_preset_audio(context.plan, mixed, plan_path=context.plan_path)['ok']
    mix['preset_track_gains_db'][names[0]] = -6
    assert not validate_preset_audio(context.plan, mixed, plan_path=context.plan_path)['ok']


def _static_gain_draft():
    return {'tracks': [
        {'name': 'JY_PRESET_A__NODE__one_AUX_AUDIO_01', 'type': 'audio',
         'segments': [{'volume': .42}, {'volume': .6}]},
        {'name': 'JY_PRESET_A__NODE__two_AUX_AUDIO_01', 'type': 'audio', 'segments': [{'volume': .8}]},
        {'name': 'JY_SFX_other', 'type': 'audio', 'segments': [{'volume': 1.}]},
    ]}


@pytest.mark.parametrize('overrides', [
    None, [], {'missing': -3}, {'JY_SFX_other': -3}, {1: -3},
    *[{'JY_PRESET_A__NODE__two_AUX_AUDIO_01': value}
      for value in (True, '-3', float('nan'), float('inf'), -61, 1)],
])
def test_bad_track_gain_map_rejected_without_partial_changes(overrides):
    draft = _static_gain_draft()
    before = copy.deepcopy(draft)
    with pytest.raises(ValueError):
        apply_preset_gain(draft, {'preset_gain_db': -12, 'preset_track_gains_db': overrides})
    assert draft == before


@pytest.mark.parametrize('conflict', ['volume_keyframes', 'keyframe_refs', 'invalid_volume', 'duplicate_name'])
def test_late_preset_gain_conflicts_do_not_attenuate_earlier_cues(conflict):
    draft = _static_gain_draft()
    second = draft['tracks'][1]
    if conflict == 'volume_keyframes':
        second['segments'][0]['common_keyframes'] = [{'property_type': 'KFTypeVolume'}]
    elif conflict == 'keyframe_refs':
        second['segments'][0]['keyframe_refs'] = ['unresolved-volume-automation']
    elif conflict == 'invalid_volume':
        second['segments'][0]['volume'] = 'not-a-volume'
    else:
        second['name'] = draft['tracks'][0]['name']
    before = copy.deepcopy(draft)
    with pytest.raises(ValueError):
        apply_preset_gain(draft, {'preset_gain_db': -12})
    assert draft == before

import copy
import hashlib
import json
import wave
from types import SimpleNamespace

import pytest

from jianying_adapter.action_audio import mix_action_audio, validate_action_audio
from jianying_adapter.candidate_plan import validate_timeline_equivalence
from jianying_adapter.visual_planning import validate_visual_events


def case(tmp_path):
    path = tmp_path / 'sound.wav'
    with wave.open(str(path), 'wb') as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(48000)
        out.writeframes(b'\x00\x00' * 48000)
    state = tmp_path / 'state.json'
    state.write_text(json.dumps({'current_authorizations': [{'option': 'action_sfx', 'enabled': True, 'source': 'user'}]}))
    event = dict(id='landing', source_path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                 start_us=100000, duration_us=500000, source_start_us=0, anchor_offset_us=10000,
                 action_time_us=110000, visual_event_id='answer', action_operation_id='text',
                 reason='answer lands', gain_db=-12, fade_in_us=2000, fade_out_us=20000)
    spec = dict(id='mix', kind='mix_action_audio', authorization_source='user', dialogue_gain_db=-4, events=[event])
    plan = dict(project_state=str(state), audio_authorization_source='user', operations=[{'id': 'text', 'kind': 'add_preset_group'}, spec],
                project_format={'visual_planning_version': 1}, visual_events=[dict(id='answer',start_us=0,end_us=1000000,
                audience_need='answer',composition='person',primary_visual='person',techniques=[{'kind':'text_preset','operation_ids':['text']},{'kind':'sound','operation_ids':['mix']}])])
    draft = dict(duration=1000000,materials={'audios':[],'audio_fades':[],'speeds':[]},tracks=[dict(name='JY_ROUGH_CUT_VIDEO',type='video',segments=[dict(id='v',volume=1.0,target_timerange={'start':0,'duration':1000000},common_keyframes=[])])])
    return draft, spec, SimpleNamespace(plan=plan)


def test_native_audio_export_and_writer_validation(tmp_path):
    draft, spec, ctx = case(tmp_path)
    before = copy.deepcopy(draft)
    result, report = mix_action_audio(draft,spec,ctx)
    assert draft == before
    assert report['events_written'] == 1
    assert validate_visual_events(ctx.plan)['ok']
    assert validate_action_audio(ctx.plan,result)['ok']
    assert validate_timeline_equivalence(ctx.plan,result)['ok']
    refs = result['tracks'][-1]['segments'][0]['extra_material_refs']
    ids = {m['id'] for group in result['materials'].values() for m in group}
    assert set(refs) <= ids


def test_quiet_dialogue_can_be_raised_without_changing_sound_or_timing(tmp_path):
    draft, spec, ctx = case(tmp_path)
    spec['dialogue_gain_db'] = 8
    before = copy.deepcopy(draft)
    result, _ = mix_action_audio(draft, spec, ctx)
    voice = result['tracks'][0]['segments'][0]
    assert voice['volume'] == pytest.approx(10 ** (8 / 20))
    assert voice['target_timerange'] == before['tracks'][0]['segments'][0]['target_timerange']
    assert result['tracks'][-1]['segments'][0]['volume'] == pytest.approx(10 ** (-12 / 20))
    assert validate_action_audio(ctx.plan, result)['ok']
    voice['volume'] = 1.0
    assert not validate_action_audio(ctx.plan, result)['ok']
    assert draft == before


def test_base_silent_observation_stays_silent_through_gain_and_split(tmp_path):
    draft, spec, ctx = case(tmp_path)
    mute = draft['tracks'][0]['segments'][0]
    mute.update(volume=0.0, target_timerange={'start': 0, 'duration': 400000})
    voice = copy.deepcopy(mute)
    voice.update(id='voice', volume=1.0, target_timerange={'start': 400000, 'duration': 600000})
    draft['tracks'][0]['segments'] = [mute, voice]
    base = tmp_path / 'base.json'
    base.write_text(json.dumps(draft), encoding='utf-8')
    ctx.plan['base_draft'] = str(base)
    # Candidate splitting changes IDs, but must preserve the base mute range.
    mute['target_timerange']['duration'] = 200000
    second = copy.deepcopy(mute)
    second.update(id='split-mute', target_timerange={'start': 200000, 'duration': 200000})
    draft['tracks'][0]['segments'].insert(1, second)
    spec['dialogue_gain_db'] = 8
    result, _ = mix_action_audio(draft, spec, ctx)
    segments = result['tracks'][0]['segments']
    gain = 10 ** (8 / 20)
    assert [s['volume'] for s in segments] == pytest.approx([0, 0, gain])
    assert validate_action_audio(ctx.plan, result)['ok']
    segments[1]['volume'] = gain
    assert not validate_action_audio(ctx.plan, result)['ok']  # cannot reopen silence
    segments[1]['volume'] = 0
    segments[2]['volume'] = 0
    assert not validate_action_audio(ctx.plan, result)['ok']  # cannot silently lose speech


@pytest.mark.parametrize('volume', [.5, 2, -.1, False])
def test_existing_dialogue_gain_is_still_rejected(tmp_path, volume):
    draft, spec, ctx = case(tmp_path)
    draft['tracks'][0]['segments'][0]['volume'] = volume
    before = copy.deepcopy(draft)
    with pytest.raises(ValueError, match='existing dialogue gain/automation'):
        mix_action_audio(draft, spec, ctx)
    assert draft == before


def test_silent_base_does_not_allow_volume_automation(tmp_path):
    draft, spec, ctx = case(tmp_path)
    segment = draft['tracks'][0]['segments'][0]
    segment['volume'] = 0
    base = tmp_path / 'base.json'
    base.write_text(json.dumps(draft), encoding='utf-8')
    ctx.plan['base_draft'] = str(base)
    segment['common_keyframes'] = [{'property_type': 'KFTypeVolume', 'keyframe_list': []}]
    with pytest.raises(ValueError, match='existing dialogue gain/automation'):
        mix_action_audio(draft, spec, ctx)


@pytest.mark.parametrize('db', [12.1, float('inf'), True])
def test_invalid_positive_dialogue_gain_remains_rejected(tmp_path, db):
    draft, spec, ctx = case(tmp_path)
    spec['dialogue_gain_db'] = db
    before = copy.deepcopy(draft)
    with pytest.raises(ValueError):
        mix_action_audio(draft, spec, ctx)
    assert draft == before


@pytest.mark.parametrize('field,value',[('sha256','bad'),('anchor_offset_us',999999),('gain_db',float('nan')),('duration_us',2000000),('fade_out_us',900000),('action_operation_id','absent'),('source_start_us',900000)])
def test_invalid_sound_rejected_atomically(tmp_path,field,value):
    draft,spec,ctx=case(tmp_path)
    spec['events'][0][field]=value
    before=copy.deepcopy(draft)
    with pytest.raises((ValueError,KeyError)):
        mix_action_audio(draft,spec,ctx)
    assert draft==before


def test_reject_unauthorized_and_duplicate_mix(tmp_path):
    draft,spec,ctx=case(tmp_path)
    ctx.plan['audio_authorization_source']='different'
    with pytest.raises(ValueError):mix_action_audio(draft,spec,ctx)
    ctx.plan['audio_authorization_source']='user'
    result,_=mix_action_audio(draft,spec,ctx)
    with pytest.raises(ValueError):mix_action_audio(result,spec,ctx)


@pytest.mark.parametrize('field,value',[('volume',1.0),('speed',2.0),('reverse',True),('source_timerange',{'start':1,'duration':500000}),('target_timerange',{'start':0,'duration':500000})])
def test_writer_detects_sound_drift(tmp_path,field,value):
    draft,spec,ctx=case(tmp_path);result,_=mix_action_audio(draft,spec,ctx)
    result['tracks'][-1]['segments'][0][field]=value
    assert not validate_timeline_equivalence(ctx.plan,result)['ok']


@pytest.mark.parametrize('mute', [False, True])
def test_native_track_mute_is_checked_after_round_trip(tmp_path, mute):
    draft, spec, ctx = case(tmp_path)
    result, _ = mix_action_audio(draft, spec, ctx)
    from pyJianYingDraft.track import Track, TrackType
    original = result['tracks'][-1]
    native_track = Track(TrackType.audio, original['name'], 0, mute)
    result['tracks'][-1] = {**native_track.export_json(), 'segments': original['segments']}
    # Use the SDK's actual mute serialization, rather than an invented flag.
    assert validate_action_audio(ctx.plan, result)['ok'] is not mute
    assert validate_timeline_equivalence(ctx.plan, result)['ok'] is not mute


@pytest.mark.parametrize('target,field,value', [
    ('track', 'type', 'text'),
    ('segment', 'visible', False),
    ('segment', 'track_attribute', 1),
    ('material', 'type', 'photo'),
])
def test_writer_rejects_disabled_or_non_audio_sound(tmp_path, target, field, value):
    draft, spec, ctx = case(tmp_path)
    result, _ = mix_action_audio(draft, spec, ctx)
    assert validate_action_audio(ctx.plan, result)['ok']
    track = result['tracks'][-1]
    segment = track['segments'][0]
    material = next(m for m in result['materials']['audios'] if m['id'] == segment['material_id'])
    {'track': track, 'segment': segment, 'material': material}[target][field] = value
    assert not validate_action_audio(ctx.plan, result)['ok']
    assert not validate_timeline_equivalence(ctx.plan, result)['ok']


def test_writer_detects_missing_fade_and_changed_resource(tmp_path):
    draft,spec,ctx=case(tmp_path);result,_=mix_action_audio(draft,spec,ctx)
    result['materials']['audio_fades'].clear()
    assert not validate_action_audio(ctx.plan,result)['ok']


def test_writer_detects_changed_file_and_undeclared_audio(tmp_path):
    draft,spec,ctx=case(tmp_path);result,_=mix_action_audio(draft,spec,ctx)
    from pathlib import Path
    Path(spec['events'][0]['source_path']).write_bytes(b'changed')
    assert not validate_action_audio(ctx.plan,result)['ok']
    result['tracks'].append(dict(name='hidden',type='audio',segments=[{}]))
    assert 'unplanned additional audio in action mix' in validate_action_audio(ctx.plan,result)['errors']


def test_writer_summary_counts_actual_audio_not_legacy_name():
    import importlib.util
    from pathlib import Path
    path=Path(__file__).resolve().parents[1]/'scripts/write_candidate_plan.py'
    spec=importlib.util.spec_from_file_location('audio_writer_summary',path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.track_facts({'tracks':[{'name':'JY_SFX_landing','type':'audio','segments':[{},{}]},
                                        {'name':'JY_SFX_ACTION_BOUND_V2','type':'audio','segments':[{}]},
                                        {'name':'JY_SFX_fake','type':'text','segments':[{}]}]})['action_sfx_segments']==3

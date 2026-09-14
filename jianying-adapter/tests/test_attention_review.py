import copy
import json

from jianying_adapter.attention_review import review_attention, storyboard


def case():
    plan = {'creative_brief': {'hook': {'event_id': 'opening', 'strategy': 'pain_question', 'reason': '当前台词提出用户熟悉的困难'}},
            'visual_events': [{'id': 'opening', 'start_us': 0, 'end_us': 10_000_000,
                               'audience_need': '理解问题', 'composition': '人物为主', 'review_focus': '核对真实手部动作'}]}
    seg = {'id': 'v', 'material_id': 'video', 'target_timerange': {'start': 0, 'duration': 10_000_000}, 'clip': {}}
    draft = {'duration': 10_000_000, 'materials': {'videos': [{'id': 'video', 'path': '/fixture/video.mp4'}]},
             'tracks': [{'type': 'video', 'name': 'JY_ROUGH_CUT_VIDEO', 'segments': [seg]}]}
    return plan, draft


def test_caption_refresh_and_fake_split_do_not_hide_long_hold():
    plan, draft = case()
    original = draft['tracks'][0]['segments'][0]
    draft['tracks'][0]['segments'] = [{**copy.deepcopy(original), 'target_timerange': {'start': i*1_000_000, 'duration': 1_000_000}} for i in range(10)]
    draft['materials']['texts'] = [{'id': 'text', 'content': json.dumps({'text': '普通字幕'})}]
    draft['tracks'].append({'name': 'JY_ZH_SUBTITLES', 'type': 'text', 'segments': [
        {'material_id': 'text', 'target_timerange': {'start': i*1_000_000, 'duration': 1_000_000}} for i in range(10)]})
    report = review_attention(plan, draft)
    assert report['hook_declared']
    assert [(r['start_us'], r['end_us']) for r in report['long_holds']] == [(0, 10_000_000)]
    assert report['review_required'] and not report['creative_quality_verified']


def test_visible_new_special_text_closes_gap_but_hidden_text_does_not():
    plan, draft = case()
    draft['materials']['texts'] = [{'id': str(i), 'content': json.dumps({'text': str(i)})} for i in range(3)]
    track = {'name': 'JY_PRESET_1', 'type': 'text', 'segments': [
        {'material_id': str(i), 'target_timerange': {'start': (i+1)*3_000_000, 'duration': 1_000_000}} for i in range(3)]}
    draft['tracks'].append(track)
    assert not review_attention(plan, draft)['long_holds']
    track['visible'] = False
    assert review_attention(plan, draft)['long_holds']


def test_missing_or_late_hook_is_reported_and_storyboard_uses_existing_events():
    plan, _ = case()
    plan['visual_events'][0]['start_us'] = 4_000_000
    assert not review_attention(plan)['hook_declared']
    assert '理解问题' in storyboard(plan) and '4.00' in storyboard(plan)

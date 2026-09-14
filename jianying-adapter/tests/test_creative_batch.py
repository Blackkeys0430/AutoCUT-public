import copy
import json
from pathlib import Path

import pytest

from jianying_adapter.cli import main
from jianying_adapter.creative_batch import run_batch
from jianying_adapter.material_library import validate_plan_materials, register_material
from jianying_adapter.preset_registry import validate_preset_selections
from test_creative_pipeline import choice_case, decision, query_for, framed_handoff
from test_material_library import setup, write_json, usage_for


def test_failed_preset_does_not_stop_material_and_resume_skips_success(setup, capsys):
    folder, config, _, _ = setup
    plan, registry = choice_case(folder)
    media, handoff, usage, req = framed_handoff(setup)
    media['operations'], media['brolls'] = [], []
    event = media['visual_events'][0]
    event['techniques'] = []
    event['visual_requirements'][0]['operation_ids'] = []
    event['supporting_visual'].update(status='needs_asset', operation_ids=[])
    plan['visual_events'].append(event)
    source = write_json(folder/'source_plan.json', plan)
    reg = write_json(folder/'registry.json', registry)
    cfg = write_json(folder/'config.json', config)
    choices = [decision(query_for(plan, registry, e), e, t) for e, t in [('a', 'first'), ('b', 'second')]]
    items = [{'id': e, 'action': 'preset', 'registry': str(reg), 'decision': d,
              'operations': [{'id': 'preset_'+e, 'kind': 'add_preset_group', 'node_id': e, 'template_id': d['template_id']}]}
             for e, d in zip(['a', 'b'], choices)]
    items[0]['decision']['reviewed']['source_path'] += '.missing'
    items[1]['depends_on'] = ['a']
    items.append({'id': 'material', 'action': 'material', 'request': str(req), 'config': str(cfg), 'usage': usage})
    matrix = write_json(folder/'decisions.json', {'items': items})
    output = folder/'current.json'
    assert main(['creative-batch', str(source), str(matrix), '-o', str(output)]) == 1
    failed = json.loads(output.read_text('utf8'))
    assert failed['_batch']['items']['material']['status'] == 'done'
    assert failed['_batch']['items']['a']['status'] == failed['_batch']['items']['b']['status'] == 'failed'
    assert len(failed['operations']) == 1
    items[0]['decision']['reviewed']['source_path'] = choices[0]['reviewed']['source_path'].removesuffix('.missing')
    write_json(matrix, {'items': items})
    result = run_batch(source, matrix, output, resume=True)
    assert result['ok'] and result['attempted'] == 2
    complete = json.loads(output.read_text('utf8'))
    assert len(complete['operations']) == 3
    assert validate_plan_materials(complete, output)['ok']
    assert validate_preset_selections(complete, require_choices=True)['ok']
    assert json.loads(source.read_text('utf8')) == plan
    assert run_batch(source, matrix, output, resume=True)['attempted'] == 0
    capsys.readouterr()


def test_batch_runs_twenty_contextual_choices_with_one_cli_invocation(tmp_path, capsys):
    plan, registry = choice_case(tmp_path)
    template = plan['visual_events'][0]
    plan['visual_events'] = []
    for i in range(20):
        event = copy.deepcopy(template)
        eid = f'e{i:02}'
        event.update(id=eid, start_us=i*1_000_000, end_us=(i+1)*1_000_000)
        event['preset_request']['requirement_id'] = event['visual_requirements'][0]['id'] = 'text_'+eid
        plan['visual_events'].append(event)
    reg = write_json(tmp_path/'registry.json', registry)
    items = []
    for event in plan['visual_events']:
        eid = event['id']
        items.append({'id': eid, 'action': 'preset', 'registry': str(reg),
                      'decision': decision(query_for(plan, registry, eid), eid, 'first')})
    source = write_json(tmp_path/'plan.json', plan)
    matrix = write_json(tmp_path/'matrix.json', {'items': items})
    output = tmp_path/'current.json'
    assert main(['creative-batch', str(source), str(matrix), '-o', str(output)]) == 0
    current = json.loads(output.read_text('utf8'))
    last = current['visual_events'][-1]['preset_selections'][0]
    assert len(last['prior_events']) == 19
    assert all(row['template_ids'] == ['first'] for row in last['prior_events'])
    assert len(list(tmp_path.glob('current*'))) == 3  # plan, OS lock, query artifacts
    assert len(list((tmp_path/'current.queries').glob('*.json'))) == 20
    for row in current['_batch']['items'].values():
        assert 'query' not in row['result']
        query = json.loads((tmp_path/row['result']['query_ref']['path']).read_text('utf8'))
        assert row['result']['template_id'] in [item['template_id'] for item in query['top_k']]
        assert row['elapsed_seconds'] >= 0
    assert not list(tmp_path.glob('.batch-*'))
    items[0]['decision']['reason'] = 'modified completed decision'
    write_json(matrix, {'items': items})
    with pytest.raises(RuntimeError, match='已完成决策'):
        run_batch(source, matrix, output, resume=True)
    capsys.readouterr()


def test_batched_material_reaches_real_candidate_assembler(setup, capsys):
    from test_candidate_plan import make_plan
    folder, config, _, _ = setup
    source = make_plan(folder)
    plan = json.loads(source.read_text('utf8'))
    creative, _, usage, request = framed_handoff(setup)
    plan.update(creative)
    plan['project_format']['visual_planning_version'] = 2
    plan['operations'] = [{'id': 'captions', 'kind': 'reflow_ordinary_captions'}]
    plan['brolls'] = []
    event = plan['visual_events'][0]
    event['techniques'] = []
    event['visual_requirements'][0]['operation_ids'] = []
    event['supporting_visual'].update(status='needs_asset', operation_ids=[])
    state_path = Path(plan['project_state'])
    state = json.loads(state_path.read_text('utf8'))
    state.update(status='planning', visual_planning_min_version=2)
    write_json(state_path, state)
    base_path = Path(plan['base_draft'])
    base = json.loads(base_path.read_text('utf8'))
    font = Path('C:/Windows/Fonts/msyh.ttc')  # Installed font, read only.
    if not font.is_file(): pytest.skip('Windows font fixture unavailable')
    base['materials']['texts'][0]['content'] = json.dumps({'text': '完整语音', 'styles': [
        {'range': [0, 4], 'size': 18, 'font': {'path': str(font)}}]}, ensure_ascii=False)
    base['tracks'][0]['segments'][0]['clip'] = {'scale': {'x': 1, 'y': 1}, 'transform': {'x': 0, 'y': -.7}}
    write_json(base_path, base)
    write_json(source, plan)
    cfg = write_json(folder/'config.json', config)
    matrix = write_json(folder/'matrix.json', {'items': [
        {'id': 'detail', 'action': 'material', 'request': str(request), 'config': str(cfg), 'usage': usage}]})
    current_path = folder/'current.json'
    assert main(['creative-batch', str(source), str(matrix), '-o', str(current_path)]) == 0
    assert main(['candidate-preview', str(current_path)]) == 0
    current = json.loads(current_path.read_text('utf8'))
    candidate = json.loads(Path(plan['preview_output']).read_text('utf8'))
    assert validate_plan_materials(current, current_path, candidate)['ok']
    assert main(['creative-review', str(current_path), '--draft', plan['preview_output']]) == 0
    assert json.loads(Path(plan['preview_manifest']).read_text('utf8'))['writer_allowed'] is False
    assert json.loads(state_path.read_text('utf8')) == state
    assert json.loads(source.read_text('utf8')) == plan
    capsys.readouterr()


def test_interrupted_library_import_is_reused_and_tampered_current_plan_rejected(setup):
    folder, config, request, selection = setup
    # Simulate interruption after a real atomic import, before progress save.
    asset = register_material(request, selection, config)
    req = write_json(folder/'request.json', request)
    cfg = write_json(folder/'config.json', config)
    plan = {'visual_events': [{'id': 'choice', 'start_us': 0, 'end_us': 1_000_000, 'techniques': [],
                              'supporting_visual': {'status': 'needs_asset', 'purpose': request['purpose'],
                                                    'request_paths': [str(req)], 'operation_ids': []}}]}
    source = write_json(folder/'plan.json', plan)
    matrix = write_json(folder/'matrix.json', {'items': [{'id': 'media', 'action': 'material', 'request': str(req),
        'config': str(cfg), 'selection': selection, 'usage': usage_for(asset, selection)}]})
    output = folder/'current.json'
    assert run_batch(source, matrix, output)['ok']
    assert len(list(Path(config['library_root']).glob('*/ledger.json'))) == 1
    current = json.loads(output.read_text('utf8'))
    current['operations'][0]['clip']['scale']['x'] = 9
    write_json(output, current)
    with pytest.raises(ValueError, match='当前批次计划已变化'):
        run_batch(source, matrix, output, resume=True)


def _single_preset_batch(folder):
    plan, registry = choice_case(folder)
    plan['visual_events'] = plan['visual_events'][:1]
    eid = plan['visual_events'][0]['id']
    reg = write_json(folder/'registry.json', registry)
    source = write_json(folder/'plan.json', plan)
    matrix = write_json(folder/'matrix.json', {'items': [{
        'id': eid, 'action': 'preset', 'registry': str(reg),
        'decision': decision(query_for(plan, registry, eid), eid, 'first')}]})
    return source, matrix, folder/'current.json', eid


def test_preset_batch_with_layout_operations_passes_visual_handoff_validation(tmp_path):
    from jianying_adapter.visual_planning import validate_visual_events

    source, matrix, output, eid = _single_preset_batch(tmp_path)
    plan = json.loads(source.read_text('utf8'))
    plan['project_format'] = {'visual_planning_version': 1}
    plan['visual_events'][0]['primary_visual'] = 'text'
    write_json(source, plan)
    decisions = json.loads(matrix.read_text('utf8'))
    operations = [
        {'id': 'import', 'kind': 'add_preset_group', 'node_id': eid, 'template_id': 'first'},
        {'id': 'place', 'kind': 'place_preset_group', 'node_id': eid, 'template_id': 'first'},
        {'id': 'retime', 'kind': 'retime_preset_text_tracks', 'node_id': eid, 'template_id': 'first'},
    ]
    decisions['items'][0]['operations'] = operations
    write_json(matrix, decisions)
    assert run_batch(source, matrix, output)['ok']
    current = json.loads(output.read_text('utf8'))
    report = validate_visual_events(current)
    assert report['ok'], report['errors']
    assert current['operations'] == operations


@pytest.mark.parametrize('damage', ['changed', 'missing'])
def test_resume_checks_the_saved_query_instead_of_silently_reselecting(tmp_path, damage):
    source, matrix, output, eid = _single_preset_batch(tmp_path)
    first = run_batch(source, matrix, output)
    assert first['ok'] and first['attempted'] == 1 and first['reused'] == 0
    current = json.loads(output.read_text('utf8'))
    record = current['_batch']['items'][eid]['result']
    query_path = tmp_path/record['query_ref']['path']
    before = query_path.read_bytes()
    assert run_batch(source, matrix, output, resume=True)['reused'] == 1
    assert query_path.read_bytes() == before
    if damage == 'missing':
        query_path.unlink()
    else:
        query = json.loads(before)
        query['request']['intent'] = 'a different request'
        write_json(query_path, query)
    with pytest.raises(ValueError, match='已完成项输入无法复核'):
        run_batch(source, matrix, output, resume=True)
    assert json.loads(output.read_text('utf8')) == current


def test_legacy_inline_query_progress_can_still_resume(tmp_path):
    source, matrix, output, eid = _single_preset_batch(tmp_path)
    assert run_batch(source, matrix, output)['ok']
    current = json.loads(output.read_text('utf8'))
    result = current['_batch']['items'][eid]['result']
    result['query'] = json.loads((tmp_path/result.pop('query_ref')['path']).read_text('utf8'))
    write_json(output, current)
    assert run_batch(source, matrix, output, resume=True)['attempted'] == 0


def test_failed_query_save_does_not_commit_a_partial_choice(tmp_path, monkeypatch):
    import jianying_adapter.creative_batch as batch
    source, matrix, output, _ = _single_preset_batch(tmp_path)
    store = batch._store_query
    def fail(*args):
        raise OSError('simulated query save failure')
    monkeypatch.setattr(batch, '_store_query', fail)
    assert not run_batch(source, matrix, output)['ok']
    assert not json.loads(output.read_text('utf8'))['visual_events'][0].get('preset_selections')
    monkeypatch.setattr(batch, '_store_query', store)
    assert run_batch(source, matrix, output, resume=True)['ok']

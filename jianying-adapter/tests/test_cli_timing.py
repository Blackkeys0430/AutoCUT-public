import json

from jianying_adapter.cli import main


def test_command_timings_append_without_changing_json_output(tmp_path, capsys):
    plan = tmp_path/'plan.json'
    plan.write_text(json.dumps({'visual_events': [], 'operations': []}), encoding='utf8')
    log = tmp_path/'timings.jsonl'
    assert main(['--timing-log', str(log), 'creative-review', str(plan)]) == 0
    assert isinstance(json.loads(capsys.readouterr().out), dict)
    assert main(['--timing-log', str(log), 'creative-review', str(tmp_path/'missing.json')]) == 2
    assert json.loads(capsys.readouterr().out)['ok'] is False
    rows = [json.loads(line) for line in log.read_text('utf8').splitlines()]
    assert [row['exit_code'] for row in rows] == [0, 2]
    assert all(row['command'] == 'creative-review' and row['elapsed_seconds'] >= 0 for row in rows)


def test_timing_output_cannot_append_to_an_input_plan(tmp_path, capsys):
    plan = tmp_path/'plan.json'
    original = '{"visual_events": []}'
    plan.write_text(original, encoding='utf8')
    assert main(['--timing-log', str(plan), 'creative-review', str(plan)]) == 2
    assert plan.read_text('utf8') == original
    capsys.readouterr()

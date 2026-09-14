"""Batch existing creative handoffs into one resumable workspace CandidatePlan."""
import copy
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import tempfile
from time import perf_counter

from .material_library import (DEFAULT_CONFIG, _storage_root, attach_material_handoff,
                               material_handoff, register_material, search_materials)
from .preset_registry import attach_preset_selection, preset_request_from_plan, select_template
from .visual_planning import TECHNIQUE_OPERATIONS


def _read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode('utf8')).hexdigest()


def _save_json(path, value):
    fd, temporary = tempfile.mkstemp(prefix='.batch-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf8') as out:
            json.dump(value, out, ensure_ascii=False, indent=2)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _save(path, plan):
    plan['_batch']['plan_digest'] = _digest({k: v for k, v in plan.items() if k != '_batch'})
    _save_json(path, plan)


def _store_query(output, result, signature):
    """Keep the original query readable without copying it into every plan."""
    if 'query' not in result:
        return result
    query = result['query']
    folder = output.with_name(output.stem + '.queries')
    folder.mkdir(exist_ok=True)
    path = folder / (signature + '.json')
    _save_json(path, query)
    return {**{k: v for k, v in result.items() if k != 'query'},
            'query_ref': {'path': path.relative_to(output.parent).as_posix(), 'digest': _digest(query)}}


def _verify_query(output, result):
    reference = result.get('query_ref')
    if reference is None:  # Existing inline-query progress remains resumable.
        return
    path = (output.parent / reference['path']).resolve()
    folder = output.with_name(output.stem + '.queries').resolve()
    if not path.is_relative_to(folder) or _digest(_read(path)) != reference.get('digest'):
        raise ValueError('已完成项的查询记录已变化，不能复用旧进度')


def _register_or_reuse(request, selection, config):
    """A crash after atomic library import must not duplicate registration."""
    asset_id = selection.get('asset_id', '')
    import re
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,99}', asset_id):
        raise ValueError('invalid asset_id')
    folder = _storage_root(config) / asset_id
    if not folder.exists():
        return register_material(request, selection, config)
    asset = _read(folder/'ledger.json')['assets'][0]
    source = Path(selection['local_path']).resolve()
    if (_read(folder/'request.json') != request or Path(asset['original_path']).resolve() != source
            or hashlib.sha256(source.read_bytes()).hexdigest() != asset['sha256']
            or hashlib.sha256(Path(asset['local_path']).read_bytes()).hexdigest() != asset['sha256']):
        raise ValueError('已有素材ID与本次入库输入不一致；不能作为失败重试复用')
    for key, value in selection.items():
        if key == 'local_path':
            continue
        actual = asset.get(key)
        if key == 'visual_review':
            if any(actual.get(k) != v for k, v in value.items()):
                raise ValueError('已有素材查看记录不同')
        elif actual != value:
            raise ValueError('已有素材来源或授权字段不同: ' + key)
    return {'asset_id': asset_id, 'local_path': asset['local_path'], 'ledger_path': str(folder/'ledger.json')}


def _merge(items, additions, key):
    for addition in additions:
        identifier = addition[key]
        existing = [row for row in items if row.get(key) == identifier]
        if existing and existing != [addition]:
            raise ValueError('交接与已有内容冲突: ' + str(identifier))
        if not existing:
            items.append(copy.deepcopy(addition))


def _execute(plan, output, item, files):
    action = item['action']
    if action in {'search', 'material'}:
        request_path, request = files['request']
        config = files['config'][1]
        if action == 'search':
            return plan, search_materials(request, config)
        # Check the destination before the library import has side effects.
        if not any(e.get('id') == request['visual_event_id'] for e in plan.get('visual_events', [])):
            raise ValueError('素材需求未连接当前视觉事件')
        usage = copy.deepcopy(item['usage'])
        if item.get('selection'):
            registered = _register_or_reuse(request, item['selection'], config)
            if usage.get('asset_id') not in (None, registered['asset_id']):
                raise ValueError('入库与使用的asset_id不同')
            usage['asset_id'] = registered['asset_id']
        # material_handoff already computes framing and verifies the reviewed
        # crop/clip. No synthetic visual review or separate frame file is made.
        handoff = material_handoff(request_path, usage, config)
        return attach_material_handoff(plan, output, handoff), {'asset_id': usage['asset_id'], 'operation_id': handoff['operation']['id']}
    if action == 'preset':
        decision = item['decision']
        request = preset_request_from_plan(plan, decision['event_id'])
        query = select_template(files['registry'][1], request)
        result = attach_preset_selection(plan, query, decision)
        operations = item.get('operations', [])
        presets = item.get('presets', [])
        _merge(result.setdefault('operations', []), operations, 'id')
        _merge(result.setdefault('presets', []), presets, 'node_id')
        if operations:
            imports = [o for o in operations if o.get('kind') in {'add_preset_group', 'replace_preset_group'}]
            if not imports or any((o.get('node_id'), o.get('template_id') or o.get('new_template_id')) !=
                                  (decision['node_id'], decision['template_id']) for o in imports):
                raise ValueError('实际预设操作与本次选择不一致')
            event = next(e for e in result['visual_events'] if e['id'] == decision['event_id'])
            need = next(n for n in event['visual_requirements'] if n['id'] == event['preset_request']['requirement_id'])
            technique = next((t for t in event.setdefault('techniques', []) if t['kind'] == 'text_preset'), None)
            if technique is None:
                technique = {'kind': 'text_preset', 'operation_ids': []}
                event['techniques'].append(technique)
            for op in operations:
                if (op.get('kind') in TECHNIQUE_OPERATIONS['text_preset']
                        and op['id'] not in technique['operation_ids']):
                    technique['operation_ids'].append(op['id'])
            for op in imports:
                if op['id'] not in need.setdefault('operation_ids', []):
                    need['operation_ids'].append(op['id'])
        return result, {'selection_saved': True, 'operation_count': len(operations),
                        'template_id': decision['template_id'], 'reason': decision['reason'], 'query': query}
    raise ValueError('未知batch action: ' + str(action))


def _run_batch(source_path, matrix_path, output, *, resume=False):
    started = perf_counter()
    source_path, matrix_path, output = (Path(p).resolve() for p in (source_path, matrix_path, output))
    if output == source_path or output.parent != source_path.parent or PureWindowsPath(str(output)).drive.casefold() == 'c:':
        raise ValueError('批次输出须为原计划同目录的独立非C文件')
    source = _read(source_path)
    items = _read(matrix_path)['items']
    identifiers = [row.get('id') for row in items]
    if any(not isinstance(i, str) or not i.strip() for i in identifiers) or len(set(identifiers)) != len(identifiers):
        raise ValueError('batch item id须唯一且非空')
    if resume:
        plan = _read(output)
        progress = plan['_batch']
        if (progress['source_path'] != str(source_path) or progress['source_digest'] != _digest(source)
                or progress['plan_digest'] != _digest({k: v for k, v in plan.items() if k != '_batch'})):
            raise ValueError('原计划或当前批次计划已变化；拒绝从旧进度继续')
    else:
        if output.exists():
            raise FileExistsError('批次输出已存在；续跑须显式--resume')
        plan = copy.deepcopy(source)
        plan['_batch'] = {'source_path': str(source_path), 'source_digest': _digest(source), 'items': {}}
    progress = plan['_batch']
    if any(i not in identifiers for i in progress['items']):
        raise ValueError('续跑不能静默删除批次项；变更工作范围请从当前计划另起批次')
    resolved, failures = {}, {}
    for item in items:
        try:
            dependencies = item.get('depends_on', [])
            if not isinstance(dependencies, list) or any(not isinstance(dep, str) for dep in dependencies):
                raise ValueError('depends_on须为item id列表')
            files = {}
            for key in ('request', 'config', 'registry'):
                value = item.get(key)
                if key == 'config' and item['action'] in {'search', 'material'}:
                    value = value or str(DEFAULT_CONFIG)
                if key == 'registry' and item['action'] == 'preset':
                    value = value or str(Path(__file__).resolve().parents[2]/'preset_catalog/preset_usage_registry_v1.json')
                if value:
                    path = Path(value)
                    if not path.is_absolute():
                        path = matrix_path.parent / path
                    files[key] = (path.resolve(), _read(path))
            signature = _digest({'item': item, 'files': {k: [str(p), v] for k, (p, v) in files.items()}})
            previous = progress['items'].get(item['id'], {})
            if previous.get('status') == 'done' and previous.get('input_digest') != signature:
                raise RuntimeError('已完成决策或其输入文件已变化: ' + item['id'])
            if previous.get('status') == 'done':
                _verify_query(output, previous.get('result', {}))
            resolved[item['id']] = (files, signature)
        except RuntimeError:
            raise
        except Exception as exc:
            if progress['items'].get(item['id'], {}).get('status') == 'done':
                raise ValueError('已完成项输入无法复核: ' + item['id']) from exc
            failures[item['id']] = str(exc)
    pending = {i for i in identifiers if progress['items'].get(i, {}).get('status') != 'done'}
    reused = len(identifiers) - len(pending)
    attempted = 0
    while pending:
        advanced = False
        for item in items:
            iid = item['id']
            if iid not in pending:
                continue
            dependencies = item.get('depends_on', []) if iid not in failures else []
            if any(dep in pending for dep in dependencies):
                continue
            item_started = perf_counter()
            try:
                if iid in failures:
                    raise ValueError(failures[iid])
                if any(progress['items'].get(dep, {}).get('status') != 'done' for dep in dependencies):
                    raise ValueError('依赖尚未完成: ' + ', '.join(dependencies))
                files, signature = resolved[iid]
                attempted += 1
                updated, result = _execute(plan, output, item, files)
                result = _store_query(output, result, signature)
                plan = updated
                progress = plan['_batch']
                progress['items'][iid] = {'status': 'done', 'input_digest': signature, 'result': result}
            except Exception as exc:
                progress['items'][iid] = {'status': 'failed', 'error': str(exc)}
            progress['items'][iid]['elapsed_seconds'] = round(perf_counter() - item_started, 6)
            pending.remove(iid)
            advanced = True
            _save(output, plan)
        if not advanced:
            for iid in pending:
                progress['items'][iid] = {'status': 'failed', 'error': '依赖存在循环'}
            pending.clear()
            _save(output, plan)
    _save(output, plan)
    rows = {i: {k: v for k, v in r.items() if k in {'status', 'error'}} for i, r in progress['items'].items()}
    return {'ok': all(r['status'] == 'done' for r in rows.values()), 'plan_output': str(output),
            'attempted': attempted, 'reused': reused, 'elapsed_seconds': round(perf_counter() - started, 6),
            'items': rows, 'writer_allowed': False}


def run_batch(source_path, matrix_path, output, *, resume=False):
    """One writer per current plan; OS releases the lock after interruption."""
    output, source_path = Path(output).resolve(), Path(source_path).resolve()
    if output == source_path or output.parent != source_path.parent or PureWindowsPath(str(output)).drive.casefold() == 'c:':
        raise ValueError('批次输出须为原计划同目录的独立非C文件')
    with output.with_suffix(output.suffix + '.lock').open('a+b') as lock:
        if not lock.tell():
            lock.write(b'0')
            lock.flush()
        lock.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _run_batch(source_path, matrix_path, output, resume=resume)

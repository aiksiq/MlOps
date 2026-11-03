"""Исполняемый прототип для расследования. Только синтетические данные, без модели.

Запускайте в отдельном каталоге .lab. Этот последовательный прототип надо
превратить в корректный Prefect-flow; его зелёный статус не доказывает корректность.
"""
import argparse
import hashlib
import json
from pathlib import Path
import release


def canonical(value):
    return release.canonical_json(value)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write(path,value):
    payload = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    release.atomic_write_bytes(path, payload)


def initialize(root):
    if root.exists():
        raise FileExistsError('Рабочий каталог уже существует. Выберите новый --workspace.')
    root.mkdir(parents=True)
    runtime={'base_revision':'fixture-only','tokenizer_sha256':'a'*64,
             'template_sha256':'b'*64,'generation':{'max_new_tokens':32,'do_sample':False},'dtype':'float32'}
    for version in ['A','B']:
        artifact=root/f'model-{version}.txt';artifact.write_text(f'STUB ONLY — version {version}\n', encoding='utf-8')
        files={artifact.name:digest(artifact.read_bytes())}
        m={'schema_version':1,'model_id':f'fixture-{version}',
           'artifact_uri':artifact.name,'model_sha256':digest(canonical(files)),
           'protocol_sha256':'d'*64,'golden_sha256':'e'*64,
           'runtime_sha256':digest(canonical(runtime)),'runtime':runtime,'file_hashes':files}
        report={k:m[k] for k in ['model_id','model_sha256','protocol_sha256','golden_sha256','runtime_sha256']}
        report.update(status='complete',passed=True,evaluation_run_id=f'fixture-eval-{version}',metrics={'accuracy':.8,'format_rate':1.0},synthetic=True)
        write(root/f'manifest-{version}.json',m);write(root/f'report-{version}.json',report)
    write(root/'accepted.json',read(root/'manifest-A.json'))
    report=read(root/'report-B.json');report['status']='running';report['passed']='false'
    write(root/'report-incomplete.json',report)
    report=read(root/'report-B.json');report['passed']=False
    write(root/'report-reject.json',report)
    write(root/'config.json',{'tokenizer_revision':'r1','template_sha256':'b'*64,'max_length':128,'code_revision':'prepare-v1'})
    (root/'input.txt').write_text('Первый пример\nВторой пример\n', encoding='utf-8')
    print('Создан синтетический стенд:',root)


def prepare(root):
    config=read(root/'config.json');key=release.cache_key(root/'input.txt',config)
    cache=root/'cache';cache.mkdir(exist_ok=True);entry=cache/(key+'.json')
    if entry.exists():
        result=read(entry)
        expected = digest((root/'input.txt').read_bytes())
        if result.get('input_sha256') != expected or result.get('config') != config:
            raise ValueError(f'Corrupt preparation cache entry: {entry}')
        print('CACHE_HIT',result['input_sha256']);return result
    payload=(root/'input.txt').read_bytes()
    result={'input_sha256':digest(payload),'rows':len(payload.splitlines()),'config':config}
    write(entry,result);print('PREPARED',result['input_sha256']);return result


def _publication_id(manifest, report):
    return digest(canonical({'manifest': manifest, 'report': report}))


def _record_publication(root, manifest, report, prepared=None):
    if read(root/'accepted.json') != manifest:
        raise ValueError('Publication journal can only follow the matching accepted pointer')
    journal = root/'publications.jsonl'
    publication_id = _publication_id(manifest, report)
    events = []
    if journal.exists():
        for line_number, line in enumerate(journal.read_text(encoding='utf-8').splitlines(), start=1):
            if not line.strip():
                raise ValueError(f'Empty publication journal row {line_number}')
            event = json.loads(line)
            if not isinstance(event, dict) or not isinstance(event.get('publication_id'), str):
                raise ValueError(f'Invalid publication journal row {line_number}')
            events.append(event)
    existing_index = next(
        (index for index, event in enumerate(events) if event['publication_id'] == publication_id),
        None,
    )
    if events and events[-1]['publication_id'] == publication_id:
        return False
    event = {
        'publication_id': publication_id,
        'model_id': manifest['model_id'],
        'model_sha256': manifest['model_sha256'],
        'protocol_sha256': manifest['protocol_sha256'],
        'golden_sha256': manifest['golden_sha256'],
        'runtime_sha256': manifest['runtime_sha256'],
        'evaluation_run_id': report['evaluation_run_id'],
    }
    if prepared is not None:
        event['prepared_input_sha256'] = prepared['input_sha256']
    if existing_index is not None:
        event = events.pop(existing_index)
    events.append(event)
    payload = ''.join(json.dumps(event, ensure_ascii=False, sort_keys=True) + '\n' for event in events)
    release.atomic_write_bytes(journal, payload.encode('utf-8'))
    return True


def publish_release(root, report_name, prepared):
    manifest=read(root/'manifest-B.json');report=read(root/report_name)
    release.publish(manifest,report,root/'accepted.json')
    created = _record_publication(root, manifest, report, prepared)
    print(('PUBLISHED' if created else 'ALREADY_PUBLISHED'),manifest['model_id'])
    return created


def run(root,report_name,fail_after_prepare=False):
    prepared=prepare(root)
    if fail_after_prepare:raise RuntimeError('Инъекция сбоя после подготовки')
    return publish_release(root, report_name, prepared)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--workspace',type=Path,default=Path('.lab'))
    sub=parser.add_subparsers(dest='command',required=True);sub.add_parser('init')
    run_parser=sub.add_parser('run');run_parser.add_argument('--report',default='report-B.json');run_parser.add_argument('--fail-after-prepare',action='store_true')
    args=parser.parse_args()
    if args.command=='init':initialize(args.workspace)
    else:run(args.workspace,args.report,args.fail_after_prepare)


if __name__=='__main__':main()

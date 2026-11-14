"""분리본 해시·정의·경로 검증. --restore-to는 별도 신규 폴더에 Sigma 기준 소스를 복원한다."""
import argparse
import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote


def digest(data):
    return hashlib.sha256(data).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-repo', type=Path, help='기준·추출 커밋 Git 객체가 있는 로컬 SION 저장소')
    parser.add_argument('--restore-to', type=Path, help='아직 존재하지 않는 별도 복원 폴더')
    parser.add_argument('--sigma-dir', type=Path, help='운영자가 준비한 Sigma 사본의 기준 해시·패치 적용 가능성을 읽기 전용 점검')
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    manifest = json.loads((root / '분리내역.json').read_text())
    errors = []
    def require(condition, label):
        if not condition:
            errors.append(label)
    def local(path):
        p = (root / path).resolve()
        if not p.is_relative_to(root):
            raise ValueError('분리본 밖 경로: ' + path)
        return p
    for key, field in [('copied_files', 'sha256'), ('integration_patches', 'patch_sha256')]:
        for item in manifest[key]:
            p = local(item['path'])
            require(p.is_file() and digest(p.read_bytes()) == item[field], item['path'])
            if p.is_file() and key == 'copied_files':
                require(p.stat().st_size == item['bytes'], '파일 크기 불일치: ' + item['path'])
                mode = '100755' if p.stat().st_mode & 0o100 else '100644'
                require(mode == item['mode'], '파일 권한 불일치: ' + item['path'])
    authored = {'README.md', '적용안내.md', '업데이트내역.md', '객체구조_전체목록.md',
                '05_설계검토문서/시멘틱실행계약.md'}
    metadata = authored | {'LICENSE', '.gitignore', '분리내역.json', '분리본검증.py',
                           '구조보기.html', '03_기존엔진_연결변경/기존엔진_의존목록.json'}
    expected = metadata | {x['path'] for k in ('copied_files', 'integration_patches') for x in manifest[k]}
    actual = {p.relative_to(root).as_posix() for p in root.rglob('*')
              if p.is_file() and not {'.git', '__pycache__', '.pytest_cache'}.intersection(p.relative_to(root).parts)
              and p.name != '.DS_Store'}
    require(expected == actual, '공개 파일 목록 불일치: ' + str(sorted(expected ^ actual)))
    for name in authored:
        path = local(name)
        for link in re.findall(r'\[[^\]]*\]\(([^)]+)\)', path.read_text()):
            if not link.startswith(('http:', 'https:', '#')):
                target = (path.parent / unquote(link.split('#', 1)[0])).resolve()
                require(target.is_relative_to(root) and target.exists(), '안내 링크 누락: ' + name + ': ' + link)
    syntax_count = 0
    for item in manifest['copied_files']:
        p = local(item['path'])
        if p.suffix == '.py' and p.exists():
            try:
                ast.parse(p.read_text(), filename=str(p))
                syntax_count += 1
            except SyntaxError as exc:
                errors.append(str(exc))
    ir = json.loads((root / '01_모델정의/sigma-six.ir.json').read_text())
    bindings = json.loads((root / '01_모델정의/bindings.json').read_text())
    html = (root / '구조보기.html').read_text()
    data = json.loads(re.search(r'<script type="application/json" id="model-source">(.*?)</script>', html, re.S)[1])
    require(data['ontology'] == ir['ontology'], 'HTML 모델 불일치')
    require(data['bindings'] == bindings, 'HTML 매핑 불일치')
    semantics = json.loads((root / '01_모델정의/semantic_definitions.json').read_text())
    require(data.get('semanticDefinitions') == semantics, 'HTML 공통 의미 정의 불일치')
    concepts = semantics['concepts']
    require(semantics['format'] == 'SIGMA_SEMANTIC_DEFINITIONS@v1'
            and len({c['id'] for c in concepts}) == len(concepts), '공통 의미 정의 형식/중복 ID')
    for concept in concepts:
        require(all(concept.get(k) for k in ('id', 'fields', 'meaning', 'producer', 'consumer', 'validation')),
                '공통 의미 정의 누락: ' + str(concept.get('id')))
    require(data['files'] == manifest['copied_files'], 'HTML 파일 목록 불일치')
    require(data['patches'] == manifest['integration_patches'], 'HTML 패치 목록 불일치')
    deps = json.loads((root / '03_기존엔진_연결변경/기존엔진_의존목록.json').read_text())
    require(data['dependencies'] == deps['modules'], 'HTML 의존 목록 불일치')
    for path in re.findall(r'href="([^"]+)"', html):
        if not path.startswith(('http:', 'https:', '#')):
            require(local(path).exists(), 'HTML 링크 누락: ' + path)
    adapter = root / '02_신규기능코드/agent/src/ontology_adapter.py'
    # Only import the hash-verified, dependency-free model reader.
    if not errors:
        sys.dont_write_bytecode = True
        spec = importlib.util.spec_from_file_location('extract_model_reader', adapter)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.load_definition_bundle(root / '01_모델정의')
    if args.restore_to and not args.source_repo:
        parser.error('--restore-to에는 --source-repo가 필요합니다')
    if errors:
        raise ValueError('\n'.join(errors))
    sigma_checked = False
    if args.sigma_dir:
        sigma = args.sigma_dir.resolve(strict=True)
        if not sigma.is_dir():
            raise ValueError('Sigma 사본 폴더가 필요합니다')
        for item in manifest['integration_patches']:
            p = (sigma / item['source']).resolve()
            require(p.is_relative_to(sigma) and p.is_file()
                    and digest(p.read_bytes()) == item['baseline_sha256'],
                    'Sigma 기준 불일치: ' + item['source'])
            if p.is_file() and item.get('baseline_mode'):
                mode = '100755' if p.stat().st_mode & 0o100 else '100644'
                require(mode == item['baseline_mode'], 'Sigma 파일 권한 불일치: ' + item['source'])
        for item in manifest['copied_files']:
            p = (sigma / item['source']).resolve()
            require(p.is_relative_to(sigma), 'Sigma 밖 배치 경로: ' + item['source'])
            if p.exists():
                require(p.is_file() and digest(p.read_bytes()) == item['sha256'],
                        '추가 파일 충돌: ' + item['source'])
        if errors:
            raise ValueError('\n'.join(errors))
        subprocess.run(['git', 'apply', '--check', *[str(local(x['path']))
                       for x in manifest['integration_patches']]], cwd=sigma, check=True)
        sigma_checked = True
    source_verified = 0
    if args.source_repo:
        def git(*params):
            return subprocess.check_output(['git', '-C', str(args.source_repo), *params])
        commit = manifest['source_commit']
        base = manifest['baseline_commit']
        def tree(ref):
            entries = {}
            for raw in git('ls-tree', '-rz', ref).split(b'\0'):
                if raw:
                    meta, name = raw.split(b'\t', 1)
                    mode, kind, oid = meta.decode().split()
                    entries[name.decode()] = {'mode': mode, 'kind': kind, 'oid': oid}
            return entries
        target_tree, base_tree = tree(commit), tree(base)
        for item in manifest['copied_files']:
            require(target_tree[item['source']]['mode'] == item['mode'], '원본 권한 불일치: ' + item['source'])
            require(digest(git('show', commit + ':' + item['source'])) == item['sha256'], '원본 불일치: ' + item['source'])
            source_verified += 1
        for item in manifest['integration_patches']:
            require(digest(git('show', commit + ':' + item['source'])) == item['current_sha256'], '변경 후 원본 불일치: ' + item['source'])
            require(digest(git('show', base + ':' + item['source'])) == item['baseline_sha256'], '기준 원본 불일치: ' + item['source'])
            source_verified += 1
            require(target_tree[item['source']]['mode'] == item['current_mode'], '변경 후 원본 권한 불일치: ' + item['source'])
            require(base_tree[item['source']]['mode'] == item['baseline_mode'], '기준 원본 권한 불일치: ' + item['source'])
        for item in manifest['excluded']:
            entry = target_tree[item['source']]
            if entry['kind'] == 'commit':
                require(all(item.get(k) == entry[k] for k in ('mode', 'kind', 'oid')),
                        '제외 서브모듈 참조 불일치: ' + item['source'])
            else:
                require(digest(git('show', commit + ':' + item['source'])) == item['sha256'],
                        '제외 원본 해시 불일치: ' + item['source'])
        raw = git('diff', '--name-only', '-z', base, commit).decode().split('\0')
        covered = {x['source'] for x in manifest['copied_files'] + manifest['integration_patches'] + manifest['excluded']}
        require(set(filter(None, raw)) == covered, '기준 대비 변경 파일 누락 또는 과다 포함')
        if errors:
            raise ValueError('\n'.join(errors))
        if args.restore_to:
            destination = args.restore_to.resolve()
            if destination.exists() or destination.is_relative_to(root) or destination.is_relative_to(args.source_repo.resolve()):
                raise ValueError('복원 대상은 분리본·원본 외부의 존재하지 않는 폴더여야 합니다')
            destination.mkdir(parents=True)
            for name in filter(None, git('ls-tree', '-rz', '--name-only', base).decode().split('\0')):
                p = destination / name
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(git('show', base + ':' + name))
                p.chmod(0o755 if base_tree[name]['mode'] == '100755' else 0o644)
            for item in manifest['integration_patches']:
                patch = str(local(item['path']))
                subprocess.run(['git', 'apply', '--check', patch], cwd=destination, check=True)
                subprocess.run(['git', 'apply', patch], cwd=destination, check=True)
                p = destination / item['source']
                require(digest(p.read_bytes()) == item['current_sha256'], '패치 복원 불일치: ' + item['source'])
                require(('100755' if p.stat().st_mode & 0o100 else '100644') == item['current_mode'],
                        '패치 복원 권한 불일치: ' + item['source'])
            for item in manifest['copied_files']:
                p = destination / item['source']
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(local(item['path']).read_bytes())
                if item.get('mode'):
                    p.chmod(0o755 if item['mode'] == '100755' else 0o644)
    if errors:
        raise ValueError('\n'.join(errors))
    print(json.dumps({'status': 'passed', 'copied_files': len(manifest['copied_files']), 'patches': len(manifest['integration_patches']), 'python_syntax_files': syntax_count, 'source_paths_verified': source_verified, 'excluded_paths_verified': len(manifest['excluded']) if args.source_repo else 0, 'sigma_base_check': sigma_checked, 'restored_to': str(args.restore_to) if args.restore_to else None}, ensure_ascii=False))


if __name__ == '__main__':
    main()

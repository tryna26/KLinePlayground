"""Upload checked Windows artifacts to an existing release draft from CI."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import zipfile

import requests


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    root = Path(os.environ['GITHUB_WORKSPACE']).resolve()
    repo = os.environ['GITHUB_REPOSITORY']
    assert repo == 'tryna26/KLinePlayground'
    tag = os.environ['RELEASE_TAG']
    assert re.fullmatch(r'v\d+\.\d+\.\d+', tag)
    version = tag[1:]
    release_id = int(os.environ['RELEASE_ID'])
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
    assert subprocess.check_output(['git', 'rev-parse', tag+'^{}'], cwd=root, text=True).strip() == commit
    output = root / 'dist/release'
    expected = {f'KLinePlayground-{version}-windows-x64-{suffix}' for suffix in ('setup.exe','portable.zip')} | {'bundle-manifest.json','SHA256SUMS.txt'}
    assert {path.name for path in output.iterdir() if path.is_file()} == expected
    manifest = json.loads((output/'bundle-manifest.json').read_text(encoding='utf-8'))
    assert manifest['version'] == version
    for line in (output/'SHA256SUMS.txt').read_text(encoding='utf-8').splitlines():
        checksum, name = line.split('  ', 1)
        assert name in expected and digest(output/name) == checksum
    with zipfile.ZipFile(output/f'KLinePlayground-{version}-windows-x64-portable.zip') as archive:
        assert set(archive.namelist()) == {'KLinePlayground/bin/'+row['path'] for row in manifest['files']}
    for row in manifest['files']:
        relative = Path(row['path'])
        assert not relative.is_absolute() and '..' not in relative.parts
        assert not any(part.lower() in {'users','.runtime','.git','.venv','portfolio_reviewed','portfolio_replay'} for part in relative.parts)
        assert relative.suffix.lower() not in {'.db','.sqlite','.sqlite3','.log','.enc'}
        for folder in (root/'dist/KLinePlayground', root/'.runtime/ci-portable/KLinePlayground/bin'):
            path = folder/relative
            assert path.stat().st_size == row['size'] and digest(path) == row['sha256']
    for kind in ('installed','portable'):
        result = json.loads((root/f'.runtime/ci-{kind}-features/result.json').read_text(encoding='utf-8'))
        assert result['status'] == 'passed' and result['version'] == version
    session = requests.Session()
    session.headers.update({'Authorization':'Bearer '+os.environ['GH_TOKEN'], 'Accept':'application/vnd.github+json',
                            'X-GitHub-Api-Version':'2022-11-28'})

    def call(method, path, **kwargs):
        response = session.request(method, 'https://api.github.com'+path, timeout=60, **kwargs)
        response.raise_for_status()
        return response.json() if response.content else None

    release = call('GET', f'/repos/{repo}/releases/{release_id}')
    assert release['draft'] and not release['prerelease'] and version in release['name']
    assert release['tag_name'] == tag or release['tag_name'].startswith('untagged-')
    remote_tag = call('GET', f'/repos/{repo}/git/ref/tags/{tag}')['object']
    if remote_tag['type'] == 'tag':
        remote_tag = call('GET', f'/repos/{repo}/git/tags/{remote_tag["sha"]}')['object']
    assert remote_tag['sha'] == commit
    existing = {asset['name']:asset for asset in call('GET',f'/repos/{repo}/releases/{release_id}/assets')}
    uploaded = []
    for name in sorted(expected):
        path = output/name
        checksum = digest(path)
        if name in existing:
            asset = existing[name]
            if asset['state'] == 'uploaded' and asset.get('digest') == 'sha256:'+checksum:
                uploaded.append({'name':name,'size':path.stat().st_size,'sha256':checksum})
                continue
            assert asset['state'] == 'starter' and asset.get('digest') is None, 'Refusing to replace a completed artifact'
            call('DELETE',f'/repos/{repo}/releases/assets/{asset["id"]}')
        with path.open('rb') as stream:
            response = session.post(f'https://uploads.github.com/repos/{repo}/releases/{release_id}/assets',
                                    params={'name':name},headers={'Content-Type':'application/octet-stream'},
                                    data=stream,timeout=(60,300))
        response.raise_for_status()
        asset = response.json()
        assert asset['state'] == 'uploaded' and asset['size'] == path.stat().st_size
        assert asset.get('digest') == 'sha256:'+checksum
        uploaded.append({'name':name,'size':path.stat().st_size,'sha256':checksum})
        print(json.dumps({'uploaded':name,'bytes':path.stat().st_size,'sha256':checksum}),flush=True)
    current = call('GET',f'/repos/{repo}/releases/{release_id}/assets')
    assert {asset['name'] for asset in current} == expected
    for asset in current:
        local = next(row for row in uploaded if row['name'] == asset['name'])
        assert asset['state'] == 'uploaded' and asset['size'] == local['size'] and asset['digest'] == 'sha256:'+local['sha256']
    verification = {'status':'passed','version':version,'tag':tag,'source_commit':commit,'bundled_files':len(manifest['files']),
                    'checks':['exact tagged source','installer and portable feature checks','manifest and SHA256SUMS','no user databases','release asset digests'],
                    'assets':uploaded}
    print('RELEASE_VERIFICATION_JSON='+json.dumps(verification),flush=True)
    with Path(os.environ['GITHUB_STEP_SUMMARY']).open('a',encoding='utf-8') as summary:
        summary.write(f'## Verified {tag} packages\n\nSource: `{commit}`\n\nInstaller and portable checks passed.\n\n')
        for asset in uploaded:
            summary.write(f'- `{asset["name"]}`: `{asset["sha256"]}`\n')


if __name__ == '__main__':
    main()

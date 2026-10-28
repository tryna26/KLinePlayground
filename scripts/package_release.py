"""Create a portable layout and release hashes without touching runtime data."""
import hashlib
import json
from pathlib import Path
import zipfile

ROOT=Path(__file__).resolve().parents[1]
VERSION='4.0.0-rc.1'


def main():
    folder=ROOT/'dist/KLinePlayground'
    output=ROOT/'dist/release';output.mkdir(parents=True,exist_ok=True)
    assert (folder/'KLinePlayground.exe').is_file()
    rows=[]
    for file in sorted(folder.rglob('*')):
        if not file.is_file():continue
        relative=file.relative_to(folder)
        # Never include home, caches, SQLite, credentials, or developer paths.
        if any(part.lower() in {'users','portfolio_reviewed','portfolio_replay','.runtime','.venv','.git'} for part in relative.parts):
            raise RuntimeError(f'Forbidden release path: {relative}')
        if file.suffix.lower() in {'.db','.sqlite','.sqlite3','.log','.enc'}:
            raise RuntimeError(f'Forbidden release file: {relative}')
        rows.append({'path':relative.as_posix(),'size':file.stat().st_size,'sha256':hashlib.sha256(file.read_bytes()).hexdigest()})
    manifest={'version':VERSION,'platform':'windows-x64','files':rows}
    (output/'bundle-manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    zip_path=output/f'KLinePlayground-{VERSION}-windows-x64-portable.zip'
    with zipfile.ZipFile(zip_path,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
        for row in rows:
            archive.write(folder/row['path'],f'KLinePlayground/bin/{row["path"]}')
    artifacts=[output/f'KLinePlayground-{VERSION}-windows-x64-setup.exe',zip_path,output/'bundle-manifest.json']
    (output/'SHA256SUMS.txt').write_text(''.join(f'{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n' for path in artifacts),encoding='utf-8')
    print(json.dumps({'version':VERSION,'bundled_files':len(rows),'artifacts':[p.name for p in artifacts]}))


if __name__=='__main__':main()

"""Verify a new portable extraction, default storage layout, and real-data download."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import zipfile

import requests


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--zip',type=Path,required=True)
    parser.add_argument('--manifest',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();root=args.output.resolve();root.mkdir(parents=True,exist_ok=False)
    manifest=json.loads(args.manifest.read_text(encoding='utf-8'))
    with zipfile.ZipFile(args.zip) as archive:
        for name in archive.namelist():
            assert (root/name).resolve().is_relative_to(root),'Unsafe archive path'
        for row in manifest['files']:
            content=archive.read('KLinePlayground/bin/'+row['path'])
            assert len(content)==row['size'] and hashlib.sha256(content).hexdigest()==row['sha256']
        archive.extractall(root)
    home=root/'KLinePlayground';ready_file=root/'ready.json'
    env={k:v for k,v in os.environ.items() if not k.startswith(('KLINE_TRAINER_','PYTHON'))}
    process=subprocess.Popen([str(home/'bin/KLinePlayground.exe'),'--serve-only','--ready-file',str(ready_file)],
        cwd=root,env=env,creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        deadline=time.monotonic()+50
        while not ready_file.is_file() and time.monotonic()<deadline:
            assert process.poll() is None,'Portable application exited'
            time.sleep(.2)
        ready=json.loads(ready_file.read_text(encoding='utf-8'))
        assert Path(ready['home'])==home and ready['frozen']
        session=requests.Session();session.trust_env=False;base=ready['url']
        def call(method,path,body=None):
            response=session.request(method,base+path,json=body,timeout=90)
            response.raise_for_status();return response.json()
        assert call('GET','/api/users')==[]
        call('POST','/api/users',{'username':'portable_acceptance'})
        call('POST','/api/portfolio/session',{'user':'portable_acceptance'})
        assert len(call('POST','/api/portfolio/prepare-data',{})['stocks'])==3
        params={'stocks':['600000','000001','600036'],'start_date':'2024-01-02','end_date':'2024-06-12','data_source':'sina'}
        assert call('POST','/api/portfolio/preview',params)['supported']
        result={'status':'passed','checks':['all archived hashes','clean extraction','portable home','real download','three-stock preview']}
        (root/'result.json').write_text(json.dumps(result,indent=2),encoding='utf-8');print(json.dumps(result))
    finally:
        process.terminate();process.wait(timeout=15)


if __name__=='__main__':main()

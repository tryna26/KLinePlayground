"""Opt-in online acceptance: genuine bars, isolated HTTP account, restart offline.

No developer fixtures or demo-mode flag. All users/ledger files are temporary.
"""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

import requests

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from backend.portfolio_real_data import BANKS, END, START, prepare_real_data


def main():
    with tempfile.TemporaryDirectory(prefix='kline-real-http-') as temp:
        root=Path(temp);data=root/'quotes';users=root/'users'
        data.mkdir()
        names={code:value[0] for code,value in BANKS.items()}
        (data/'stock_names.json').write_text(json.dumps(names,ensure_ascii=False),encoding='utf-8')
        (data/'stock_list.csv').write_text('code,name\n'+''.join(f'{c},{n}\n' for c,n in names.items()),encoding='utf-8')
        prepare_real_data(data)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        base=f'http://127.0.0.1:{port}'
        env={k:v for k,v in os.environ.items() if not k.startswith('KLINE_TRAINER_')}
        env['PYTHONIOENCODING']='utf-8'
        command=[sys.executable,str(ROOT/'run_dev.py'),'--data-dir',str(data),'--users-dir',str(users),'--port',str(port)]
        session=requests.Session();session.trust_env=False
        process=None

        def call(method,path,body=None,expected=200):
            response=session.request(method,base+path,json=body,timeout=30)
            assert response.status_code==expected,(path,response.status_code,response.text)
            return response.json()

        def start(log):
            child=subprocess.Popen(command,cwd=root,env=env,stdout=log,stderr=log)
            deadline=time.monotonic()+40
            while time.monotonic()<deadline:
                if child.poll() is not None:raise RuntimeError('real HTTP server exited')
                try:
                    call('GET','/api/health');return child
                except requests.ConnectionError:time.sleep(.15)
            child.terminate();child.wait(timeout=10)
            raise RuntimeError('real HTTP server did not start')

        with (root/'http.log').open('w',encoding='utf-8') as log:
            try:
                process=start(log)
                call('POST','/api/users',{'username':'real_acceptance'})
                call('POST','/api/portfolio/session',{'user':'real_acceptance'})
                config={'stocks':['600000','000001','600036'],'start_date':START,'end_date':END,'data_source':'sina','initial_cash':'100000'}
                catalog=call('GET','/api/portfolio/options?data_source=sina')
                assert {r['name'] for r in catalog['stocks']}==set(names.values())
                assert call('POST','/api/portfolio/preview',config)['supported']
                assert not call('POST','/api/portfolio/preview',{**config,'end_date':'2024-06-14'})['supported']
                state=call('POST','/api/portfolio/trainings',{'request_id':str(uuid4()),**config},201)
                assert not state['synthetic']
                prefix='/api/portfolio/trainings/'+state['training_id']
                history=call('GET',prefix+'/history/600000')['bars']
                assert len(history)==1 and history[0]['close']=='6.6' and history[0]['volume']=='22066700'
                call('GET',prefix+'/history/600000?through=2024-01-03',expected=400)

                def mutate(action,**fields):
                    return call('POST',prefix+'/'+action,{'request_id':str(uuid4()),'expected_version':state['version'],**fields})

                state=mutate('orders',stock='600000',side='buy',quantity=200)
                state=mutate('orders',stock='000001',side='buy',quantity=100)
                state=mutate('advance',expected_date=state['current_date'])
                assert [f['price'] for f in state['fills']]==['6.59','9.19']
                assert state['account']['cash']=='97752.98'
                assert state['daily_assets'][-1]['total_assets']=='100000.98'
                state=mutate('orders',stock='600000',side='sell',quantity=100)
                state=mutate('advance',expected_date=state['current_date'])
                assert state['account']['cash']=='98411.64'
                assert state['daily_assets'][-1]['total_assets']=='99984.64'
                assert state['fills'][-1]['fees']['total']=='5.34'
                process.terminate();process.wait(timeout=10);process=None
                # Remove sources from the application's path before restarting:
                # restored training must use its pinned ledger/replay observations.
                (data/'portfolio_reviewed').rename(data/'reviewed-not-available')
                (data/'portfolio_replay').rename(data/'replay-not-available')
                process=start(log)
                assert call('POST',prefix+'/restore',{})==state
                state=mutate('advance',expected_date=state['current_date'])
                assert state['current_date']=='2024-01-05'
                print(json.dumps({'result':'PASS','source':'real Sina unadjusted daily','stocks':list(names),
                    'window':[START,END],'checks':['105-day preview','out-of-range refusal','future guard',
                    'two-stock fills','partial sell and fees','restart without sources','continue pinned run']},ensure_ascii=False))
            finally:
                if process is not None:
                    process.terminate();process.wait(timeout=10)


if __name__=='__main__':
    if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')
    main()

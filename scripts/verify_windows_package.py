"""Opt-in installed Windows acceptance. Only a new dedicated test directory is used."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import time
from uuid import uuid4

import pandas as pd
import requests


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--installer',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True,help='New, empty test directory; retained for inspection')
    args=parser.parse_args()
    root=args.output.resolve();root.mkdir(parents=True,exist_ok=False)
    install=root/'installed';ready_file=root/'ready.json'
    subprocess.run([str(args.installer.resolve()),'/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/SP-',
        '/CURRENTUSER','/NOICONS',f'/DIR={install}',f'/LOG={root / "install.log"}'],check=True,timeout=180)
    exe=install/'bin/KLinePlayground.exe'
    assert exe.is_file()
    assert not (install/'data').exists() and not (install/'users').exists()
    env={k:v for k,v in os.environ.items() if not k.startswith(('KLINE_TRAINER_','PYTHON'))}
    session=requests.Session();session.trust_env=False
    process=None;base=None

    def call(method,path,body=None,expected=200):
        response=session.request(method,base+path,json=body,timeout=90)
        assert response.status_code==expected,(path,response.status_code,response.text[:1200])
        return response.json()

    def start():
        nonlocal base
        ready_file.unlink(missing_ok=True)
        child=subprocess.Popen([str(exe),'--serve-only','--ready-file',str(ready_file)],
            cwd=root,env=env,creationflags=subprocess.CREATE_NO_WINDOW)
        deadline=time.monotonic()+50
        while time.monotonic()<deadline:
            if child.poll() is not None:raise RuntimeError('Installed application exited; inspect installed/logs')
            if ready_file.is_file():
                ready=json.loads(ready_file.read_text(encoding='utf-8'))
                if ready['pid']==child.pid:
                    base=ready['url']
                    try:
                        call('GET','/api/health')
                        assert ready['frozen'] and Path(ready['home'])==install
                        return child
                    except requests.ConnectionError:pass
            time.sleep(.2)
        child.terminate();child.wait(timeout=10)
        raise RuntimeError('Installed application did not become ready')

    try:
        process=start()
        assert call('GET','/api/users')==[]
        for path in ('/','/portfolio.html','/js/portfolio.js','/js/portfolio_ui_math.js',
                     '/css/portfolio.css','/js/lightweight-charts.standalone.production.js'):
            assert session.get(base+path,timeout=10).status_code==200
        call('POST','/api/users',{'username':'windows_acceptance'})
        call('POST','/api/portfolio/session',{'user':'windows_acceptance'})
        metadata=call('POST','/api/portfolio/prepare-data',{})
        assert {row['stock'] for row in metadata['stocks']}=={'600000','000001','600036'}
        config={'stocks':['600000','000001','600036'],'start_date':'2024-01-02','end_date':'2024-06-12',
                'data_source':'sina','initial_cash':'100000'}
        assert call('POST','/api/portfolio/preview',config)['supported']
        state=call('POST','/api/portfolio/trainings',{'request_id':str(uuid4()),**config},201)
        assert not state['synthetic'] and state['account']['cash']=='100000.00'
        prefix='/api/portfolio/trainings/'+state['training_id']

        def mutate(action,**fields):
            return call('POST',prefix+'/'+action,{'request_id':str(uuid4()),'expected_version':state['version'],**fields})

        state=mutate('orders',stock='600000',side='buy',quantity=200)
        state=mutate('orders',stock='000001',side='buy',quantity=100)
        assert state['account']['available_cash']=='97524.98'
        assert len(call('GET',prefix+'/history/600036')['bars'])==1
        assert call('GET',prefix)==state
        call('GET',prefix+'/history/600000?through=2024-01-03',expected=400)
        state=mutate('advance',expected_date=state['current_date'])
        assert [f['price'] for f in state['fills']]==['6.59','9.19']
        assert state['account']['cash']=='97752.98' and state['daily_assets'][-1]['total_assets']=='100000.98'
        state=mutate('orders',stock='600000',side='sell',quantity=100)
        state=mutate('advance',expected_date=state['current_date'])
        assert state['account']['cash']=='98411.64' and state['daily_assets'][-1]['total_assets']=='99984.64'
        state=mutate('orders',stock='600000',side='buy',quantity=100)
        assert len([row for row in state['orders'] if row['status']=='pending'])==1
        process.terminate();process.wait(timeout=15);process=None
        process=start()
        assert call('POST',prefix+'/restore',{})==state
        state=mutate('advance',expected_date=state['current_date'])
        assert state['current_date']=='2024-01-05' and state['orders'][-1]['status']=='filled'
        ended=mutate('end');state=ended
        report=call('GET',prefix+'/report')
        assert report['summary']['final_assets']==state['daily_assets'][-1]['total_assets']
        # Legacy single-stock flow uses a clearly synthetic offline fixture;
        # it is created only after the genuine portfolio acceptance above.
        data=install/'data';offline=data/'a_market_offline';offline.mkdir(exist_ok=True)
        dates=pd.bdate_range('2024-01-01',periods=140)
        pd.DataFrame({'date':dates,'open':10.,'close':10.,'high':11.,'low':9.,'volume':10000,'factor':1.}).to_csv(offline/'600000.SH.csv',index=False)
        (data/'stock_names.json').write_text('{"600000":"SYNTHETIC SINGLE-STOCK TEST"}',encoding='utf-8')
        (data/'stock_list.csv').write_text('code,name\n600000,SYNTHETIC SINGLE-STOCK TEST\n',encoding='utf-8')
        original=call('POST','/api/training/start',{'user':'windows_acceptance','mode':'specific','stock_code':'600000',
            'start_date':'2024-03-01','data_source':'offline','initial_capital':100000})
        original_path=f'/api/training/{original["id"]}'
        call('POST',original_path+'/trade',{'action':'buy','quantity':1})
        call('POST',original_path+'/trade',{'action':'sell','quantity':1},expected=400)
        call('POST',original_path+'/next')
        call('POST',original_path+'/trade',{'action':'sell','quantity':1})
        assert call('POST',original_path+'/end')['final_capital']==99989.
        result={'status':'passed','version':'4.0.0-rc.1','training_id':state['training_id'],
            'date':state['current_date'],'cash':state['account']['cash'],'final_assets':report['summary']['final_assets'],
            'checks':['clean installation','frozen resources','real download and admission','shared cash','switch unchanged',
                      'next-open fills','partial sell','pending-order exact restart','resume','report reconciliation','legacy single-stock T+1'],
            'test_directory':str(root)}
        (root/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(result,ensure_ascii=False))
    finally:
        if process is not None:process.terminate();process.wait(timeout=15)


if __name__=='__main__':main()

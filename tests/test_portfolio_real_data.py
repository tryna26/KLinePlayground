"""Offline admission failure tests. All source records here are explicit mocks."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend.portfolio_real_data import (
    BANKS, END, START, ReviewedSinaSource, prepare_real_data, review_facts,
)
from backend.replay_models import ReplayDataError, day
from backend.replay_storage import read_cache, write_cache
from backend.sina_source import normalize_sina_records


class MockCalendar:
    def between(self, start, end):
        return tuple(map(day, [START, END]))


def mock_facts(code):
    name, listed = BANKS[code]
    return {
        'profile': {'records':[{'ASECCODE':code,'ASECNAME':name,'F006D':listed,'F002V':None}]},
        'dividends': {'records':[{'F020D':'2023-07-01'},{'F020D':'2024-07-01'}]},
        'capital': {'records':[{'SECCODE':code,'F002V':'定期报告'}]},
        'sina': {'old_names':[name], 'dividends':[
            ['2023-06-01','0','0','1','实施','2023-07-01','2023-06-30','--'],
            ['2024-06-20','0','0','1','实施','2024-07-01','2024-06-30','--']],
            'rights':[['暂时没有数据！']]},
        'quotes':[{'date':date,'open':'10','high':'11','low':'9','close':'10','volume':'12345'} for date in [START,END]],
    }


class RealAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.code='000001'
        self.facts=mock_facts(self.code)
        self.calendar=MockCalendar()

    def reject(self, facts=None):
        with self.assertRaises(ReplayDataError):
            review_facts(self.code, facts or self.facts, self.calendar)

    def test_identity_and_former_special_status_fail_closed(self):
        for field,value in [('ASECCODE','600000'),('ASECNAME','另一家公司'),('F006D','2024-01-01'),('F002V','ＳＴ银行')]:
            with self.subTest(field=field):
                facts=deepcopy(self.facts);facts['profile']['records'][0][field]=value
                self.reject(facts)
        self.facts['sina']['old_names']=[]
        self.reject()

    def test_dividend_record_date_blocks_even_if_ex_date_is_later(self):
        self.facts['dividends']['records'].append({'F018D':END,'F020D':'2024-06-14'})
        self.reject()

    def test_independent_action_source_and_unknown_progress_block(self):
        for kind in ('empty','undated','unknown','rights'):
            with self.subTest(kind=kind):
                facts=deepcopy(self.facts)
                if kind=='empty':facts['dividends']['records']=[]
                if kind=='undated':facts['dividends']['records'].append({'F020D':None})
                if kind=='unknown':facts['sina']['dividends'][0][4]='未知状态'
                if kind=='rights':facts['sina']['rights']=[]
                self.reject(facts)

    def test_no_distribution_requires_zero_amounts(self):
        self.facts['sina']['dividends'].append(['2024-02-01','0','0','0','不分配','--','--','--'])
        review_facts(self.code,self.facts,self.calendar)
        self.facts['sina']['dividends'][-1][3]='1'
        self.reject()

    def test_missing_calendar_day_or_zero_volume_blocks(self):
        facts=deepcopy(self.facts);facts['quotes'].pop()
        self.reject(facts)
        self.facts['quotes'][0]['volume']='0'
        self.reject()

    def test_unsupported_capital_change_blocks(self):
        facts=deepcopy(self.facts);facts['capital']['records']=[]
        self.reject(facts)
        self.facts['capital']['records'][0]['F002V']='送股'
        self.reject()

    def test_sina_preserves_actual_shares_and_raw_prices(self):
        rows=normalize_sina_records(self.code,self.facts['quotes'],day(START),day(END))
        self.assertEqual(rows[0]['volume'],'12345')
        self.assertEqual(rows[0]['close'],'10')
        bad=deepcopy(self.facts['quotes']);bad[0]['low']='12'
        with self.assertRaises(ReplayDataError):
            normalize_sina_records(self.code,bad,day(START),day(END))

    def test_failed_review_never_replaces_previous_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'portfolio_reviewed/history.json'
            path.parent.mkdir();path.write_text('{"previous":"keep"}',encoding='utf-8')
            facts={code:mock_facts(code) for code in BANKS}
            facts['000001']['quotes'].pop()
            with patch('backend.portfolio_real_data.load_public_calendar',return_value=self.calendar), \
                 patch('backend.portfolio_real_data._collect',side_effect=lambda code:facts[code]):
                with self.assertRaises(ReplayDataError):prepare_real_data(temp)
            self.assertEqual(path.read_text(encoding='utf-8'),'{"previous":"keep"}')

    def test_receipt_integrity_ranges_and_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch('backend.portfolio_real_data.load_public_calendar',return_value=self.calendar), \
                 patch('backend.portfolio_real_data._collect',side_effect=mock_facts):
                result=prepare_real_data(temp)
            self.assertNotIn('quotes',result)
            root=Path(temp)/'portfolio_reviewed'
            manifest=json.loads((root/'history.json').read_text(encoding='utf-8'))
            source=ReviewedSinaSource(root,manifest['receipts'])
            self.assertEqual(len(source.fetch(self.code,START,END)),2)
            with self.assertRaises(ReplayDataError):source.fetch(self.code,START,'2024-06-13')
            record=manifest['receipts'][self.code]
            path=root/record['path'];payload=read_cache(path);payload['quotes'][0]['close']='999'
            write_cache(path,payload)  # A matching wrapper hash alone cannot replace the admitted receipt.
            with self.assertRaises(ReplayDataError):source.fetch(self.code,START,END)
            record['path']='../outside.json'
            with self.assertRaises(ReplayDataError):source.fetch(self.code,START,END)


if __name__=='__main__':unittest.main()

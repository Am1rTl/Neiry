"""In-process integration checks. Fixtures are isolated; no hardware/network."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import analysis_engine
from lab_api import create_lab_router


def hashes(directory: Path):
    return {p.name:hashlib.sha256(p.read_bytes()).hexdigest()
            for p in directory.iterdir() if p.is_file() and not p.is_symlink()}


def strict_json(text):
    def invalid_constant(token):
        raise ValueError('Non-standard JSON constant: '+token)
    return json.loads(text,parse_constant=invalid_constant)


class LabApiIntegration(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.records=self.root/'records';self.records.mkdir()
        self.reports=self.root/'reports';self.reports.mkdir()
        self.static=self.root/'static';self.static.mkdir()
        (self.static/'knowledge.json').write_text(json.dumps({'sections':[],'sources':[],'protocol':[]}),encoding='utf-8')
        self.ident='session_test';self.session=self.records/self.ident;self.session.mkdir()
        self.metadata={'session_id':self.ident,'started_at':'2026-10-24T15:17:27+03:00',
            'channels':['O1-T3','O2-T4'],'sample_rates':{'eeg':250,'ppg':100,'mems':250}}
        (self.session/'meta.json').write_text(json.dumps(self.metadata),encoding='utf-8')
        (self.session/'eeg.csv').write_text('',encoding='utf-8')
        (self.session/'metrics.jsonl').write_text('{"stream":"productivity","currentValue":NaN,"ts_unix":1}\n',encoding='utf-8')
        (self.session/'events.jsonl').write_text('',encoding='utf-8')
        self.active=None
        router,self.store=create_lab_router(self.records,self.reports,self.static,lambda:self.active)
        app=FastAPI();app.include_router(router)
        self.client=TestClient(app)

    def tearDown(self):
        self.client.close();self.temp.cleanup()

    def get_report(self):
        return self.client.get('/api/lab/analysis/'+self.ident)

    def test_missing_eeg_strict_json_and_session_list(self):
        response=self.get_report();self.assertEqual(response.status_code,200)
        r=strict_json(response.text)
        self.assertFalse(r['eeg']['available']);self.assertEqual(r['quality']['status'],'no_eeg')
        self.assertIsNone(r['vendor']['streams'][0]['fields']['currentValue']['mean'])
        listing=self.client.get('/api/lab/sessions').json()['sessions']
        self.assertEqual(len(listing),1);self.assertEqual(listing[0]['id'],self.ident)
        self.assertTrue(listing[0]['analysis_ready']);self.assertFalse(listing[0]['eeg_available'])

    def test_cache_refresh_version_and_source_invalidation(self):
        with patch('analysis_engine.analyze_session',wraps=analysis_engine.analyze_session) as analysis:
            self.assertEqual(self.get_report().status_code,200);self.assertEqual(analysis.call_count,1)
            self.assertEqual(self.get_report().status_code,200);self.assertEqual(analysis.call_count,1)
            self.store._verified.clear()  # a new process must verify content hashes
            self.assertEqual(self.get_report().status_code,200);self.assertEqual(analysis.call_count,1)
            self.assertEqual(self.client.post('/api/lab/analysis/'+self.ident+'/refresh').status_code,200)
            self.assertEqual(analysis.call_count,2)
            (self.session/'events.jsonl').write_text('{"type":"marker","label":"new","ts_unix":1}\n',encoding='utf-8')
            r=self.get_report();self.assertEqual(r.status_code,200);self.assertEqual(analysis.call_count,3)
            self.assertEqual(r.json()['session']['markers_count'],1)
            report_path=self.reports/(self.ident+'.json')
            cached=json.loads(report_path.read_text());cached['analysis_version']='old-version'
            report_path.write_text(json.dumps(cached),encoding='utf-8')
            self.assertEqual(self.get_report().status_code,200);self.assertEqual(analysis.call_count,4)

    def test_context_sidecar_preserves_raw_and_metrics(self):
        before=hashes(self.session);computed=self.get_report().json()
        payload={'eye_state':'closed','task':'Покой','notes':'<script>alert("x")</script>'}
        response=self.client.patch('/api/lab/context/'+self.ident,json=payload)
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()['provenance'],'retrospective_user_context')
        self.assertEqual(hashes(self.session),before)
        result=self.get_report().json()
        self.assertEqual(result['context']['eye_state'],'closed')
        self.assertEqual(result['eeg'],computed['eeg'])
        self.assertEqual(result['observations'],computed['observations'])
        exported=self.client.get('/api/lab/analysis/'+self.ident+'/export?format=html')
        self.assertNotIn('<script>alert',exported.text)
        self.assertIn('&lt;script&gt;',exported.text)

    def test_exports_json_csv_html_and_validation(self):
        for format_,mime in [('json','application/json'),('csv','text/csv'),('html','text/html')]:
            with self.subTest(format=format_):
                r=self.client.get(f'/api/lab/analysis/{self.ident}/export?format={format_}')
                self.assertEqual(r.status_code,200);self.assertIn(mime,r.headers['content-type'])
                self.assertIn(self.ident+'.'+format_,r.headers['content-disposition'])
                if format_=='json':strict_json(r.text)
                elif format_=='csv':
                    rows=list(csv.reader(io.StringIO(r.text.lstrip('\ufeff'))))
                    self.assertEqual(rows[0],['section','channel','metric','value','units','note'])
                    self.assertGreater(len(rows),3)
                else:self.assertIn('Исследовательский отчёт',r.text)
        self.assertEqual(self.client.get(f'/api/lab/analysis/{self.ident}/export?format=pdf').status_code,422)
        self.assertEqual(self.client.patch('/api/lab/context/'+self.ident,json={'eye_state':'sleeping'}).status_code,422)
        self.assertEqual(self.client.patch('/api/lab/context/'+self.ident,json={'notes':'x'*4001}).status_code,422)

    def test_active_session_analysis_and_exports_blocked(self):
        self.active=self.ident
        self.assertEqual(self.get_report().status_code,409)
        self.assertEqual(self.client.post('/api/lab/analysis/'+self.ident+'/refresh').status_code,409)
        self.assertEqual(self.client.get('/api/lab/analysis/'+self.ident+'/export').status_code,409)
        self.assertFalse((self.reports/(self.ident+'.json')).exists())

    def test_source_change_during_analysis_rejected(self):
        actual=analysis_engine.analyze_session
        def mutate(path):
            result=actual(path)
            with (Path(path)/'events.jsonl').open('a') as h:h.write('{"type":"marker","ts_unix":2}\n')
            return result
        with patch('analysis_engine.analyze_session',side_effect=mutate):
            self.assertEqual(self.get_report().status_code,409)
        self.assertFalse((self.reports/(self.ident+'.json')).exists())

    def test_traversal_missing_and_symlinks_rejected(self):
        for bad in ('%2E%2E','..%5Cescape','%2Fetc%2Fpasswd','unknown'):
            response=self.client.get('/api/lab/analysis/'+bad)
            self.assertIn(response.status_code,(400,404))
        (self.records/'session_alias').symlink_to(self.session,target_is_directory=True)
        self.assertEqual(self.client.get('/api/lab/analysis/session_alias').status_code,400)
        external=self.root/'outside.csv';external.write_text('sensitive',encoding='utf-8')
        (self.session/'extra.json').symlink_to(external)
        self.assertEqual(self.get_report().status_code,400)
        listed=self.client.get('/api/lab/sessions').json()['sessions']
        self.assertEqual(listed,[])
        self.assertEqual(external.read_text(),'sensitive')


class ServerHttpBoundaries(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Import alone does not enter lifespan or launch hardware workers.
        import server
        cls.server=server
        cls.client=TestClient(server.app)

    @classmethod
    def tearDownClass(cls):cls.client.close()

    def test_invalid_numeric_query_parameters(self):
        for path in ('/api/spectrum?window_s=nan','/api/spectrum?fmax=-1',
                     '/api/spectrum?window_s=100000','/api/hrv?window_s=0',
                     '/api/replay/session_missing/start?speed=nan'):
            r=self.client.post(path) if '/replay/' in path else self.client.get(path)
            self.assertEqual(r.status_code,422,path)

    def test_cross_origin_mutation_blocked(self):
        r=self.client.post('/api/markers',json={'label':'x'},headers={'Origin':'https://untrusted.example'})
        self.assertEqual(r.status_code,403)
        r=self.client.post('/api/markers',json={'label':''},headers={'Origin':'http://testserver'})
        self.assertEqual(r.status_code,400)

    def test_status_strict_json_does_not_mutate_live_state(self):
        server=self.server
        original=server.S.latest_calibration
        try:
            server.S.latest_calibration={'invalid':float('nan')}
            r=self.client.get('/api/status');self.assertEqual(r.status_code,200)
            self.assertIsNone(strict_json(r.text)['calibration']['invalid'])
            self.assertTrue(server.S.latest_calibration['invalid'] != server.S.latest_calibration['invalid'])
            self.assertEqual(r.headers['x-content-type-options'],'nosniff')
        finally:server.S.latest_calibration=original

    def test_legacy_delete_and_download_do_not_follow_symlinks(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);records=root/'records';records.mkdir();target=records/'session_real';target.mkdir()
            (target/'meta.json').write_text('{}')
            (records/'session_alias').symlink_to(target,target_is_directory=True)
            with patch.object(self.server,'RECORDS_DIR',str(records)),patch.object(self.server,'RECORDER',None):
                self.assertEqual(self.client.delete('/api/records/session_alias').status_code,400)
                self.assertTrue(target.is_dir())
                external=root/'secret';external.write_text('outside')
                (target/'eeg.csv').symlink_to(external)
                self.assertEqual(self.client.get('/api/records/session_real/download').status_code,400)
                self.assertEqual(external.read_text(),'outside')

if __name__=='__main__':unittest.main()

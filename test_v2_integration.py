"""Research dashboard end-to-end regression tests (no proprietary hardware)."""
from __future__ import annotations
import csv
import json
import math
import tempfile
import unittest
from pathlib import Path
from fastapi import FastAPI
from fastapi.testclient import TestClient
from lab_api import create_lab_router


class DashboardV2Api(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name)
        self.records=root/'records';self.reports=root/'reports';self.static=root/'static'
        for path in (self.records,self.reports,self.static):path.mkdir()
        (self.static/'knowledge.json').write_text('{"sources":[],"sections":[]}', encoding='utf-8')
        self.ident='synthetic'
        self.path=self.records/self.ident;self.path.mkdir()
        (self.path/'meta.json').write_text(json.dumps({
            'session_id':self.ident,'started_at':'2026-11-11T00:00:00Z',
            'channels':['O1-T3','O2-T4'],'sample_rates':{'eeg':250,'ppg':100,'mems':250}}))
        with (self.path/'eeg.csv').open('w',newline='') as handle:
            writer=csv.writer(handle)
            writer.writerow(['sample_index','ts_unix','O1-T3','O2-T4'])
            for i in range(1250):
                writer.writerow([i,5 - i*.004,math.sin(i*2*math.pi*10/250),math.cos(i*2*math.pi*11/250)])
        self.active=None
        router,self.store=create_lab_router(self.records,self.reports,self.static,lambda:self.active)
        app=FastAPI();app.include_router(router)
        self.client=TestClient(app)
        self.addCleanup(self.client.close)

    def test_segment_preserves_order_and_has_band_metrics(self):
        r=self.client.get(f'/api/lab/segment/{self.ident}?start_s=0&duration_s=4')
        self.assertEqual(r.status_code,200,r.text)
        obj=r.json();self.assertTrue(obj['available']);self.assertEqual(obj['sample_count'],1000)
        self.assertEqual(obj['points'][0]['t'],0)
        self.assertLess(obj['points'][0]['t'],obj['points'][-1]['t'])
        self.assertEqual(len(obj['bands']),2)
        self.assertIn('alpha',obj['bands'][0]['bands'])

    def test_pdf_available_without_prebundled_file(self):
        r=self.client.get('/api/lab/report')
        self.assertEqual(r.status_code,200,r.text[:300] if r.status_code!=200 else '')
        self.assertEqual(r.headers['content-type'],'application/pdf')
        self.assertTrue(r.content.startswith(b'%PDF'))
        self.assertGreater(len(r.content),1500)

    def test_context_annotation_history_and_complete_deletion(self):
        self.assertEqual(self.client.get('/api/lab/analysis/'+self.ident).status_code,200)
        self.assertEqual(self.client.patch('/api/lab/context/'+self.ident,json={
            'eye_state':'open','task':'rest','participant_code':'anon01','protocol_id':'eyes'}).status_code,200)
        r=self.client.post('/api/lab/annotations/'+self.ident,json={
            'start_s':1,'duration_s':2,'category':'artifact','label':'motion'})
        self.assertEqual(r.status_code,200,r.text)
        history=self.client.get('/api/lab/history');self.assertEqual(history.status_code,200)
        self.assertEqual(history.json()['sessions'][0]['participant_code'],'anon01')
        self.assertTrue((self.reports/'cache'/f'{self.ident}.json').is_file())
        self.assertTrue((self.reports/'context'/f'{self.ident}.json').is_file())
        self.assertTrue((self.reports/'annotations'/f'{self.ident}.json').is_file())
        result=self.store.delete(self.ident)
        self.assertEqual(result['deleted'],self.ident)
        self.assertFalse(self.path.exists())
        for sidecar in ('cache','context','annotations'):
            self.assertFalse((self.reports/sidecar/f'{self.ident}.json').exists())
        self.assertFalse((self.reports/f'{self.ident}.json').exists())

    def test_bids_disallows_unknown_voltage_scale(self):
        result=self.client.post('/api/lab/bids/'+self.ident,json={
            'subject':'01','units':'','confirmed':False})
        self.assertEqual(result.status_code,409,result.text)

    def test_invalid_annotation_and_active_export(self):
        invalid=self.client.post('/api/lab/annotations/'+self.ident,
            content='{"start_s":NaN,"duration_s":2,"label":"invalid"}',
            headers={'Content-Type':'application/json'})
        self.assertIn(invalid.status_code,(400,422))
        self.active=self.ident
        self.assertEqual(self.client.post('/api/lab/bids/'+self.ident,json={
            'subject':'01','units':'uV','confirmed':True}).status_code,409)
        self.assertEqual(self.store.session(self.ident).name,self.ident)
        from fastapi import HTTPException
        with self.assertRaises(HTTPException):self.store.delete(self.ident)


if __name__ == '__main__':unittest.main()

"""In-process integration checks. Fixtures are isolated; no hardware/network."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import shutil
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
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

    def test_opening_session_runs_review_of_recorded_vs_unusable_streams(self):
        fs = self.metadata["sample_rates"]["ppg"]
        ppg = self.session / "ppg.csv"
        baseline = self.get_report().json()["quick_review"]["streams"]["ppg"]
        self.assertEqual((baseline["status"], baseline["sample_count"]), ("missing", 0))
        self.assertFalse(baseline["recorded"])

        def write_ppg(seconds):
            with ppg.open("w", newline="", encoding="utf-8") as file:
                writer = csv.writer(file)
                writer.writerow(["ts_unix", "sample_index", "value"])
                writer.writerows((i / fs, i, 1) for i in range(fs * seconds))

        write_ppg(5)
        short = self.get_report().json()["quick_review"]["streams"]["ppg"]
        self.assertEqual((short["status"], short["sample_count"]), ("too_short", 500))
        write_ppg(30)
        flat = self.get_report().json()["quick_review"]["streams"]["ppg"]
        self.assertEqual((flat["status"], flat["sample_count"], flat["total_windows"]),
                         ("rejected", 3000, 1))
        self.assertTrue(flat["recorded"])
        self.assertIsNone(flat["pulse_rate_bpm"])
        self.assertEqual(flat["top_reasons"], ["flatline"])

    def test_review_flags_pulse_estimate_based_on_one_window(self):
        fs = self.metadata["sample_rates"]["ppg"]
        ppg = self.session / "ppg.csv"
        with ppg.open("w", newline="", encoding="utf-8") as file:
            writer = csv.writer(file)
            writer.writerow(["ts_unix", "sample_index", "value"])
            for index in range(fs * 240):
                elapsed = index / fs
                value = math.sin(2 * math.pi * 1.2 * elapsed) if elapsed < 30 else 0
                writer.writerow([elapsed, index, value])

        review = self.get_report().json()["quick_review"]["streams"]["ppg"]
        self.assertTrue(review["recorded"])
        self.assertEqual((review["status"], review["accepted_windows"], review["total_windows"]),
                         ("limited", 1, 8))
        self.assertAlmostEqual(review["pulse_rate_bpm"], 72, delta=1)

    def test_review_distinguishes_passed_eeg_windows_from_limited_spectrum(self):
        fs = self.metadata["sample_rates"]["eeg"]
        eeg = self.session / "eeg.csv"
        with eeg.open("w", newline="", encoding="utf-8") as file:
            writer = csv.writer(file)
            writer.writerow(["ts_unix", "sample_index", "O1-T3", "O2-T4"])
            for index in range(fs * 40):
                elapsed = index / fs
                value = round(4 * math.sin(2 * math.pi * 10 * elapsed)) * .001
                writer.writerow([elapsed, index, value, -value])

        review = self.get_report().json()["quick_review"]["streams"]["eeg"]
        self.assertTrue(review["recorded"])
        self.assertEqual(review["spectral_channels"], 2)
        self.assertEqual(review["spectral_interpretability"], "severely_limited")
        self.assertEqual(review["coarse_channels"], ["O1-T3", "O2-T4"])
        self.assertEqual(review["status"], "limited")

    def test_review_uses_sdk_packet_timing_and_artifact_counts_when_present(self):
        packets = [
            (0, 2, 1000, 1004),
            (2, 2, 1008, 1012),
            (4, 2, 1032, 1036),
            (7, 2, 1040, 1044),
            (9, 2, 1042, 1046),
        ]
        fields = ("first_sample_index", "sample_count", "sdk_first_ms", "sdk_last_ms")
        rows = [dict(zip(fields, packet), stream="eeg_batch_timing") for packet in packets]
        rows.extend([
            {"stream": "eeg_batch_timing", "sample_count": 0},
            {"stream": "eeg_artifacts", "channels": [{"index": 0, "flags": 0}, {"index": 1, "flags": 2}]},
            {"stream": "eeg_artifacts", "channels": [{"index": 0, "flags": 4}]},
        ])
        (self.session / "metrics.jsonl").write_text("\n".join(map(json.dumps, rows)), encoding="utf-8")
        reviewed = self.get_report().json()["quick_review"]["sdk"]
        self.assertEqual(reviewed["eeg_timing"], {
            "valid_packets": 5, "invalid_packets": 1, "compared_steps": 3, "long_steps": 1,
            "nonincreasing_steps": 1, "noncontiguous_indices": 1, "longest_step_ms": 20,
            "long_step_threshold_ms": 6.0,
        })
        self.assertEqual(reviewed["eeg_artifact_channels"], [
            {"index": 0, "observations": 2, "nonzero_flags": 1},
            {"index": 1, "observations": 1, "nonzero_flags": 1},
        ])

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

    def test_archive_listing_and_history_require_verified_revision(self):
        for ident in (self.ident, 'session_other'):
            if ident != self.ident:
                shutil.copytree(self.session, self.records / ident)
            self.client.patch('/api/lab/context/' + ident, json={'participant_code': 'p1'})
        def item(endpoint, ident=None):
            return next(row for row in self.client.get('/api/lab/' + endpoint).json()['sessions']
                        if row['id'] == (ident or self.ident))

        self.assertEqual(item('sessions')['analysis_state'], 'pending')
        response = self.client.post(f'/api/lab/analysis/{self.ident}/verify')
        self.assertEqual(response.status_code, 200)
        revision = response.json()['analysis_revision']
        self.assertEqual(set(response.json()), {'id', 'analysis_state', 'analysis_revision'})
        self.client.post('/api/lab/analysis/session_other/verify').raise_for_status()
        self.assertEqual(self.get_report().json()['analysis_revision'], revision)
        self.assertEqual(item('history')['quality_status'], 'no_eeg')
        self.assertEqual(len(self.store.history()['comparable_groups']), 1)

        self.active = self.ident
        self.assertEqual(item('sessions')['analysis_state'], 'active')
        self.assertFalse(item('history')['analysis_ready'])
        self.assertEqual(self.client.post(f'/api/lab/analysis/{self.ident}/verify').status_code, 409)
        self.active = None
        (self.session / 'events.jsonl').write_text('{"type":"marker","ts_unix":1}\n', encoding='utf-8')
        with patch('analysis_engine.recording_source_signatures', side_effect=AssertionError("bulk hashing")):
            for endpoint in ('sessions', 'history'):
                row = item(endpoint)
                self.assertEqual(row['analysis_state'], 'stale')
                self.assertFalse(row['analysis_ready'])
                self.assertIsNone(row['analysis_revision'])
                self.assertIsNone(row['duration_s'])
                self.assertEqual(row['quality_status'], 'pending')
            self.assertEqual(self.store.history()['comparable_groups'], [])
            self.store._verified.clear()
            self.assertEqual(item('sessions')['analysis_state'], 'stale')
            self.assertEqual(item('sessions', 'session_other')['analysis_state'], 'unverified')
        refreshed = self.get_report().json()
        self.assertEqual(refreshed['session']['markers_count'], 1)
        self.assertNotEqual(refreshed['analysis_revision'], revision)
        self.assertEqual(item('history')['analysis_revision'], refreshed['analysis_revision'])

    def test_mixed_report_and_manifest_cannot_return_a_partial_session(self):
        report_path = self.reports / f'{self.ident}.json'
        old_revision = self.get_report().json()['analysis_revision']
        old = report_path.read_bytes()
        (self.session / 'events.jsonl').write_text('{"type":"marker","ts_unix":1}\n', encoding='utf-8')
        self.assertEqual(self.get_report().json()['session']['markers_count'], 1)
        report_path.write_bytes(old)  # current manifest survives a restored/externally exported report
        self.assertEqual(self.store.summaries()['sessions'][0]['analysis_state'], 'stale')
        self.store._verified.clear()
        result = self.get_report().json()
        self.assertEqual(result['session']['markers_count'], 1)
        self.assertNotEqual(result['analysis_revision'], old_revision)
        self.assertEqual(self.store.summaries()['sessions'][0]['analysis_revision'], result['analysis_revision'])

    def test_imported_cli_report_needs_one_verification_against_finished_sources(self):
        # A CLI-produced legacy report lacks the server's before/after checks
        # and may have been generated while the recording was still growing.
        old = analysis_engine.analyze_session(self.session)
        (self.reports / f'{self.ident}.json').write_text(json.dumps(old), encoding='utf-8')
        with patch('analysis_engine.analyze_session', wraps=analysis_engine.analyze_session) as compute:
            self.assertEqual(self.store.summaries()['sessions'][0]['analysis_state'], 'unverified')
            self.get_report().raise_for_status()
            self.assertEqual(compute.call_count, 1)
            self.store._verified.clear()
            self.get_report().raise_for_status()
            self.assertEqual(compute.call_count, 1)

    def test_explicit_verify_hashes_bytes_even_if_filesystem_state_appears_unchanged(self):
        import lab_api
        events = self.session / "events.jsonl"
        events.write_text('{"type":"marker","ts_unix":1}\n', encoding="utf-8")
        first = self.get_report().json()["analysis_revision"]
        unchanged = lab_api._source_state(self.session)
        with patch("lab_api._source_state", return_value=unchanged), \
                patch("analysis_engine.analyze_session", wraps=analysis_engine.analyze_session) as compute, \
                patch("analysis_engine.recording_source_signatures", wraps=analysis_engine.recording_source_signatures) as digest:
            # Mimic an in-place write on a filesystem that does not expose change time.
            events.write_text('{"type":"marker","ts_unix":2}\n', encoding="utf-8")
            self.assertEqual(self.store.summaries()["sessions"][0]["analysis_revision"], first)
            self.assertEqual(digest.call_count, 0)
            url = f"/api/lab/analysis/{self.ident}/verify"
            checked = self.client.post(url).json()["analysis_revision"]
            self.assertNotEqual(checked, first)
            self.assertEqual(self.client.post(url).json()["analysis_revision"], checked)
            self.assertEqual(compute.call_count, 1)
            self.assertGreaterEqual(digest.call_count, 2)

    def test_exports_keep_readiness_and_narrowband_denominators(self):
        from lab_api import _csv_report, _html_report
        report = self.get_report().json()
        self.assertIn("Готовность к исследованию", _html_report(report, {}))
        self.assertIn("alpha.status,no_eeg", _csv_report(report))
        qc = {"supported": True, "target_hz": 250 / 3, "dominance_fraction": .5, "eligible_windows": 7,
              "screened_windows": 0, "target_dominant_windows": 0, "combined_dominant_windows": 6,
              "screened_target_dominant_windows": 0, "screened_combined_dominant_windows": 0}
        report["eeg"]["channels"] = [{"name": "O1-T3", "narrowband_qc": qc}]
        csv_rows = list(csv.reader(io.StringIO(_csv_report(report))))
        values = {r[2]: r[3] for r in csv_rows if r[0] == "narrowband_qc"}
        self.assertEqual(values["target_dominant_windows"], "0")
        self.assertEqual(values["screened_target_dominant_windows"], "")
        self.assertEqual(values["eligible_windows"], "7")
        page = _html_report(report, {})
        self.assertIn("6 из 7 окон", page)
        self.assertIn("нет пригодных для подсчёта окон", page)
        qc["supported"] = False
        self.assertNotIn("6 из 7 окон", _html_report(report, {}))
        self.assertNotIn(",eligible_windows,", _csv_report(report))
        del report["eeg"]["channels"][0]["narrowband_qc"]
        del report["research_readiness"]
        self.assertNotIn("Готовность к исследованию", _html_report(report, {}))
        self.assertNotIn("narrowband_qc", _csv_report(report))

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
        events = self.session / 'events.jsonl'
        events.write_text('{"type":"marker","ts_unix":1}\n', encoding='utf-8')

        def mutate(path, **kwargs):
            result=actual(path, **kwargs)
            previous = events.stat()
            events.write_text('{"type":"marker","ts_unix":2}\n', encoding='utf-8')
            os.utime(events, ns=(previous.st_atime_ns, previous.st_mtime_ns))
            return result
        with patch('analysis_engine.analyze_session',side_effect=mutate):
            self.assertEqual(self.get_report().status_code,409)
        self.assertFalse((self.reports/(self.ident+'.json')).exists())

    def test_analysis_reuses_verified_hashes_in_provenance(self):
        extra = self.session / 'operator.json'
        extra.write_text('{"source":"demo"}', encoding='utf-8')
        signatures = analysis_engine.recording_source_signatures
        with patch('analysis_engine.recording_source_signatures', wraps=signatures) as read_hashes:
            r = self.get_report()
            self.assertEqual(r.status_code, 200)
            self.assertEqual(read_hashes.call_count, 2 if os.name == 'nt' else 1)
        recorded = {row['name']: row['sha256'] for row in r.json()['provenance']['files']}
        self.assertEqual(recorded, hashes(self.session))
        self.assertEqual(r.json()['provenance'], analysis_engine.analyze_session(self.session)['provenance'])

    def test_archive_worker_serializes_cold_reports_without_blocking_warm_reads(self):
        self.assertEqual(self.get_report().status_code, 200)
        cold_ids = ('session_cold_first', 'session_cold_second')
        for ident in cold_ids:
            shutil.copytree(self.session, self.records / ident)
        actual = analysis_engine.analyze_session
        first_started, second_started, release = threading.Event(), threading.Event(), threading.Event()

        def delayed(path, **kwargs):
            if Path(path).name == cold_ids[0]:
                first_started.set()
                if not release.wait(4):
                    raise TimeoutError('first archive report was not released')
            else:
                second_started.set()
            return actual(path, **kwargs)

        with patch('analysis_engine.analyze_session', side_effect=delayed), ThreadPoolExecutor(max_workers=3) as requests:
            first = requests.submit(self.client.get, '/api/lab/analysis/' + cold_ids[0])
            try:
                self.assertTrue(first_started.wait(2))
                second = requests.submit(self.client.get, '/api/lab/analysis/' + cold_ids[1])
                warm = requests.submit(self.get_report)
                self.assertEqual(warm.result(timeout=2).status_code, 200)
                self.assertFalse(second_started.is_set())
            finally:
                release.set()
            self.assertEqual(first.result(timeout=2).status_code, 200)
            self.assertEqual(second.result(timeout=2).status_code, 200)

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

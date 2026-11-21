import csv,json,tempfile,unittest
from pathlib import Path
from segments import experiment_bounds, segment
from test_recording import make_rec
class TestSegments(unittest.TestCase):
    def test_index_based_segment_and_bound(self):
        with tempfile.TemporaryDirectory() as tmp:
            d=Path(tmp)
            (d/'meta.json').write_text(json.dumps({'sample_rates':{'eeg':100}}))
            with (d/'eeg.csv').open('w',newline='') as h:
                w=csv.writer(h);w.writerow(['ts_unix','sample_index','O1','O2'])
                for i in range(1000):w.writerow([1000-i,i,i%37,i%23])
            r=segment(d,2,5,400)
            self.assertEqual(r['sample_count'],500)
            self.assertEqual(r['points'][0]['t'],2)
            self.assertLessEqual(len(r['points']),410)
            self.assertEqual(r['channels'],['O1','O2'])
            self.assertTrue(r['bands'][0]['bands'])
    def test_finished_experiment_inside_active_recording(self):
        with tempfile.TemporaryDirectory() as tmp:
            rec = make_rec(tmp, sample_rates={'eeg': 10})
            path = rec.start()
            try:
                rec.record_eeg([[9, 9]], [1])
                rec.event('experiment_lab_start', run_id='a')
                rec.record_eeg([[2, 3]] * 1500, range(1500))
                rec.event('experiment_lab_finish', run_id='a')
                rec.record_eeg([[8, 8]], [2])
                edges = experiment_bounds(path, 'a')
                self.assertEqual((edges['start_s'], edges['end_s']), (0.1, 150.1))
                self.assertFalse(edges['approximate'])
                overview = segment(path, edges['start_s'], 150)
                self.assertEqual(overview['sample_count'], 1500)
                self.assertTrue(all(p['values'] == [2, 3] for p in overview['points']))
                self.assertEqual(segment(path, 0.1, 0.2)['sample_count'], 2)
                self.assertTrue(rec.active)
            finally:
                rec.close()

    def test_legacy_protocol_and_missing_finish(self):
        with tempfile.TemporaryDirectory() as tmp:
            rec = make_rec(tmp, sample_rates={'eeg': 10})
            path = rec.start()
            rec.record_eeg([[2, 3]] * 10, range(10))
            rec.close()
            events = path / 'events.jsonl'
            start = {'type': 'protocol_start', 'run_id': 'a', 'ts_unix': 2}
            events.write_text(json.dumps(start) + '\n')
            with self.assertRaises(ValueError):
                experiment_bounds(path, 'a')
            with events.open('a') as out:
                out.write(json.dumps({**start, 'type': 'protocol_complete', 'ts_unix': 6}))
            result = experiment_bounds(path, 'a')
            self.assertTrue(result['approximate'])
            self.assertEqual((result['start_s'], result['end_s']), (0.2, 0.6))
if __name__=='__main__':unittest.main()

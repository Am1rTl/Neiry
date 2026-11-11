import csv,json,tempfile,unittest
from pathlib import Path
from segments import segment
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
if __name__=='__main__':unittest.main()

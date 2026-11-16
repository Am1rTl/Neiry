import math
import unittest
import numpy as np
from live_quality import assess_eeg, live_spectrogram

class TestLiveQuality(unittest.TestCase):
    def test_insufficient_and_constant(self):
        self.assertFalse(assess_eeg([[0.]]*20,250)['available'])
        r=assess_eeg([[0.,0.]]*1000,250)
        self.assertEqual(r['channels'][0]['status'],'warning')
        self.assertIn('постоянный',r['channels'][0]['reasons'][0])

    def test_line_interference(self):
        sr=250; t=np.arange(2000)/sr
        x=np.sin(2*np.pi*50*t)+.1*np.sin(2*np.pi*10*t)
        r=assess_eeg([[v] for v in x],sr)
        self.assertGreater(r['channels'][0]['line_power_fraction'],.5)

    def test_spectrogram_bounds(self):
        sr=250;t=np.arange(1500)/sr;x=np.sin(2*np.pi*10*t)
        r=live_spectrogram([[v] for v in x],sr)
        self.assertTrue(r['available']); a=r['channels'][0]
        self.assertTrue(a['available']);self.assertLessEqual(len(a['time_s']),64)
        self.assertGreater(len(a['freq_hz']),25)
        self.assertEqual(len(a['log10_psd']),len(a['freq_hz']))

    def test_spectrogram_preserves_channel_labels_and_missing_input(self):
        sr = 250
        t = np.arange(1500) / sr
        x = np.sin(2 * np.pi * 10 * t)
        data = np.column_stack((x, x / 2, np.full_like(x, np.nan), x * 2))
        result = live_spectrogram(data, sr, ['T3', 'T4', 'O1', 'O2'])
        self.assertTrue(result['available'])
        self.assertEqual([c['name'] for c in result['channels']], ['T3', 'T4', 'O1', 'O2'])
        self.assertEqual([c['available'] for c in result['channels']], [True, True, False, True])

if __name__=='__main__': unittest.main()

"""Hardware-free integration checks for real device lifecycle and unified API.

A fake vendor SDK is injected; passing these tests is NOT equivalent to testing
with a physical Neiry headband or validating Bluetooth on the user's OS.
"""
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _sdk_stubs import install
install()
from fastapi.testclient import TestClient
import server


class FakeCapsule:
    def __init__(self, lib): pass
    def get_lib(self): return object()
    def get_version(self): return "mock-1.0"


class FakeEntry:
    def __init__(self, serial, name): self.serial=serial;self.name=name
    def get_serial(self):return self.serial
    def get_name(self):return self.name


class FakeLocator:
    update_threads = set()
    instances = []
    def __init__(self, path, lib):
        self.callback=None; self.pending=False; self.updates=0;self.requests=0
        FakeLocator.instances.append(self)
    def set_on_devices_list(self,cb):self.callback=cb
    def request_devices(self, typ, duration):
        self.pending=True;self.requests+=1
    def update(self):
        FakeLocator.update_threads.add(threading.get_ident())
        self.updates+=1
        if self.pending:
            self.pending=False
            self.callback(self,[FakeEntry('AA-01','Neiry Alpha'),FakeEntry('BB-02','Neiry Beta')],0)
        for device in FakeDevice.instances:
            if device.connecting and not device.connected:
                device.pending_updates-=1
                if device.pending_updates<=0:
                    device.connected=True
                    if device.conn_callback:
                        device.conn_callback(device,"connected")


class FakeNames:
    def __len__(self):return 2
    def get_name_by_index(self,i):return ['O1-T3','O2-T4'][i]


class FakeInfo:
    def __init__(self, device):self.device=device
    def get_serial(self):return self.device.serial
    def get_name(self):return 'Neiry Headband'


class FakeDevice:
    instances=[]
    def __init__(self,locator,serial,lib):
        self.serial=serial;self.connected=False;self.connecting=False
        self.conn_callback=None;self.started=False;self.stop_calls=0;self.release_calls=0
        self.pending_updates=4
        FakeDevice.instances.append(self)
    def set_on_connection_status_changed(self, cb):self.conn_callback=cb
    def connect(self, **kwargs):
        self.connecting=True
        # False transitional event must not be treated as a connection.
        if self.conn_callback:self.conn_callback(self,'connecting')
    def is_connected(self):return self.connected
    def get_channel_names(self):return FakeNames()
    def get_info(self):return FakeInfo(self)
    def get_eeg_sample_rate(self):return 250
    def get_ppg_sample_rate(self):return 100
    def get_mems_sample_rate(self):return 250
    def start(self):self.started=True
    def stop(self):self.stop_calls+=1;self.connected=False
    def release(self):self.release_calls+=1;self.connected=False;self.connecting=False
    def __getattr__(self, name):
        if name.startswith('set_on_'):return lambda cb: None
        raise AttributeError(name)


class FakeCalibrator:
    instances=[]
    def __init__(self,device,lib):self.started=0;FakeCalibrator.instances.append(self)
    def set_on_calibration_finished(self,cb):self.callback=cb
    def calibrate_quick(self):self.started+=1


def until(predicate, timeout=3):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        if predicate():return True
        time.sleep(.05)
    return False


class TestNoDevice(unittest.TestCase):
    def setUp(self):
        self.old=server.S
        server.S=server.State()
    def tearDown(self):server.S=self.old

    def test_no_device_disables_calibration_and_recording(self):
        with patch.object(server,'SDK_AVAILABLE',False):
            client=TestClient(server.app)
            info=client.get('/api/device/status').json()
            self.assertFalse(info['connected'])
            self.assertFalse(info['calibration_available'])
            self.assertEqual(client.post('/api/device/scan').status_code,503)
            self.assertEqual(client.post('/api/device/calibration/start').status_code,409)
            self.assertEqual(client.post('/api/device/calibration/skip').status_code,409)
            self.assertEqual(client.post('/api/recording/start').status_code,409)
            self.assertEqual(client.post('/api/device/connect',json={'serial':'not_found'}).status_code,503)
            self.assertEqual(client.get('/api/lab/sessions').status_code,200)
            self.assertEqual(client.get('/#device').status_code,200)

    def test_skipping_does_not_claim_calibration(self):
        st=server.S
        st.status='connected';st._device_ready=True;st._calibrator=object();st.calibration_phase='ready'
        client=TestClient(server.app)
        r=client.post('/api/device/calibration/skip')
        self.assertEqual(r.status_code,200)
        self.assertFalse(r.json()['calibrated'])
        self.assertTrue(st._calibration_skip)


class TestFakeSdkLifecycle(unittest.TestCase):
    def test_discover_select_connect_disconnect_and_optional_calibrate(self):
        FakeDevice.instances.clear();FakeLocator.instances.clear();FakeLocator.update_threads.clear();FakeCalibrator.instances.clear()
        saved=server.S
        server.S=server.State()
        server._STOP.clear()
        try:
            with patch.multiple(server,Capsule=FakeCapsule,DeviceLocator=FakeLocator,Device=FakeDevice,
                                Calibrator=FakeCalibrator,SDK_AVAILABLE=True):
                with patch.object(server, '_rec_start', lambda *args, **kwargs: None):
                    with TestClient(server.app) as client:
                        self.assertTrue(until(lambda:server.S.sdk_ready))
                        status=client.get('/api/device/status').json()
                        self.assertEqual(status['status'],'idle')
                        self.assertFalse(status['connected'])
                        self.assertEqual(len(status['discovered']),0)
                        self.assertFalse(status['calibration_available'])
                        self.assertEqual(len(FakeDevice.instances),0)
                        self.assertEqual(client.post('/api/device/calibration/start').status_code,409)
                        self.assertEqual(client.post('/api/device/scan').status_code,200)
                        self.assertTrue(until(lambda:len(server.S.discovered_devices)==2))
                        self.assertEqual(len(FakeDevice.instances),0,'Discovery must not auto-connect')
                        self.assertEqual(client.post('/api/device/connect',json={'serial':'unknown'}).status_code,404)
                        r=client.post('/api/device/connect',json={'serial':'BB-02'})
                        self.assertEqual(r.status_code,200)
                        self.assertTrue(until(lambda:server.S._device_ready))
                        d=FakeDevice.instances[-1]
                        self.assertEqual(d.serial,'BB-02')
                        self.assertTrue(d.started)
                        self.assertEqual(client.get('/api/device/status').json()['serial'],'BB-02')
                        self.assertFalse(client.get('/api/device/status').json()['calibrated'])
                        self.assertEqual(FakeCalibrator.instances[-1].started,0,'Calibration must be opt-in')
                        self.assertEqual(client.post('/api/device/calibration/start').status_code,200)
                        self.assertTrue(until(lambda:FakeCalibrator.instances[-1].started==1))
                        self.assertEqual(client.post('/api/device/calibration/start').status_code,409)
                        self.assertEqual(client.post('/api/device/disconnect').status_code,200)
                        self.assertTrue(until(lambda:not server.S._device_ready and server.S.status=='idle'))
                        self.assertTrue(d.release_calls>=1)
                        self.assertFalse(client.get('/api/device/status').json()['calibration_available'])
                        self.assertEqual(client.post('/api/device/calibration/start').status_code,409)
                        self.assertEqual(client.post('/api/device/scan').status_code,200)
                        self.assertTrue(until(lambda:len(server.S.discovered_devices)==2))
                        self.assertEqual(len(FakeDevice.instances),1,'Rescan must not secretly auto-connect')
                        self.assertEqual(len(FakeLocator.update_threads),1,'Exactly one SDK pump thread')
        finally:
            server._STOP.clear()
            server.S=saved

if __name__=='__main__':unittest.main()

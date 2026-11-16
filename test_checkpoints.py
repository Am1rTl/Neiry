"""Recording checkpoint regressions using the local API and a real recorder."""

import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch
import zipfile

from fastapi.testclient import TestClient

from analysis_engine import analyze_session
from recording import SessionRecorder
import server


class TestCheckpoints(unittest.TestCase):
    def setUp(self):
        self.records = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(patch.multiple(server, RECORDS_DIR=self.records, RECORDER=None,
                                        LAB_STORE=None))
        self.client = TestClient(server.app)
        self.addCleanup(self.client.close)

    def start_recording(self, session_id):
        recorder = SessionRecorder(self.records, channel_names=["O1-T3", "O2-T4"],
                                   sample_rates={"eeg": 250})
        recorder.start(session_id=session_id)
        self.addCleanup(recorder.close)
        server.RECORDER = recorder

    def capture(self):
        before = time.time()
        response = self.client.post("/api/checkpoints", json={})
        self.assertTrue(response.is_success, response.text)
        capture = response.json()
        self.assertLessEqual(before, capture["ts_unix"])
        self.assertLessEqual(capture["ts_unix"], time.time())
        self.assertIsNone(capture["label"])
        return capture

    def events(self, session_id):
        events_path = self.records / session_id / "events.jsonl"
        return [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]

    def list_checkpoints(self, session_id=None):
        query = {"session_id": session_id} if session_id is not None else {}
        response = self.client.get("/api/checkpoints", params=query)
        self.assertTrue(response.is_success, response.text)
        return response.json()

    def test_capture_requires_an_active_recording(self):
        self.assertEqual(self.client.post("/api/checkpoints", json={}).status_code, 409)
        self.assertEqual(self.list_checkpoints(), {"session_id": None, "items": []})
        self.assertEqual(list(self.records.iterdir()), [])

    def test_capture_persists_each_checkpoint_before_naming_or_stopping(self):
        session_id = "session_first"
        self.start_recording(session_id)
        first, second = self.capture(), self.capture()
        self.assertNotEqual(first["id"], second["id"])

        # Read the file during the recording, without an API call that might flush it.
        markers = [event for event in self.events(session_id) if event["type"] == "marker"]
        self.assertEqual([e["checkpoint_id"] for e in markers], [first["id"], second["id"]])
        for checkpoint, event in zip((first, second), markers, strict=True):
            self.assertEqual(checkpoint["session_id"], session_id)
            self.assertEqual(event["ts_unix"], checkpoint["ts_unix"])
            self.assertIsNone(event["label"])

        response = self.client.post("/api/markers", json={"label": " Старый маркер "})
        self.assertTrue(response.is_success, response.text)
        self.assertEqual(response.json()["label"], "Старый маркер")
        self.assertEqual(self.client.post("/api/markers", json={"label": " "}).status_code, 400)
        listing = self.list_checkpoints()
        self.assertEqual(listing["session_id"], session_id)
        self.assertCountEqual([item["id"] for item in listing["items"]], [first["id"], second["id"]])

    def test_late_name_changes_only_the_original_checkpoint(self):
        first_session = "session_first"
        self.start_recording(first_session)
        named, unnamed = self.capture(), self.capture()
        stopped = self.client.post("/api/recording/stop")
        self.assertTrue(stopped.is_success, stopped.text)

        next_session = "session_next"
        self.start_recording(next_session)
        current = self.capture()
        endpoint = f"/api/checkpoints/{named['id']}"
        rejected = self.client.patch(endpoint, json={"session_id": next_session, "label": "Чужая сессия"})
        self.assertEqual(rejected.status_code, 404)

        for value in (" Глаза закрыты ", "Глаза закрыты"):
            response = self.client.patch(endpoint, json={"session_id": first_session, "label": value})
            self.assertTrue(response.is_success, response.text)
            saved = response.json()
            self.assertEqual(saved["ts_unix"], named["ts_unix"])
            self.assertEqual(saved["label"], "Глаза закрыты")

        original = {item["id"]: item for item in self.list_checkpoints(first_session)["items"]}
        self.assertEqual(set(original), {named["id"], unnamed["id"]})
        self.assertEqual(original[named["id"]]["label"], "Глаза закрыты")
        self.assertIsNone(original[unnamed["id"]]["label"])
        live_items = self.list_checkpoints()["items"]
        self.assertEqual([item["id"] for item in live_items], [current["id"]])
        self.assertIsNone(live_items[0]["label"])

        response = self.client.get(f"/api/records/{first_session}/download")
        self.assertTrue(response.is_success, response.text)
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            events = [json.loads(line) for line in archive.read(f"{first_session}/events.jsonl").splitlines()]
        history = [e for e in events if e.get("checkpoint_id") == named["id"]]
        self.assertEqual([e["type"] for e in history].count("marker"), 1)
        self.assertIsNone(history[0]["label"])
        self.assertEqual(history[0]["ts_unix"], named["ts_unix"])
        self.assertEqual(history[-1]["type"], "checkpoint_label")
        self.assertEqual(history[-1]["label"], "Глаза закрыты")

        session = analyze_session(self.records / first_session)["session"]
        reported = {e["checkpoint_id"]: e for e in session["events"] if e["type"] == "marker"}
        self.assertEqual(set(reported), {named["id"], unnamed["id"]})
        self.assertEqual(reported[named["id"]]["ts_unix"], named["ts_unix"])
        self.assertEqual(reported[named["id"]]["label"], "Глаза закрыты")
        self.assertIsNone(reported[unnamed["id"]]["label"])
        self.assertNotIn("checkpoint_label", [e["type"] for e in session["events"]])

"""Тесты логики подключения Capsule WebApp. Запуск: .venv/bin/python -m unittest test_connection -v

Покрывает два инварианта, нарушение которых роняло подключение повязки:
1. locator.update() нативного SDK вызывается ровно из одного потока: пока
   воркер подключения владеет прокачкой, главный цикл сканирования молчит.
2. Неудачная попытка подключения (таймаут STAGE 8, крэш воркера) сбрасывает
   состояние обработчика, и следующая волна сканирования пробует снова,
   а не клинит сервер навсегда.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _sdk_stubs import install as _install_sdk_stubs

_install_sdk_stubs()  # стабы SDK — ДО import server, чтобы тесты шли без железа

import server
from server import (
    _attempt_failed,
    _main_may_pump,
    _worker_claim_pump,
    _worker_release_pump,
)


class TestPumpOwnership(unittest.TestCase):
    def setUp(self):
        with server.STATE_LOCK:
            self._saved = server.S._worker_active
            server.S._worker_active = False

    def tearDown(self):
        with server.STATE_LOCK:
            server.S._worker_active = self._saved

    def test_main_may_pump_when_idle(self):
        self.assertTrue(_main_may_pump())

    def test_main_must_not_pump_while_worker_active(self):
        self.assertTrue(_worker_claim_pump())
        try:
            self.assertFalse(_main_may_pump())
        finally:
            _worker_release_pump()
        self.assertTrue(_main_may_pump())

    def test_double_claim_fails(self):
        self.assertTrue(_worker_claim_pump())
        try:
            self.assertFalse(_worker_claim_pump())
        finally:
            _worker_release_pump()


class TestRetryAfterFailure(unittest.TestCase):
    def setUp(self):
        with server.STATE_LOCK:
            self._saved = server.S._worker_active
            server.S._worker_active = False

    def tearDown(self):
        with server.STATE_LOCK:
            server.S._worker_active = self._saved

    def test_failed_attempt_resets_handler_for_retry(self):
        handler_state = {"done": True}  # воркер был запущен и занял обработчик
        self.assertTrue(_worker_claim_pump())
        _attempt_failed(handler_state)
        self.assertFalse(handler_state["done"])
        self.assertTrue(_main_may_pump())


if __name__ == "__main__":
    unittest.main()

"""Стабы модулей Capsule SDK для тестов без железа.

install() кладёт в sys.modules минимальные модули-заглушки с теми же
именами, что импортирует server.py (Capsule, DeviceLocator, DeviceType,
Device, Calibrator, Emotions, Cardio, PhysiologicalStates, Productivity,
MEMS, Error, CapsulePointersImpl), поэтому `import server` работает и на
машине без поставки CapsuleAPI. Тесты чистой логики (test_connection,
test_protocol) нативный libCapsuleClient.so не трогают — им достаточно
самих имён.

Стабы ставятся безусловно (а не только при отсутствии SDK), чтобы тесты
вели себя одинаково в любом окружении.
"""

import sys
import types


class _Stub:
    """Базовый стаб: принимает любые аргументы, глотает любые вызовы."""

    def __init__(self, *args, **kwargs):
        self._stub_args = args
        self._stub_kwargs = kwargs

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)

        def _noop(*args, **kwargs):
            return None

        return _noop


class Capsule(_Stub):
    pass


class DeviceLocator(_Stub):
    pass


class DeviceType:
    """Стаб enum'а DeviceType: коду нужен только DeviceType.Band."""

    Band = 0


class Device(_Stub):
    pass


class Calibrator(_Stub):
    pass


class Emotions(_Stub):
    pass


class Cardio(_Stub):
    pass


class PhysiologicalStates(_Stub):
    pass


class Productivity(_Stub):
    pass


class MEMS(_Stub):
    pass


class Error_Code:
    OK = 0


class Error(_Stub):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.code = Error_Code.OK


class CapsuleException(Exception):
    pass


def _plant(name, attrs):
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[name] = mod


def install():
    """Положить стабы модулей Capsule SDK в sys.modules.

    Вызывать ДО `import server`. Возвращает список подменённых имён
    модулей. Повторный вызов безопасен (идемпотентен по составу).
    """
    stubs = {
        "Capsule": {"Capsule": Capsule},
        "DeviceLocator": {"DeviceLocator": DeviceLocator},
        "DeviceType": {"DeviceType": DeviceType},
        "Device": {"Device": Device},
        "Calibrator": {"Calibrator": Calibrator},
        "Emotions": {"Emotions": Emotions},
        "Cardio": {"Cardio": Cardio},
        "PhysiologicalStates": {"PhysiologicalStates": PhysiologicalStates},
        "Productivity": {"Productivity": Productivity},
        "MEMS": {"MEMS": MEMS},
        "Error": {
            "Error": Error,
            "Error_Code": Error_Code,
            "CapsuleException": CapsuleException,
        },
        "CapsulePointersImpl": {"capsule_pointers": {}},
    }
    for name, attrs in stubs.items():
        _plant(name, attrs)
    return sorted(stubs)

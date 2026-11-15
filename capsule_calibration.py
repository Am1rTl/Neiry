"""Individual NFB calibration protocol and the Capsule staged C binding."""

import ctypes
from dataclasses import dataclass, field
from uuid import uuid4

EYE_STATES = {"quick": ("closed",), "full": ("closed", "open", "closed", "open")}
STAGE_SECONDS = {"quick": 30, "full": 20}
CUE_SECONDS = 3.0


@dataclass
class CalibrationRun:
    mode: str
    prepare_until: float
    session_id: str | None
    run_id: str = field(default_factory=lambda: uuid4().hex[:12])
    auto_record: bool = True
    stage: int = 0
    started: float | None = None
    stage_finished: bool = False
    previous_sdk_failure: bool = False
    baselines_at: float | None = None

    @property
    def last_stage(self) -> bool:
        return self.stage == len(EYE_STATES[self.mode]) - 1

    def event_fields(self) -> dict:
        return {
            "run_id": self.run_id,
            "mode": self.mode,
            "stage": self.stage + 1,
            "stage_count": len(EYE_STATES[self.mode]),
            "eye_state": EYE_STATES[self.mode][self.stage],
        }

    def status(self, now: float) -> dict:
        return {
            **self.event_fields(),
            "preparing": self.started is None,
            "prepare_remaining_s": max(0.0, self.prepare_until - now) if self.started is None else 0.0,
            "elapsed_s": max(0.0, now - self.started) if self.started is not None else 0.0,
            "expected_stage_s": STAGE_SECONDS[self.mode],
        }


def bind_staged_calibration(lib, calibrator, error_type, check_error, on_finished):
    """The 2.0.72 Python calibrate(stage) calls the quick symbol with the wrong ABI."""
    pointer_type = ctypes.POINTER(ctypes.c_int)
    callback_type = ctypes.CFUNCTYPE(None, pointer_type)
    start = lib.clCNFBCalibrator_CalibrateIndividualNFB
    start.argtypes = [pointer_type, ctypes.c_int, ctypes.POINTER(error_type)]
    start.restype = None
    subscribe = lib.clCNFBCalibrator_SetOnCalibrationStageFinishedEvent
    subscribe.argtypes = [pointer_type, callback_type]
    subscribe.restype = None
    pointer = calibrator.get_c_pointer()
    if not pointer:
        raise RuntimeError("Capsule returned a null calibrator handle")
    callback = callback_type(lambda _pointer: on_finished(calibrator))
    subscribe(pointer, callback)

    def start_stage(stage):
        error = error_type()
        start(pointer, stage, ctypes.byref(error))
        check_error(error)
    # Keep the native callback alive for as long as the device owns its subscription.
    start_stage.callback = callback
    return start_stage


def bind_calibration_result(lib, calibrator, result_type, on_result):
    """Keep callbacks tied to the original native device after a reconnect."""
    pointer_type = ctypes.POINTER(ctypes.c_int)
    callback_type = ctypes.CFUNCTYPE(None, pointer_type, ctypes.POINTER(result_type))
    subscribe = lib.clCNFBCalibrator_SetOnCalibratedEvent
    subscribe.argtypes = [pointer_type, callback_type]
    subscribe.restype = None

    @callback_type
    def callback(_pointer, data):
        if data:
            on_result(calibrator, data.contents)

    pointer = calibrator.get_c_pointer()
    if not pointer:
        raise RuntimeError("Capsule returned a null calibrator handle")
    subscribe(pointer, callback)
    return callback

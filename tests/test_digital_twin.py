"""Unit tests for the phase-four hardware digital twin."""

from hardware.mock_hardware import MockCamera, MockConveyor, MockSerialTransport


def test_mock_conveyor_records_safe_stop() -> None:
    """The simulated conveyor must preserve the production stop protocol."""
    transport = MockSerialTransport()
    conveyor = MockConveyor(transport)

    conveyor.move_step()
    conveyor.emergency_stop()

    assert transport.commands == ["G3", "G1", "G25"]
    assert conveyor.stopped is True


def test_mock_camera_returns_requested_roles() -> None:
    """The simulated camera source returns an isolated blank frame per role."""
    camera = MockCamera(roles=("INPUT_LEFT", "TOP"))

    frames = camera.capture_roles(("INPUT_LEFT", "TOP"))

    assert set(frames) == {"INPUT_LEFT", "TOP"}
    assert frames["INPUT_LEFT"].shape == (720, 1280, 3)
    assert frames["INPUT_LEFT"] is not frames["TOP"]

"""Round-7 WS26: parallelism default-on for boards >= 30 parts.

Covers the resolution matrix (kwarg > env > implicit part-count default) and a
real-subprocess proof that the default path is byte-identical to forced
sequential on a ~30-part fake board.
"""
from __future__ import annotations

import pytest

from skidl_layout import plan_layout
from skidl_layout.engine import (
    _PARALLEL_DEFAULT_MIN_PARTS,
    _effective_parallel_workers,
)

from tests.test_layout_parallel import BBOXES, _Circuit, _Net, _Part, _sig


class _FakeCircuit:
    def __init__(self, n):
        self.parts = list(range(n))


# --- resolution matrix -------------------------------------------------------


def test_default_resolution_matrix(monkeypatch):
    monkeypatch.setattr("os.cpu_count", lambda: 8)

    monkeypatch.delenv("SKIDL_LAYOUT_PARALLEL", raising=False)
    # implicit default engages at the threshold -> min(4, 8) = 4
    assert _effective_parallel_workers(None, _FakeCircuit(30)) == 4
    assert _effective_parallel_workers(None, _FakeCircuit(100)) == 4
    # one below the threshold -> None (sequential)
    assert _effective_parallel_workers(None, _FakeCircuit(29)) is None
    assert _PARALLEL_DEFAULT_MIN_PARTS == 30

    # explicit kwarg 1 -> kill switch (returned as-is; caller's >=2 check stays seq)
    assert _effective_parallel_workers(1, _FakeCircuit(100)) == 1
    # explicit kwarg beats the part guard on a tiny board
    assert _effective_parallel_workers(6, _FakeCircuit(3)) == 6

    # env kill switch
    monkeypatch.setenv("SKIDL_LAYOUT_PARALLEL", "1")
    assert _effective_parallel_workers(None, _FakeCircuit(100)) == 1
    # explicit env beats the part guard on a tiny board
    monkeypatch.setenv("SKIDL_LAYOUT_PARALLEL", "6")
    assert _effective_parallel_workers(None, _FakeCircuit(3)) == 6
    # explicit kwarg still beats env
    assert _effective_parallel_workers(2, _FakeCircuit(3)) == 2


def test_default_low_cpu_stays_sequential(monkeypatch):
    """A 1-core machine resolves min(4,1)=1 -> the >=2 engage check keeps it
    sequential even on a big board."""
    monkeypatch.delenv("SKIDL_LAYOUT_PARALLEL", raising=False)
    monkeypatch.setattr("os.cpu_count", lambda: 1)
    assert _effective_parallel_workers(None, _FakeCircuit(100)) == 1


# --- real-subprocess default-path proof --------------------------------------


def _big_circuit(reps=10):
    """~30-part board: reps copies of the MCU/cap/connector trio with distinct
    refs and per-trio nets (mirrors tests/test_layout_parallel.py fakes)."""
    parts = []
    nets = []
    for k in range(reps):
        vbus = _Net(f"VBUS{k}")
        vcc = _Net(f"3V3_{k}")
        gnd = _Net(f"GND{k}")
        u = _Part(f"U{k}", name="MCU", footprint="Package_QFP:MCU",
                  nets=[vcc, gnd], pins=2)
        c = _Part(f"C{k}", value="100nF", footprint="Capacitor:C_0805",
                  nets=[vcc, gnd])
        j = _Part(f"J{k}", name="USB connector", footprint="Connector:USB",
                  nets=[vbus, gnd])
        parts.extend([u, c, j])
        nets.extend([vbus, vcc, gnd])
    return _Circuit(parts, nets)


def test_default_equals_forced_sequential_on_big_fake_board(monkeypatch):
    monkeypatch.delenv("SKIDL_LAYOUT_PARALLEL", raising=False)
    assert len(_big_circuit().parts) >= _PARALLEL_DEFAULT_MIN_PARTS

    msgs: list[str] = []
    default = plan_layout(
        _big_circuit(), fp_bboxes=BBOXES, progress=lambda m: msgs.append(m)
    )
    seq = plan_layout(_big_circuit(), fp_bboxes=BBOXES, parallel_workers=1)

    assert any("in parallel" in m for m in msgs), "default path did not parallelize"
    assert _sig(default) == _sig(seq)
    assert default.score.to_dict() == seq.score.to_dict()
    assert default.report == seq.report
    assert [c.name for c in default.candidates] == [c.name for c in seq.candidates]

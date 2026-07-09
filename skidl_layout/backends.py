from __future__ import annotations

import importlib.util
from dataclasses import dataclass


@dataclass(frozen=True)
class OptionalBackendStatus:
    networkx: bool = False
    scipy: bool = False
    shapely: bool = False
    ortools: bool = False

    @property
    def enabled(self) -> list[str]:
        return [
            name
            for name, available in (
                ("networkx", self.networkx),
                ("scipy", self.scipy),
                ("shapely", self.shapely),
                ("ortools", self.ortools),
            )
            if available
        ]


def optional_backend_status() -> OptionalBackendStatus:
    """Return optional optimization/geometric backend availability."""
    return OptionalBackendStatus(
        networkx=importlib.util.find_spec("networkx") is not None,
        scipy=importlib.util.find_spec("scipy") is not None,
        shapely=importlib.util.find_spec("shapely") is not None,
        ortools=importlib.util.find_spec("ortools") is not None,
    )

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FixedPosition:
    ref: str
    x_mm: float
    y_mm: float
    rot_deg: float = 0.0


@dataclass
class AnchorZone:
    group_name: str
    x_min: float
    y_min: float
    x_max: float
    y_max: float
    refs: list[str] = field(default_factory=list)


@dataclass
class KeepOut:
    x_min: float
    y_min: float
    x_max: float
    y_max: float
    allowed_refs: list[str] = field(default_factory=list)


@dataclass
class BoardCutout:
    """Physical void in the board, distinct from a placement keepout."""

    x_min: float
    y_min: float
    x_max: float
    y_max: float
    shape: str = "rect"
    name: str = ""
    vertices: list[tuple[float, float]] = field(default_factory=list)
    radius_mm: float | None = None

    @property
    def width_mm(self) -> float:
        return self.x_max - self.x_min

    @property
    def height_mm(self) -> float:
        return self.y_max - self.y_min

    @property
    def center_x_mm(self) -> float:
        return (self.x_min + self.x_max) / 2

    @property
    def center_y_mm(self) -> float:
        return (self.y_min + self.y_max) / 2

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        return self.x_min, self.y_min, self.x_max, self.y_max

    def to_keepout(self) -> KeepOut:
        return KeepOut(self.x_min, self.y_min, self.x_max, self.y_max)

    def to_dict(self) -> dict:
        data = {
            "shape": self.shape,
            "x_min": self.x_min,
            "y_min": self.y_min,
            "x_max": self.x_max,
            "y_max": self.y_max,
        }
        if self.name:
            data["name"] = self.name
        if self.vertices:
            data["vertices"] = [(x, y) for x, y in self.vertices]
        if self.radius_mm is not None:
            data["radius_mm"] = self.radius_mm
        return data


@dataclass
class EdgeAnchor:
    ref: str
    edge: str
    offset_mm: float | None = None
    inset_mm: float = 0.5
    rot_deg: float | None = None


@dataclass
class AlignConstraint:
    refs: list[str]
    axis: str
    value_mm: float | None = None


@dataclass
class DistributeConstraint:
    refs: list[str]
    axis: str
    start_mm: float | None = None
    end_mm: float | None = None


@dataclass
class NearConstraint:
    ref: str
    target_ref: str
    distance_mm: float = 5.0


@dataclass
class FarConstraint:
    ref: str
    target_ref: str
    distance_mm: float = 10.0


@dataclass
class FaceEdgeConstraint:
    ref: str
    edge: str
    rot_deg: float | None = None


@dataclass(init=False)
class BoardOutline:
    vertices: list[tuple[float, float]] = field(default_factory=list)

    def __init__(
        self,
        width_mm: float = 0.0,
        height_mm: float = 0.0,
        vertices=None,
        corner_radius_mm: float = 0.0,
    ):
        self.corner_radius_mm = max(0.0, float(corner_radius_mm or 0.0))
        if vertices is not None:
            self.vertices = [(float(x), float(y)) for x, y in vertices]
        elif width_mm > 0 and height_mm > 0:
            width_mm = float(width_mm)
            height_mm = float(height_mm)
            self.vertices = [
                (0.0, 0.0),
                (width_mm, 0.0),
                (width_mm, height_mm),
                (0.0, height_mm),
            ]
        else:
            self.vertices = []

    @property
    def x_min(self) -> float:
        if not self.vertices:
            return 0.0
        return min(x for x, _ in self.vertices)

    @property
    def y_min(self) -> float:
        if not self.vertices:
            return 0.0
        return min(y for _, y in self.vertices)

    @property
    def x_max(self) -> float:
        if not self.vertices:
            return 0.0
        return max(x for x, _ in self.vertices)

    @property
    def y_max(self) -> float:
        if not self.vertices:
            return 0.0
        return max(y for _, y in self.vertices)

    @property
    def width_mm(self) -> float:
        return self.x_max - self.x_min

    @property
    def height_mm(self) -> float:
        return self.y_max - self.y_min


FORM_FACTORS: dict[str, BoardOutline] = {
    "feather": BoardOutline(50.8, 22.86),
    "qt_py": BoardOutline(17.78, 17.78),
    "metro": BoardOutline(82.55, 53.34),
    "metro_mini": BoardOutline(68.58, 53.34),
    "trinket": BoardOutline(27.0, 15.3),
    "itsybitsy": BoardOutline(35.56, 17.78),
    "shield_uno": BoardOutline(68.58, 53.34),
}


@dataclass
class LayoutConstraints:
    fixed: list = field(default_factory=list)
    zones: list = field(default_factory=list)
    edge_anchors: list = field(default_factory=list)
    keepouts: list = field(default_factory=list)
    cutouts: list = field(default_factory=list)
    align: list = field(default_factory=list)
    distribute: list = field(default_factory=list)
    near: list = field(default_factory=list)
    far: list = field(default_factory=list)
    face_edges: list = field(default_factory=list)
    outline: BoardOutline = None
    form_factor: str | None = None

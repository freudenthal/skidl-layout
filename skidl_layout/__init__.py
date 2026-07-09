from .constraints import (
    AnchorZone,
    AlignConstraint,
    BoardOutline,
    BoardCutout,
    DistributeConstraint,
    EdgeAnchor,
    FaceEdgeConstraint,
    FarConstraint,
    FORM_FACTORS,
    FixedPosition,
    KeepOut,
    LayoutConstraints,
    NearConstraint,
)
from .backends import OptionalBackendStatus, optional_backend_status
from .candidates import PlacementCandidate, generate_placement_candidates
from .congestion import (
    CongestionMap,
    CongestionRegion,
    build_congestion_map,
)
from .decaps import (
    DecapPlacementIntent,
    DecapRefinementResult,
    infer_decap_placement_intents,
    refine_candidate_decaps,
    refine_decaps,
)
from .context import LayoutContext
from .engine import LayoutResult, plan_layout
from .geometry import (
    FootprintGeometry,
    PadGeometry,
    load_footprint_geometries,
    load_footprint_geometry,
)
from .hierarchy import PlacementGroup, extract_groups
from .intent import (
    ChannelSlot,
    MatingIntent,
    PlacementIntent,
    PlacementIntentPlan,
    RepeatedChannelIntent,
    infer_placement_intents,
)
from .placer import derive_outline, derive_outline_from_circuit, place_parts
from .orientation import (
    OrientationResult,
    refine_candidate_orientations,
    refine_orientations,
)
from .power import (
    PowerChain,
    PowerCorridor,
    PowerNet,
    PowerRouteIntent,
    PowerRoutePlan,
    PowerTopology,
    identify_power_nets,
    infer_power_topology,
    plan_power_routes,
)
from .reader import read_board_outline, read_footprint_bboxes, read_placed_positions
from .refinement import (
    RefinementResult,
    refine_candidate_placement,
    refine_placement,
)
from .report import CandidateReport, NetExplanation, PartExplanation, PlacementReport
from .roles import PartRole, classify_part, classify_parts
from .routability import RoutabilityFeedback
from .scoring import LayoutScore, score_placement, score_placement_quick
from .spatial import SpatialGrid
from .validator import ValidationResult, find_kicad_cli, run_kicad_drc, validate
from .writer import PlacedPart, load_footprint_bboxes, parse_fp_lib_table, validate_footprints, write_kicad_pcb

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
from .fabspec import (
    FabCheckResult,
    FabSpec,
    FabViolation,
    OSHPARK_2L,
    fab_check,
    resolve_fab_spec,
    write_krt_fab_overrides,
)
from .copper_fill import (
    FilledBoard,
    NetCopper,
    fill_board,
    find_kicad_python,
    read_routed_copper,
)
from .copper_post import (
    ThermalViaPlan,
    find_exposed_pad,
    plan_thermal_vias,
)
from .current_widths import (
    ipc2221_width_mm,
    widths_from_currents,
)
from .layout_quality import (
    ADVISORY_CODES,
    BLOCKING_CODES,
    LayoutQualityResult,
    QualityIssue,
    layout_quality,
    routed_copper_issues,
)
from .context import LayoutContext
from .engine import (
    FP_LIB_DIRS_AUTO,
    LayoutResult,
    plan_layout,
    resolve_fp_lib_dirs,
)
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
from .power_constraints import (
    PowerConstraintSet,
    generate_power_constraints,
)
from .power_metrics import (
    LoopGeometry,
    PowerMetrics,
    StageMetrics,
    measure_power_layout,
)
from .power_roles import (
    CommutationLoop,
    PowerDevice,
    PowerStage,
    PowerStagePlan,
    classify_devices,
    classify_power_roles,
)
from .reader import read_board_outline, read_footprint_bboxes, read_placed_positions
from .refinement import (
    RefinementResult,
    refine_candidate_placement,
    refine_placement,
)
from .report import CandidateReport, NetExplanation, PartExplanation, PlacementReport
from .roles import (
    PartRole,
    classify_part,
    classify_parts,
    is_sim_only_part,
    sim_only_parts,
    strip_sim_only_parts,
)
from .routability import RoutabilityFeedback
from .krt import (
    KrtNotFoundError,
    check_board,
    evaluate_routability,
    find_krt,
    pour_planes,
    route_and_check,
)
from .power_copper import PowerCopperResult, emit_power_copper
from .scoring import LayoutScore, score_placement, score_placement_quick
from .spatial import SpatialGrid
from .validator import ValidationResult, find_kicad_cli, run_kicad_drc, validate
from .writer import PlacedPart, load_footprint_bboxes, parse_fp_lib_table, validate_footprints, write_kicad_pcb
from .metrics import (
    LayoutMetrics,
    discover_footprint_dir,
    discover_symbol_dir,
    evaluate_circuit,
    evaluate_circuit_dir,
    metrics_from_result,
    summary_table,
)

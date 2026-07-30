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
    GENERIC_4L,
    OSHPARK_2L,
    fab_check,
    resolve_fab_spec,
    resolve_spacing_column,
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
from .power_clearance import (
    max_required_clearance,
    net_clearance_deficits,
    net_clearance_map,
    plan_net_clearances,
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
from .power_escape import (
    ESCAPE_LANE_MM,
    ESCAPE_ROOM_FIELD,
    EscapeRoom,
    annulus_polygon,
    declared_escape_refs,
    escape_constraints,
    escape_far_constraints,
    lane_from_fab,
    mark_escape_room,
    measure_escape_room,
    measure_escape_rooms,
    resolve_escape_targets,
    resolve_lane_mm,
    write_keepout_polygons,
)
from .power_pads import (
    PAD_CLEARANCE_FIELD,
    PAD_CLEARANCE_FORMS,
    apply_pad_clearance,
    declared_pad_clearance_refs,
    mark_pad_clearance,
    pad_clearance_value,
    resolve_pad_clearance_targets,
)
from .power_zones import (
    ZonePlan,
    ZoneRegion,
    board_uses_name_nets,
    net_ids_from_board,
    plan_zone_regions,
    region_polygon,
    splice_zones,
    zone_sexprs,
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
from .ratnest import (
    Airwire,
    PadPoint,
    RatNest,
    TwistedPair,
    analyse_board,
    count_crossings,
    is_plane_net,
    mst_edges,
    net_airwires,
    read_pad_points,
    segments_cross,
    twisted_pairs,
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
from .power_copper import (
    PowerCopperResult,
    emit_power_copper,
    plan_loop_first_nets,
    plan_pinned_power_widths,
)
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

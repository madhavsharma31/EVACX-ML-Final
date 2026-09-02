"""Multi-photo reconstruction pipeline orchestrator.

Full workflow:
    Photos → YOLO (early) → Features → Overlap (with landmarks)
    → Graph → Poses → Fusion → Geometry → Floor Plan → Routing
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np

from app.reconstruction.image_features import extract_features_batch
from app.reconstruction.overlap_detector import detect_all_overlaps
from app.reconstruction.photo_graph import build_photo_graph
from app.reconstruction.camera_pose import build_pose_graph
from app.reconstruction.landmark_fusion import fuse_landmarks, FusedLandmark
from app.reconstruction.geometry_reconstruction import reconstruct_geometry
from app.reconstruction.floorplan_generator import generate_floor_plan
from app.models.detection_models import ReferenceMeasurement


def _build_occupants_from_detections(
    filtered_detections: list[dict],
    floor_plan,
) -> tuple[list[dict], dict[str, int]]:
    """
    Convert AI-detected people into evacuation occupants.

    Multiple overlapping photos may see the same person.
    To avoid double-counting, person detections are grouped by
    source photo and the photo with the highest number of
    detected people is used as the primary occupant view.

    Each detected person is then mapped to the nearest room
    or corridor in the generated floor plan.
    """

    # ---------------------------------------------------------
    # 1. Get person detections only
    # ---------------------------------------------------------
    people = [
        d for d in filtered_detections
        if d.get("type") == "person"
    ]

    rooms = [
        e for e in floor_plan.elements
        if e.type == "room"
    ]

    corridors = [
        e for e in floor_plan.elements
        if e.type == "corridor"
    ]

    locations = rooms + corridors

    occupants: list[dict] = []

    room_occupancy: dict[str, int] = {
        room.id: 0
        for room in rooms
    }

    if not people or not locations:
        return occupants, room_occupancy

    # ---------------------------------------------------------
    # 2. Group people by source photo
    # ---------------------------------------------------------
    photos: dict[str, list[dict]] = {}

    for person in people:
        photo_id = (
            person.get("image_id")
            or person.get("photo_index")
            or "default"
        )

        photo_id = str(photo_id)

        photos.setdefault(photo_id, []).append(person)

    # ---------------------------------------------------------
    # 3. Pick the photo containing the most people
    # ---------------------------------------------------------
    best_photo_id, best_photo_people = max(
        photos.items(),
        key=lambda item: len(item[1]),
    )

    logger.info(
        "[OCCUPANCY] Person detections by photo: %s",
        {photo_id: len(items) for photo_id, items in photos.items()},
    )

    logger.info(
        "[OCCUPANCY] Using photo '%s' with %d people "
        "to avoid cross-photo duplicates",
        best_photo_id,
        len(best_photo_people),
    )

    # ---------------------------------------------------------
    # 4. Convert selected detections into occupants
    # ---------------------------------------------------------
    for index, person in enumerate(best_photo_people, start=1):

        bbox = person.get("bbox", [])

        if len(bbox) != 4:
            continue

        try:
            x1, y1, x2, y2 = map(float, bbox)
        except (TypeError, ValueError):
            continue

        # -----------------------------------------------------
        # Recover original image dimensions
        # -----------------------------------------------------
        image_w = float(person.get("image_width") or 0)
        image_h = float(person.get("image_height") or 0)

        if image_w <= 0 or image_h <= 0:
            logger.warning(
                "[OCCUPANCY] Missing image dimensions for person %d",
                index,
            )
            continue

        # -----------------------------------------------------
        # Person center in normalized image coordinates
        # -----------------------------------------------------
        px = ((x1 + x2) / 2.0) / image_w
        py = ((y1 + y2) / 2.0) / image_h

        px = max(0.0, min(1.0, px))
        py = max(0.0, min(1.0, py))

        # -----------------------------------------------------
        # Convert to generated floor-plan coordinates
        # -----------------------------------------------------
        fx = px * floor_plan.width
        fy = py * floor_plan.height

        # -----------------------------------------------------
        # 5. Find room containing the person
        # -----------------------------------------------------
        containing_room = None

        for room in rooms:
            inside_x = (
                room.x <= fx <= room.x + room.width
            )

            inside_y = (
                room.y <= fy <= room.y + room.height
            )

            if inside_x and inside_y:
                containing_room = room
                break

        # -----------------------------------------------------
        # 6. If not inside a room, use nearest location
        # -----------------------------------------------------
        if containing_room is not None:
            location = containing_room

        else:

            def location_distance(element):
                cx = element.x + element.width / 2.0
                cy = element.y + element.height / 2.0

                return (
                    (cx - fx) ** 2 +
                    (cy - fy) ** 2
                ) ** 0.5

            location = min(
                locations,
                key=location_distance,
            )

        # -----------------------------------------------------
        # 7. Update room occupancy
        # -----------------------------------------------------
        if location.type == "room":
            room_occupancy[location.id] += 1

        # -----------------------------------------------------
        # 8. Create occupant
        # -----------------------------------------------------
        confidence = float(
            person.get("confidence", 0.0)
        )

        occupants.append({
            "id": f"person_{index}",
            "name": f"Person {index}",
            "location_id": location.id,
            "mobility": "normal",
            "confidence": round(confidence, 3),
            "source": "ai_person_detection",
            "x": round(fx, 2),
            "y": round(fy, 2),
        })

    # ---------------------------------------------------------
    # 9. Log final result
    # ---------------------------------------------------------
    logger.info(
        "[OCCUPANCY] Final occupants: %d",
        len(occupants),
    )

    logger.info(
        "[OCCUPANCY] Room occupancy: %s",
        room_occupancy,
    )

    return occupants, room_occupancy

logger = logging.getLogger(__name__)


class ReconstructionResult:
    """Complete output of the multi-photo reconstruction pipeline."""

    def __init__(self):
        self.success = False
        self.photos_received = 0
        self.num_groups = 0
        self.num_overlaps = 0
        self.num_poses = 0
        self.disconnected_photos: list[list[str]] = []
        self.raw_detections: list[dict] = []
        self.filtered_detections: list[dict] = []
        self.fused_landmarks: list[FusedLandmark] = []
        self.floor_plan = None
        self.overlap_details: list[dict] = []
        self.validation_issues: list[str] = []
        self.error: str | None = None
        self.occupants: list[dict] = []
        self.room_occupancy: dict[str, int] = {}

    def to_response(self) -> dict:
        if self.fused_landmarks:
            avg_conf = sum(l.confidence for l in self.fused_landmarks) / len(self.fused_landmarks)
        else:
            avg_conf = 0.0

        fp_dict = self.floor_plan.to_dict() if self.floor_plan else {
            "width": 1000, "height": 600, "units": "relative",
            "approximate": True, "confidence": 0, "elements": [],
        }

        # Count by type
        lm_by_type: dict[str, int] = {}
        for lm in self.fused_landmarks:
            lm_by_type[lm.type] = lm_by_type.get(lm.type, 0) + 1

        return {
            "success": self.success,
            "error": self.error,
            "reconstruction": {
                "photos_received": self.photos_received,
                "photo_groups": self.num_groups,
                "confirmed_overlaps": self.num_overlaps,
                "camera_poses_estimated": self.num_poses,
                "disconnected_photos": self.disconnected_photos,
                "landmark_counts": lm_by_type,
            },
            "occupants": self.occupants,
            "room_occupancy": self.room_occupancy,
            "detections": {
                "raw_count": len(self.raw_detections),
                "filtered_count": len(self.filtered_detections),
                "fused_landmarks": [
                    {
                        "id": lm.id,
                        "type": lm.type,
                        "observed_in": lm.observed_in,
                        "observation_count": lm.observation_count,
                        "confidence": lm.confidence,
                        "center_x": round(lm.avg_center_x, 4),
                        "center_y": round(lm.avg_center_y, 4),
                        "wheelchair_accessible": lm.wheelchair_accessible,
                    }
                    for lm in self.fused_landmarks
                ],
            },
            "floor_plan": fp_dict,
            "overlap_details": self.overlap_details,
            "validation": {
                "geometry_valid": len(self.validation_issues) == 0,
                "issues": self.validation_issues,
                "regions_requiring_review": [
                    lm.id for lm in self.fused_landmarks
                    if lm.confidence < 0.5
                ],
            },
            "digital_twin": {
                "nodes": [], "edges": [],
            },
            "metadata": {
                "overall_confidence": round(avg_conf, 2),
                "approximate": True,
                "warning": (
                    "This floor plan is AI-generated from photos. "
                    "Please review uncertain regions before relying on it."
                ),
            },
        }


def run_reconstruction(
    images: list[np.ndarray],
    filenames: list[str],
    reference: Optional[ReferenceMeasurement] = None,
    detector=None,
    floor: int = 1,
) -> ReconstructionResult:
    """Execute the full multi-photo reconstruction pipeline."""
    result = ReconstructionResult()
    result.photos_received = len(images)

    if not images:
        result.error = "No images provided."
        return result

    image_ids = [f"photo_{i:03d}" for i in range(len(images))]
    image_sizes = {}

    # =============================================
    # PHASE 1: Feature extraction
    # =============================================
    logger.info("[RECONSTRUCTION] Extracting features from %d photos", len(images))
    features = extract_features_batch(images, image_ids)

    for feat in features:
        image_sizes[feat.image_id] = (feat.width, feat.height)

    if len(features) < 2:
        logger.info("[RECONSTRUCTION] Single image — using YOLO-only pipeline")
        return _single_image_fallback(images[0], image_ids[0], detector, result)

    # =============================================
    # PHASE 2: Early YOLO detection (for landmark matching)
    # =============================================
    per_photo_landmarks: list[list[dict]] = []
    all_raw: list[dict] = []
    all_filtered: list[dict] = []

    if detector is not None:
        from app.ai.preprocessing import preprocess_detections, merge_detections
        from app.ai.architectural_detector import detect_architectural

        logger.info("[RECONSTRUCTION] Running early YOLO-World detection")
        for i, (img, img_id) in enumerate(zip(images, image_ids)):
            try:
                yolo_dets = detector.analyze(img)

                for d in yolo_dets:
                    d["image_id"] = img_id
                    d["photo_index"] = i
                    d["image_width"] = img.shape[1]
                    d["image_height"] = img.shape[0]
                all_raw.extend(yolo_dets)

                cv_dets = detect_architectural(img)
                for d in cv_dets:
                    d["image_id"] = img_id
                    d["photo_index"] = i
                all_raw.extend(cv_dets)

                merged = merge_detections(yolo_dets, cv_dets)
                filtered = preprocess_detections(merged)
                all_filtered.extend(filtered)
                per_photo_landmarks.append(filtered)

            except Exception as exc:
                logger.warning("[RECONSTRUCTION] YOLO failed for %s: %s", img_id, exc)
                per_photo_landmarks.append([])

        logger.info("[YOLO] Raw: %d, Filtered: %d", len(all_raw), len(all_filtered))

    result.raw_detections = all_raw
    result.filtered_detections = all_filtered

    # =============================================
    # PHASE 3: Overlap detection (with landmarks + images)
    # =============================================
    logger.info("[RECONSTRUCTION] Detecting pairwise overlaps")
    overlaps = detect_all_overlaps(
        features,
        images=images,
        all_landmarks=per_photo_landmarks,
        image_sizes=image_sizes,
    )
    confirmed = [o for o in overlaps if o.status == "confirmed"]
    result.num_overlaps = len(confirmed)

    # Store overlap details for frontend
    for o in overlaps:
        if o.status != "rejected":
            result.overlap_details.append({
                "image_a": o.image_a,
                "image_b": o.image_b,
                "combined_score": round(o.combined_score, 3),
                "status": o.status,
                "feature_score": round(o.feature_score, 3),
                "geometry_score": round(o.geometry_score, 3),
                "landmark_score": round(o.landmark_score, 3),
                "inlier_count": o.geometric_inliers,
            })

    # =============================================
    # PHASE 4: Photo connectivity graph
    # =============================================
    photo_graph = build_photo_graph(features, overlaps)
    result.num_groups = photo_graph.num_groups

    if photo_graph.num_groups > 1:
        result.disconnected_photos = photo_graph.groups[1:]

    # =============================================
    # PHASE 5: Camera pose estimation
    # =============================================
    logger.info("[RECONSTRUCTION] Estimating camera poses")
    pose_graph = build_pose_graph(features, overlaps)
    result.num_poses = pose_graph.estimated_count

    # =============================================
    # PHASE 6: Multi-view landmark fusion
    # =============================================
    logger.info("[RECONSTRUCTION] Fusing multi-view observations")
    fused = fuse_landmarks(all_filtered, image_sizes, photo_graph, pose_graph)
    result.fused_landmarks = fused
    logger.info("[FUSION] %d unique physical landmarks", len(fused))

    # =============================================
    # PHASE 7: Geometry reconstruction + floor plan
    # =============================================
    logger.info("[RECONSTRUCTION] Reconstructing geometry")
    geometry = reconstruct_geometry(fused, pose_graph)

    logger.info("[RECONSTRUCTION] Generating floor plan")
    floor_plan = generate_floor_plan(geometry, fused)
    result.floor_plan = floor_plan

    # =============================================
    # PHASE 7.5: Build occupants from AI detections
    # =============================================
    logger.info("[RECONSTRUCTION] Mapping detected people to rooms")

    occupants, room_occupancy = _build_occupants_from_detections(
    all_filtered,
    floor_plan,
   )

    result.occupants = occupants
    result.room_occupancy = room_occupancy

    logger.info(
        "[RECONSTRUCTION] Detected %d occupants across %d rooms",
        len(occupants),
        len(room_occupancy),
    )

    # =============================================
    # PHASE 8: Validation
    # =============================================
    issues = _validate_floor_plan(floor_plan, fused)
    result.validation_issues = issues

    result.success = True

    logger.info(
        "[RECONSTRUCTION] Complete: %d photos, %d overlaps, "
        "%d landmarks, %d elements, confidence=%.2f",
        result.photos_received, result.num_overlaps,
        len(fused), len(floor_plan.elements),
        floor_plan.confidence,
    )

    return result


def _single_image_fallback(
    image: np.ndarray, image_id: str, detector,
    result: ReconstructionResult,
) -> ReconstructionResult:
    """Fallback for single image."""
    from app.ai.preprocessing import preprocess_detections, merge_detections
    from app.ai.architectural_detector import detect_architectural
    from app.ai.landmarks import generate_landmarks
    from app.ai.floorplan_single import generate_floor_plan as single_generate
    from app.reconstruction.floorplan_generator import GeneratedFloorPlan, FloorPlanElement

    image_sizes = {image_id: (image.shape[1], image.shape[0])}

    if detector is not None:
        yolo_dets = detector.analyze(image)

        for d in yolo_dets:
            d["image_id"] = image_id
            d["image_width"] = image.shape[1]
            d["image_height"] = image.shape[0]
        cv_dets = detect_architectural(image)
        for d in cv_dets:
            d["image_id"] = image_id
        merged = merge_detections(yolo_dets, cv_dets)
        filtered = preprocess_detections(merged)
        result.raw_detections = yolo_dets + cv_dets
        result.filtered_detections = filtered
    else:
        filtered = []

    landmarks_dicts = generate_landmarks(filtered, image_sizes[image_id][0], image_sizes[image_id][1])

    fused = []
    for lm in landmarks_dicts:
        fused.append(FusedLandmark(
            type=lm["type"], observed_in=[image_id], observation_count=1,
            confidence=lm["confidence"], avg_center_x=lm["center_x"],
            avg_center_y=lm["center_y"], best_bbox=lm.get("bbox", []),
            wheelchair_accessible=lm.get("wheelchair_accessible", True),
        ))
    result.fused_landmarks = fused

    fp = single_generate(landmarks_dicts)
    gfp = GeneratedFloorPlan(width=fp["width"], height=fp["height"],
                             units=fp["units"], approximate=fp["approximate"], confidence=0.4)
    for el in fp["elements"]:
        gfp.elements.append(FloorPlanElement(
            id=el["id"], type=el["type"], x=el["x"], y=el["y"],
            width=el["width"], height=el["height"],
            confidence=el.get("confidence", 0.3),
            wheelchair_accessible=el.get("wheelchair_accessible", True),
            estimated=el.get("estimated", False),
            connected_door_id=el.get("connected_door_id", ""),
        ))
    result.floor_plan = gfp
    result.success = True
    return result


def _validate_floor_plan(fp, landmarks) -> list[str]:
    """Basic validation."""
    issues: list[str] = []
    ids = [e.id for e in fp.elements]
    if len(ids) != len(set(ids)):
        issues.append("Duplicate element IDs found")
    if not any(e.type == "corridor" for e in fp.elements):
        issues.append("No corridor element generated")
    if not any(e.type == "exit" for e in fp.elements):
        issues.append("No exit element generated — routing may fail")
    for i in range(len(fp.elements)):
        for j in range(i + 1, len(fp.elements)):
            ei, ej = fp.elements[i], fp.elements[j]
            if ei.type == "room" and ej.type == "room":
                ox = max(0, min(ei.x + ei.width, ej.x + ej.width) - max(ei.x, ej.x))
                oy = max(0, min(ei.y + ei.height, ej.y + ej.height) - max(ei.y, ej.y))
                if ox > 0 and oy > 0:
                    issues.append(f"Room overlap: {ei.id} and {ej.id}")
    low_conf = [lm for lm in landmarks if lm.confidence < 0.4]
    if low_conf:
        issues.append(f"{len(low_conf)} landmarks have low confidence (< 0.4)")
    return issues

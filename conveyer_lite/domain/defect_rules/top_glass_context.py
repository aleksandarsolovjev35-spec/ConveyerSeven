import cv2
import numpy as np

from domain.defect_rules.rule_top_contacts import TopContactsRule
from domain.defect_rules.top_geometry import (
    infer_shape,
    largest_valid_mask,
    mask_points,
    overlap_mask,
    rasterize_mask,
)


EXPECTED_PINS = 14
EXPECTED_CONTACTS = 14


def build_top_glass_context(detections, confidence):
    glasses = _filter(detections, "glass", confidence["glass"])
    if not glasses:
        return {
            "valid": True,
            "has_glass": False,
            "glasses": [],
            "reason": None,
        }
    invalid_glasses = [
        index
        for index, glass in enumerate(glasses, start=1)
        if mask_points(glass) is None
    ]
    if invalid_glasses:
        return _invalid(
            "missing_glass_mask",
            glasses,
            invalid_glass_indices=invalid_glasses,
        )

    platforms = _filter(detections, "platform", confidence["platform"])
    contacts = _filter(detections, "contacts", confidence["contacts"])
    cases = _filter(detections, "case", confidence["case"])
    centrals = _filter(detections, "case_central", confidence["central"])
    pins = _filter(detections, "pin", confidence["pin"])

    platform = largest_valid_mask(platforms)
    if platform is None:
        return _invalid("no_valid_platform", glasses)
    platform_bbox = platform.get("bbox")
    if not _valid_bbox(platform_bbox):
        return _invalid("invalid_platform_bbox", glasses)

    selected_contacts, contact_error = _select_contact_references(
        contacts,
        platform_bbox,
    )
    if contact_error is not None:
        return _invalid(
            contact_error["reason"],
            glasses,
            **{
                key: value
                for key, value in contact_error.items()
                if key != "reason"
            },
        )

    if len(pins) != EXPECTED_PINS:
        return _invalid(
            f"wrong_pin_count: {len(pins)}/14",
            glasses,
            pins_found=len(pins),
        )
    invalid_pins = [
        index
        for index, pin in enumerate(pins, start=1)
        if mask_points(pin) is None
    ]
    if invalid_pins:
        return _invalid(
            "missing_pin_mask",
            glasses,
            invalid_pin_indices=invalid_pins,
        )
    if len(cases) != 1 or mask_points(cases[0]) is None:
        return _invalid(
            f"invalid_case_count: {len(cases)}/1",
            glasses,
            case_found=len(cases),
        )
    if len(centrals) != 1 or mask_points(centrals[0]) is None:
        return _invalid(
            f"invalid_case_central_count: {len(centrals)}/1",
            glasses,
            case_central_found=len(centrals),
        )

    case = cases[0]
    central = centrals[0]
    shape = infer_shape(
        glasses,
        [platform],
        selected_contacts,
        [case],
        [central],
        pins,
    )
    platform_raster = rasterize_mask(platform, shape)
    contact_rasters = [
        rasterize_mask(contact, shape) for contact in selected_contacts
    ]
    pin_rasters = [rasterize_mask(pin, shape) for pin in pins]
    case_raster = rasterize_mask(case, shape)
    central_raster = rasterize_mask(central, shape)
    case_central_overlap = overlap_mask(case_raster, central_raster)
    if (
        case_central_overlap is None
        or int(np.count_nonzero(case_central_overlap)) <= 0
    ):
        return _invalid("case_central_not_inside_case", glasses)
    ring_raster = case_raster.copy()
    ring_raster[central_raster > 0] = 0
    if int(np.count_nonzero(ring_raster)) <= 0:
        return _invalid("empty_case_ring", glasses)

    contact_union = np.zeros(shape, dtype=np.uint8)
    for raster in contact_rasters:
        contact_union = cv2.bitwise_or(contact_union, raster)
    pin_union = np.zeros(shape, dtype=np.uint8)
    for raster in pin_rasters:
        pin_union = cv2.bitwise_or(pin_union, raster)

    return {
        "valid": True,
        "has_glass": True,
        "reason": None,
        "shape": shape,
        "glasses": glasses,
        "glass_rasters": [rasterize_mask(glass, shape) for glass in glasses],
        "platform": platform,
        "platform_raster": platform_raster,
        "contacts": selected_contacts,
        "contact_rasters": contact_rasters,
        "contact_union": contact_union,
        "pins": pins,
        "pin_rasters": pin_rasters,
        "pin_union": pin_union,
        "case": case,
        "central": central,
        "ring_raster": ring_raster,
        "ignored_platforms": max(0, len(platforms)-1),
        "ignored_contacts": max(0, len(contacts)-len(selected_contacts)),
    }


def _select_contact_references(contacts, platform_bbox):
    valid = [contact for contact in contacts if mask_points(contact) is not None]
    if len(valid) < EXPECTED_CONTACTS:
        return None, {
            "reason": "insufficient_valid_contacts",
            "valid_contacts": len(valid),
        }
    groups, _unassigned = TopContactsRule._group_candidates(valid, platform_bbox)
    counts = {
        group: len(groups[group]) for group in TopContactsRule.EXPECTED_GROUPS
    }
    insufficient = [
        group
        for group, expected in TopContactsRule.EXPECTED_GROUPS.items()
        if len(groups[group]) < expected
    ]
    if insufficient:
        return None, {
            "reason": "invalid_contact_layout",
            "contact_group_counts": counts,
        }
    selected = []
    for group in ("L", "R", "T", "B"):
        group_selected, _extras = TopContactsRule._select_consistent_group(
            groups[group],
            TopContactsRule.EXPECTED_GROUPS[group],
            group,
            platform_bbox,
        )
        selected.extend(TopContactsRule._sort_group(group_selected, group))
    return selected, None


def _filter(detections, class_name, confidence):
    return [
        detection for detection in detections
        if detection.get("class") == class_name
        and float(detection.get("confidence", 0.0)) >= confidence
    ]


def _invalid(reason, glasses, **details):
    return {
        "valid": False,
        "has_glass": True,
        "reason": reason,
        "glasses": glasses,
        **details,
    }


def _valid_bbox(bbox):
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        return False
    values = np.asarray(bbox, dtype=np.float64)
    return bool(
        np.isfinite(values).all()
        and values[2] > values[0]
        and values[3] > values[1]
    )

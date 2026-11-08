"""Geometry cleansing stage for the transformation pipeline."""
from __future__ import annotations

from ...model import MoosasModel
from ..geometry.cleanse import (
    cleanseCoplannerLine,
    cleanseDuplicatedWall,
    cleanseInvalidFace,
    cleanseInvalidWall,
    cleanseOverlapFace,
    cleanseOverlapWall,
)
from ..geometry.element import MoosasElement


def cleanse_coplanar_lines(model: MoosasModel) -> MoosasModel:
    """Remove redundant coplanar lines from model elements."""
    return cleanseCoplannerLine(model)


def cleanse_invalid_elements(model: MoosasModel) -> MoosasModel:
    """Remove invalid walls and faces."""
    model = cleanseInvalidWall(model)
    return cleanseInvalidFace(model)


def cleanse_overlapping_elements(model: MoosasModel) -> MoosasModel:
    """Clean overlapping walls and faces."""
    model = cleanseOverlapWall(model)
    return cleanseOverlapFace(model)


def cleanse_duplicate_walls(model: MoosasModel) -> MoosasModel:
    """Remove duplicate walls from the model."""
    return cleanseDuplicatedWall(model)


def attach_explicit_shading(model: MoosasModel) -> MoosasModel:
    """Create explicit shading elements after all wall cleansing is complete."""
    existing_shading = {element.faceId for element in model.shadingList}
    model.shadingList.extend(
        MoosasElement(model, geometry)
        for geometry in model.geometryList
        if geometry.category == -1 and geometry.faceId not in existing_shading
    )
    return model

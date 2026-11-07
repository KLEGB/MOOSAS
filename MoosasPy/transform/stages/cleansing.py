"""Geometry cleansing stage for the transformation pipeline."""
from __future__ import annotations

from ...model import MoosasModel
from ...utils import np
from ...utils.tools import searchBy
from ..geometry.cleanse import (
    cleanseCoplannerLine,
    cleanseDuplicatedWall,
    cleanseInvalidFace,
    cleanseInvalidWall,
    cleanseOverlapFace,
    cleanseOverlapWall,
)
from ..geometry.element import MoosasElement
from .glazing import attach_glazing_to_faces


def cleanse_model(
    model: MoosasModel,
    *,
    solve_duplicated: bool,
    solve_redundant: bool,
    solve_overlap: bool,
) -> tuple[MoosasModel, list[int]]:
    """Clean classified geometry and attach glazing before space generation."""
    if solve_redundant:
        model = cleanseCoplannerLine(model)

    model = cleanseInvalidWall(model)
    model = cleanseInvalidFace(model)
    model = attach_glazing_to_faces(model)

    wall_counts = [len(searchBy("level", level, model.wallList)) for level in model.levelList]
    if solve_redundant:
        model = cleanseCoplannerLine(model)
    if solve_duplicated:
        model = cleanse_duplicate_walls(model)

    model = cleanseInvalidWall(model)
    model = cleanseInvalidFace(model)

    if solve_overlap:
        model = cleanseOverlapWall(model)
        model = cleanseOverlapFace(model)
    return model, wall_counts


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

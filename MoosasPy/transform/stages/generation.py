"""Space-boundary generation stages for the transformation pipeline."""
from __future__ import annotations

from ...model import MoosasModel
from ...utils import searchBy
from ..geometry.contour import _documentBoundary, closed_contour_calculation
from ...model.boundary import MoosasEdge
from ..geometry.viewFactor import viewFactorTopology


def BTGSpaceGeneration(model: MoosasModel) -> MoosasModel:
    """Generate boundaries by selecting walls around horizontal faces."""
    valid_boundaries = []
    for level in model.levelList:
        faces = searchBy("level", level, model.faceList, asObject=True)
        walls = list(searchBy("level", level, model.wallList, asObject=True))
        if walls:
            for face in faces:
                valid_boundaries.append(MoosasEdge.selectWall(face.force_2d(), walls))
    model.boundaryList = [edge.wall for edge in valid_boundaries]
    return model


def CCRSpaceGeneration(model: MoosasModel) -> MoosasModel:
    """Generate closed contours for each building level."""
    for level in model.levelList:
        model = closed_contour_calculation(model, level)
    return model


def VFGSpaceGeneration(model: MoosasModel) -> MoosasModel:
    """Generate boundaries from wall view-factor topology."""
    boundaries = []
    for level in model.levelList:
        elements = searchBy("level", level, model.wallList, asObject=True)
        new_boundaries = viewFactorTopology(model, elements, vfNumber=12)
        print(f"\rTOPOLOGY: in {level}: find {len(new_boundaries)} boundaries")
        boundaries += new_boundaries
    return _documentBoundary(boundaries, model)

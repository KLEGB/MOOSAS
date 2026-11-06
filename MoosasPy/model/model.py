"""Connection to most of the functions in moosas+.
It records the space data we need in the analysis.

we split the MoosasModel definition from geometry.element to avoid circular import
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ..transform.geometry.element import *
from ..transform.geometry.geos import faceNormal

if TYPE_CHECKING:
    from .io import SaveResult

"""you can apply the inch to meter translation here"""
# from .utils.constant import geom
# INCH_METER_MULTIPLIER = geom.INCH_METER_MULTIPLIER
# INCH_METER_MULTIPLIER_SQR = geom.INCH_METER_MULTIPLIER_SQR
INCH_METER_MULTIPLIER = 1
INCH_METER_MULTIPLIER_SQR = 1


class MoosasModel(MoosasContainer):
    """Domain state for a building geometry and its analysis configuration.

    Resource loading is deliberately handled by ``MoosasPy.model.building_templates``.
    """

    def __init__(self):
        """
        Initialize the MoosasModel with default lists and assign types to these lists.
        
        Parameters
        ----------
        self : object
            The instance of the MoosasModel class being initialized.
        
        Returns
        ------
        None
            This constructor does not return any value.
        """
        """initialize the MoosasModel with default list, and apply type to these list"""
        super(MoosasModel, self).__init__()

        self.buildingTemplate = {}
        self.schedule = {}
        self.metadata = {}
        # Persistent energy boundary/parameter overrides, independent of templates.
        self.energy_settings = {}

    @classmethod
    def load(cls, file_path: str | Path) -> MoosasModel:
        """Load a model from a supported semantic model format."""
        from .io import load_model

        return load_model(file_path)

    def save(self, file_path: str | Path, **format_options) -> SaveResult:
        """Save by target suffix, with options specific to that format."""
        from .io import save_model

        return save_model(self, file_path, **format_options)

    def move_opaque(self, target, distance: float) -> MoosasModel:
        from .operation import move_opaque

        return move_opaque(self, target, distance)

    def move_glazing(self, target, displacement) -> MoosasModel:
        from .operation import move_glazing

        return move_glazing(self, target, displacement)

    def autoDescribe(self):
        """automatically generate description for each space and element in the model, based on their geometry and settings.
        the description will be stored in the 'description' attribute of each space and element, and can be used in the analysis or output.
        """
        for spc in self.spaceList:
            if spc.description == "":
                walls = spc.getAllFaces(to_dict=True)['MoosasWall']
                description = f"This is a space named{spc.id} located in the level {spc.level},"
                description += f" with an area of {spc.area} m2 and a height of {spc.height} m. "
                description += f" average Window to wall ratio of this space is {np.round(np.mean([w.wwr for w in walls]), 2)}. "
                spc.description = description
            
            for face in spc.getAllFaces():
                if face.description == "":
                    if isinstance(face, MoosasWall):
                        face.description = f"This is a wall with an area of {face.area} m2, and a U-value of {face.U_Value} W/m2K. "
                    elif isinstance(face, MoosasFace):
                        face.description = f"This is a floor with an area of {face.area} m2, and a U-value of {face.U_Value} W/m2K. "
                    elif isinstance(face, MoosasGlazing):
                        face.description = f"This is a glazing with an area of {face.area} m2, and a U-value of {face.U_Value} W/m2K. "
                    elif isinstance(face, MoosasSkylight):
                        face.description = f"This is a skylight with an area of {face.area} m2, and a U-value of {face.U_Value} W/m2K. "
                    else:
                        face.description = f"This is a face with an area of {face.area} m2. "
                    
                    if len(face.space)==1:
                        face.description += f" It belongs to space {face.space[0]}. "
                    elif len(face.space)>1:
                        face.description += f" It belongs to spaces {', '.join(face.space)}. "
                    if face.isOuter:
                        face.description += " It is an external face. "
                    else:
                        face.description += " It is an internal face. "

    def summary(self,wall_count=None):
        """
        Prints a formatted summary of building elements by level.
        
        Parameters
        ----------
        self : object
            The instance of the class containing the lists of building elements.
            Must have attributes: `levelList`, `wallList`, `glazingList`, `skylightList`,
            `faceList`, and `spaceList`.
        wall_count : list of int, optional
            A list specifying the previous count of walls per level for change tracking.
            If provided, differences in wall counts are displayed in parentheses.
        
        Returns
        -------
        None
            This function does not return a value. It prints the summary directly to stdout.
        """
        print('LEVEL\t\tWALL\t\tGLS\t\tSKY\t\tFACE\t\tSPACE\t\tAREA')

        for i, bld_level in enumerate(self.levelList):
            print(f"%.2f" % bld_level, end='')
            if wall_count:
                print(
                    f"\t\t{len(searchBy('level', bld_level, self.wallList))}({len(searchBy('level', bld_level, self.wallList)) - wall_count[i]})",
                    end='')
            else:
                print(
                    f"\t\t{len(searchBy('level', bld_level, self.wallList))}",
                    end='')
            print(f"\t\t{len(searchBy('level', bld_level, self.glazingList))}", end='')
            print(f"\t\t{len(searchBy('level', bld_level, self.skylightList))}", end='')
            print(f"\t\t{len(searchBy('level', bld_level, self.faceList))}", end='')
            print(f"\t\t{len(searchBy('level', bld_level, self.spaceList))}({len(searchBy('level', bld_level, self.voidList))})", end='')
            print(
                f"\t\t{np.round(np.sum([self.spaceList[i].area for i in searchBy('level', bld_level, self.spaceList)]), 1)}({np.round(np.sum([self.voidList[i].area for i in searchBy('level', bld_level, self.voidList)]), 1)})\n",
                end='')

        if wall_count:
            print(
                f"    \t\t{len(self.wallList)}({len(self.wallList) - sum(wall_count)})"
                f"\t\t{len(self.glazingList)}"
                f"\t\t{len(self.skylightList)}"
                f"\t\t{len(self.faceList)}"
                f"\t\t{len(self.spaceList)}"
                f"\t\t{np.round(np.sum([s.area for s in self.spaceList]), 1)}({np.round(np.sum([s.area for s in self.voidList]), 1)})")

        else:
            print(
                f"    \t\t{len(self.wallList)}"
                f"\t\t{len(self.glazingList)}"
                f"\t\t{len(self.skylightList)}"
                f"\t\t{len(self.faceList)}"
                f"\t\t{len(self.spaceList)}"
                f"\t\t{np.round(np.sum([s.area for s in self.spaceList]), 1)}({np.round(np.sum([s.area for s in self.voidList]), 1)})")

        # for bld_level in self.levelList:
        #     spaceList = searchBy("level", bld_level,self.spaceList,asObject=True)
        #     spaceType = [s.spaceType for s in spaceList]
        #     Corridor = [s for s,t in zip(spaceList,spaceType) if t == 'Corridor']
        #     privateSpace = [s for s, t in zip(spaceList, spaceType) if t == 'privateSpace']
        #     MainSpace = [s for s, t in zip(spaceList, spaceType) if t == 'MainSpace']
        #     print(f"level: {bld_level}, "
        #           f"Corridor {len(Corridor)} area: {np.sum([s.area for s in Corridor])},"
        #           f"privateSpace {len(privateSpace)} area: {np.sum([s.area for s in privateSpace])},"
        #           f"MainSpace {len(MainSpace)} area: {np.sum([s.area for s in MainSpace])}")

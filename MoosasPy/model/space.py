"""Building spaces, their enclosure topology, and zone settings."""
from __future__ import annotations

from typing import TYPE_CHECKING

from .boundary import MoosasEdge, MoosasFloor
from ..transform.geometry.element import (
    INCH_METER_MULTIPLIER,
    INCH_METER_MULTIPLIER_SQR,
    MoosasElement,
    MoosasFace,
    MoosasGlazing,
    MoosasSkylight,
    MoosasWall,
    _getElement,
)
from ..transform.geometry.convexify import convexify_shape
from ..transform.geometry.geos import bBox
from ..utils import encodeParams, np, shapely
from ..utils.constant import geom

if TYPE_CHECKING:
    from .container import MoosasContainer


class MoosasSpace(object):
    """define a space with topology and related data.
    it can be a void if floor or ceiling is None or area of floor/ceiling < area of edge

    """
    __slots__ = ['floor', 'edge', 'ceiling', '__void', '__id','__uniqueId', '__neighbor', 'internalMass', 'settings',
                 'description', 'space_type', 'conditioned', 'explicit_settings']

    def __init__(self, _floor: MoosasFloor | None, _edge: MoosasEdge, _ceiling: MoosasFloor | None,
                 void: list[MoosasSpace] = None, Uid: str = None, space_type: str = "room",
                 conditioned: bool = True):
        """
        Initialize a new instance with floor, edge, ceiling, and optional void spaces.
        
        Parameters
        ----------
        _floor : MoosasFloor or None
            The floor object associated with the space, or None if not present.
        _edge : MoosasEdge
            The edge object defining the boundary and internal mass of the space.
        _ceiling : MoosasFloor or None
            The ceiling object associated with the space, or None if not present.
        void : list of MoosasSpace, optional
            A list of void spaces within the zone. Defaults to an empty list if None.
        
        Returns
        -------
        None
        """
        self.floor: MoosasFloor | None = _floor
        self.edge: MoosasEdge = _edge
        self.ceiling: MoosasFloor | None = _ceiling
        self.description = ""
        self.space_type = space_type
        self.conditioned = bool(conditioned)
        self.explicit_settings = []

        self.__neighbor = {}
        self.internalMass: list[MoosasElement] = _edge.internalMass
        self.__void: list[MoosasSpace] = [] if void is None else void
        self.__id: str = '' if Uid is None else Uid
        self.__uniqueId: bool = False if Uid is None else True
        self.regenerateId()

        # Thermal Settings
        self.settings = {
            "zone_name": self.id,

            "zone_summerrad": None,  # summer radiant heat units:kwh
            "zone_winterrad": None,  # winter radiant heat units:kwh

            "zone_template": None
        }


    @classmethod
    def fromDict(cls, spaceDict, model: MoosasContainer):
        """
        Construct a Space object from a dictionary representation.
        
        Parameters
        ----------
        spaceDict : dict
            Dictionary containing space elements such as 'edge', 'ceiling', 'floor', 
            'internalMass', and 'void'.
        model : MoosasContainer
            Model container providing context for constructing associated objects.
        
        Returns
        -------
        Space
            A new Space instance constructed from the provided dictionary and model.
        """
        edge = _getElement('edge', dictionary=spaceDict)[0]
        ceiling = _getElement('ceiling', dictionary=spaceDict, strict=False)[0]
        floor = _getElement('floor', dictionary=spaceDict, strict=False)[0]
        Uid = _getElement('id', dictionary=spaceDict, strict=False)[0]
        space_type = _getElement('space_type', dictionary=spaceDict, strict=False)[0]
        conditioned_value = _getElement('conditioned', dictionary=spaceDict, strict=False)[0]
        conditioned = True if conditioned_value is None else str(conditioned_value).strip().lower() == "true"

        internalMass = _getElement('internalMass', dictionary=spaceDict, strict=False)
        void = _getElement('void', dictionary=spaceDict, strict=False)

        if ceiling:
            ceiling = MoosasFloor.fromDict(floor, model)
        if edge:
            edge = MoosasEdge.fromDict(edge, model)
        if floor:
            floor = MoosasFloor.fromDict(ceiling, model)
        
        if Uid:
            space = cls(
                floor, edge, ceiling, Uid=Uid, space_type=space_type or "room", conditioned=conditioned
            )
        else:
            space = cls(floor, edge, ceiling, space_type=space_type or "room", conditioned=conditioned)

        if internalMass[0]:
            for _intWall in internalMass:
                space.addInternalMass(MoosasWall.fromDict(_intWall, model))

        if void[0]:
            for _void in void:
                space.add_void(cls.fromDict(_void, model))
        return space

    @property
    def neighbor(self) -> dict:
        """
        Dictionary of neighbors associated with the object.
        
        Parameters
        ----------
        self : object
            The instance of the class containing the neighbor property.
        
        Returns
        -------
        dict
            A dictionary representing the neighbors.
        """
        return self.__neighbor

    @property
    def void(self) -> list[MoosasSpace]:
        """
        List of MoosasSpace objects representing the void.
        
        Parameters
        ----------
        self : object
            The instance of the class containing the void property.
        
        Returns
        -------
        list[MoosasSpace]
            A list of MoosasSpace objects stored in the private __void attribute.
        """
        return self.__void

    @property
    def parent(self):
        """
        Parent property of the edge.
        
        Returns the parent node associated with the edge of this instance.
        
        Returns
        -------
        object
            The parent node of the edge.
        """
        return self.edge.parent

    @property
    def id(self) -> str:
        """
        Return the ID of the object as a string.
        
        Returns
        -------
        str
            The private attribute `__id` representing the object's ID.
        """
        return self.__id

    @property
    def area(self) -> float:
        """
        Compute the effective area of the object, accounting for any voids.
        
        Parameters
        ----------
        self : object
            The instance of the class containing the `edge` and `void` attributes.
            It is expected to have an `edge` attribute with an `area` property,
            and a `void` attribute which is a collection of objects each having an `area` property.
        
        Returns
        -------
        float
            The effective area, calculated as the area of the edge minus the sum of the areas of all voids.
        """
        area = self.edge.area
        if len(self.void) > 0:
            for _void in self.void:
                area -= _void.area
        return area

    @property
    def level(self) -> float:
        """
        Level of the edge.
        
        Parameters
        ----------
        self : object
            The instance of the class containing the `edge` attribute.
        
        Returns
        -------
        float
            The level value from the associated edge.
        """
        return self.edge.level

    @property
    def topLevel(self) -> float:
        """
        Top-level elevation of the structure, depending on whether it is void or not.
        
        Returns the top-level elevation based on the object's state: if the object is void,
        returns the toplevel from the edge; otherwise, returns the ceiling level.
        
        Parameters
        ----------
        self : object
            The instance of the class containing this property. Assumes the presence
            of `is_void()`, `edge.toplevel`, and `ceiling.level` attributes/methods.
        
        Returns
        -------
        float
            The top-level elevation value, either from `edge.toplevel` (if void) or `ceiling.level`.
        """
        explicit=getattr(self,'settings',{}).get('geometry_top_elevation_m')
        if explicit is not None:return float(explicit)
        if self.space_type=='attic' and self.ceiling:
            return max(float(np.max(shapely.get_coordinates(f.face,include_z=True)[:,2])) for f in self.ceiling.face)
        if self.is_void():
            return self.edge.toplevel
        else:
            return self.ceiling.level

    @property
    def height(self) -> float:
        """
        Height of the object calculated based on edge, ceiling, and floor levels and offsets.
        
        Parameters
        ----------
        self : object
            The instance of the class containing the height property. It is expected to have
            methods `is_void()` and attributes `edge`, `ceiling`, and `floor`. The `edge`
            attribute should have `toplevel` and `level` properties. The `ceiling` and `floor`
            attributes should each have `level` and `offset` properties.
        
        Returns
        -------
        float
            The computed height. If the instance is void (determined by `is_void()`), returns
            the difference between `toplevel` and `level` of the edge. Otherwise, returns the
            difference between the adjusted ceiling level and the adjusted floor level.
        """
        volume=getattr(self,'settings',{}).get('geometry_volume_m3')
        if volume is not None:
            volume=float(volume)
            if not np.isfinite(volume) or volume<=0 or self.area<=0:raise ValueError('Invalid explicit physical volume')
            return volume/self.area
        if self.is_void():
            return self.edge.toplevel - self.edge.level
        else:
            if self.space_type == "attic":
                # Roof facets can carry different reference levels. Use their
                # actual elevations so RDF interface order cannot change the
                # reported attic height during a save/reload cycle.
                ceiling_z = np.mean([face.level + face.offset for face in self.ceiling.face])
                floor_z = np.mean([face.level + face.offset for face in self.floor.face])
                return float(ceiling_z - floor_z)
            return self.ceiling.level + self.ceiling.offset - self.floor.level - self.floor.offset

    @property
    def spaceType(self) -> str:
        """
        Determine the type of space based on geometric properties of 2D faces.
        
        Parameters
        ----------
        self : object
            The instance of the class containing the `force_2d` method and geometric data.
            It is assumed that `self` has a method `force_2d()` which returns a 2D geometric representation,
            and that `shapely` and `bBox` utilities are available for area and bounding box computations.
        
        Returns
        -------
        str
            The classified space type, one of 'Corridor', 'MainSpace', or 'privateSpace',
            based on area, aspect ratio, and dimensional thresholds of decomposed convex faces.
        """
        """Select one of the following types of the space:
        Corridor: most of the pieces are narrow and long
        MainSpace: the main space in the building, like the living room or the hall
        privateSpace: the small space in the building, provided for a few amount of persons
        """
        convexFaces, _ = convexify_shape(self.force_2d())
        narrowPart, mainPart = [], []
        for face in convexFaces:
            if shapely.area(face) > 9.0:
                boxDict = bBox(face)
                xSize = boxDict['x-domain'][1] - boxDict['x-domain'][0]
                ySize = boxDict['y-domain'][1] - boxDict['y-domain'][0]
                if min(xSize, ySize) < 2.5:
                    narrowPart.append(face)
                elif max(xSize, ySize) / min(xSize, ySize) > 3 and min(xSize, ySize) < 5:
                    narrowPart.append(face)
                else:
                    mainPart.append(face)
        if len(mainPart) == 0:
            return 'Corridor'
        else:
            if np.sum([shapely.area(p) for p in mainPart]) / 18 < 3.0:
                """space less for 3 person"""
                return 'privateSpace'
            else:
                return 'MainSpace'

    def regenerateId(self) -> str:
        """calculate the id for the space
        the id comes from 7 params,each params space two indent('0'to'9' & 'a'to'j')
        so the id will be encoded like this:
            0x 1a 2b 3c 4d 5e 6f 7g

        Returns:
            str: self.id
        """
        originalId = self.id
        if not self.__uniqueId:
            walls = self.getAllFaces(to_dict=True)['MoosasWall']
            params = [self.area, self.height * 10, self.level * 10, len(walls)]
            params += list([np.sum([w.wwr * 100 for w in walls])])
            params += list(self.edge.getWeightCenter() * 10)
            self.__id = encodeParams(*params)
        # Record self.id to all MoosasGeometries
        is_void = self.is_void()
        for moface in self.getAllFaces(to_dict=False):
            if originalId != "" and originalId in moface.space:
                moface.space.remove(originalId)
            if not is_void:
                if not self.id in moface.space:
                    moface.space.append(self.id)
        return self.__id

    def add_void(self, void: MoosasSpace) -> None:
        """
        Add a void space to the collection of voids and update space attributes in all faces.
        
        Parameters
        ----------
        void : MoosasSpace
            The void space object to be added to the internal void list.
        
        Returns
        -------
        None
        """
        """add void to self.__void, and change the space attribute in self.getAllFaces()
        """
        self.__void.append(void)
        # Record self.id to all MoosasGeometries
        self.regenerateId()

    def force_2d(self, top=False) -> shapely.Geometry:
        """
        Project the geometry to 2D and return a 2D polygon.
        
        Parameters
        ----------
        top : bool, optional
            If True, project to the top plane; otherwise, use default 2D projection.
            Default is False.
        
        Returns
        -------
        shapely.Geometry
            A 2D polygon geometry. If the object has voids, a polygon with holes is constructed;
            otherwise, the 2D version of the edge is returned directly.
        """
        if len(self.void) > 0:
            # Adjacent contained regions share edges; encoding them as
            # independent interior rings produces an invalid polygon and
            # falsely exposes the divider between them to the parent room.
            polygon = shapely.difference(self.edge.force_2d(top), shapely.union_all(
                [v.edge.force_2d(top) for v in self.void]))
        else:
            polygon = self.edge.force_2d(top)
        return polygon

    def getWeightCenter(self) -> np.ndarray:
        """Return the geometric center of the space boundary and vertical extent."""
        centroid = shapely.centroid(self.force_2d())
        if self.is_void():
            bottom = self.edge.level
            top = self.edge.toplevel
        else:
            bottom = self.floor.level + self.floor.offset
            top = self.ceiling.level + self.ceiling.offset
        return np.array([centroid.x, centroid.y, (bottom + top) / 2.0])

    def is_void(self):
        """
        Check if the space is considered void based on floor and ceiling area conditions.
        
        Parameters
        ----------
        self : object
            The instance of the class containing the attributes `floor`, `ceiling`, `area`, 
            where `floor` and `ceiling` are objects with an `area` attribute, and `area` 
            represents the reference area of the space. It is assumed that `geom.AREA_PRECISION` 
            is a predefined constant used for numerical precision tolerance.
        
        Returns
        -------
        bool
            True if the space is considered void (i.e., either floor or ceiling is missing, 
            or their area is less than the space's area within the given precision), 
            False otherwise.
        """
        if not self.floor or not self.ceiling:
            return True
        # Labelled solid shells already carry a validated closed boundary.
        # A multi-level hall can have floors and ceilings at several levels;
        # comparing one modal elevation or net opaque ceiling area to the
        # footprint incorrectly classifies its rooflights as an open void.
        if getattr(self, 'settings', {}).get('geometry_shell_closed') is True:
            volume = float(self.settings.get('geometry_volume_m3', 0))
            if not np.isfinite(volume) or volume <= 0:
                raise ValueError('Closed shell requires a positive physical volume')
            return False
        # Inclined roof faces do not cover the footprint in XY projection. An attic
        # is validated by its existing floor, eave walls and roof topology instead.
        if self.space_type == "attic":
            return False
        if (
            self.ceiling.level + self.ceiling.offset
            - self.floor.level - self.floor.offset
            < geom.LEVEL_MAX_OFFSET
        ):
            return True
        if self.floor.area < self.area - geom.AREA_PRECISION:
            return True
        if self.ceiling.area < self.area - geom.AREA_PRECISION:
            # print(self.ceiling.face[0].face if len(self.ceiling.face)>0 else None)
            return True
        return False

    def is_open(self):
        """
            Check if the space is considered exposed to the outdoor air based on floor and ceiling area conditions.

            Parameters
            ----------
            self : object
                The instance of the class containing the attributes `floor`, `ceiling`, `area`,
                where `floor` and `ceiling` are objects with an `area` attribute, and `area`
                represents the reference area of the space.

            Returns
            -------
            bool
                True if the space is considered open (i.e., either floor or ceiling is missing,
                or their area is less than the space's area within the given precision),
                False otherwise.
        """
        for moElement in self.getAllFaces(to_dict=False):
            # moElement:MoosasElement = moElement
            for glazing in moElement.glazingElement:
                if glazing.category == 2 and moElement.isOuter:
                        return True
        return False

    def boundBox(self) -> np.ndarray:
        """
        Compute the axis-aligned bounding box of all faces in the object.
        
        Parameters
        ----------
        self : object
            The instance of the class containing the `getAllFaces` method, which returns a list of face objects.
            Each face object must have a `face` attribute compatible with `shapely.get_coordinates`.
        
        Returns
        -------
        numpy.ndarray
            A 2x3 array containing the minimum and maximum coordinates of the bounding box.
            The first row is the minimum (x, y, z) corner, and the second row is the maximum (x, y, z) corner.
        """
        facesCoor = [shapely.get_coordinates(moface.face, include_z=True) for moface in self.getAllFaces(to_dict=False)]
        facesCoorMin = np.min([np.min(coor, axis=0) for coor in facesCoor], axis=0)
        facesCoorMax = np.max([np.max(coor, axis=0) for coor in facesCoor], axis=0)
        return np.array([facesCoorMin, facesCoorMax])

    def applySettings(self, template_id):
        """Select a registered BuildingTemplate without copying its parameters."""
        from .building_templates import space_template
        if not isinstance(template_id, str):
            raise ValueError('Select a BuildingTemplate using its complete namespaced id')
        space_template(self.parent, self, template_id)
        self.settings['zone_template'] = template_id


    def add_neighbor(self, neighbor_id, element: MoosasElement):
        """
        Add a neighbor element to the specified neighbor ID.
        
        Parameters
        ----------
        neighbor_id : hashable
            The identifier for the neighbor group to which the element will be added.
        element : MoosasElement
            The element to be added to the neighbor list associated with neighbor_id.
        
        Returns
        -------
        None
            This function does not return any value.
        """
        if neighbor_id not in self.neighbor:
            self.neighbor[neighbor_id] = [element]
        else:
            self.neighbor[neighbor_id] += [element]

    def clear_neighbors(self):
        """Clear derived space adjacency before rebuilding it."""
        self.neighbor.clear()

    def addInternalMass(self, wall: MoosasWall):
        """
        Add an internal mass wall to the current object.
        
        Parameters
        ----------
        wall : MoosasWall
            The wall object representing internal mass to be added.
        
        Returns
        -------
        None
            This function does not return any value.
        """
        self.internalMass.append(wall)

    def getAllFaces(self, to_dict=False) -> list[MoosasElement] | dict:
        """get all faces in the space.

        Args:
            to_dict (bool, optional): whether to return a dictionary or a list. Defaults to False.

        Returns:
            list[MoosasElement]: all faces in the space.
            dict:
            {
                MoosasFloor: list[MoosasFloor],
                MoosasCeiling:list[MoosasCeiling],
                MoosasWall:list[MoosasWall],
                MoosasSkylight:list[MoosasSkylight],
                MoosasGlazing:list[MoosasGlazing],
                Shading:list[MoosasElement],
                InternalMass:list[MoosasElement],
            }

        """

        faces = {}
        faces['MoosasFloor']: list[MoosasFace] = [f for f in self.floor.face] if self.floor else []
        faces['MoosasCeiling']: list[MoosasFace] = [f for f in self.ceiling.face] if self.ceiling else []
        secondary=set(getattr(self,'settings',{}).get('secondary_ceiling_element_ids',[]))
        if secondary and self.edge and len(self.edge.wall):
            existing={f.Uid for f in faces['MoosasCeiling']}
            actual={w.Uid:w for w in self.edge.wall[0].parent.wallList if w.Uid in secondary}
            if secondary-set(actual):raise ValueError('Secondary ceiling refers to missing physical wall')
            faces['MoosasCeiling'] += [actual[uid] for uid in sorted(secondary-existing)]
        faces['MoosasWall']: list[MoosasWall] = [w for w in self.edge.wall]
        inner_boundary = self.force_2d().boundary if self.void else None
        for void in self.void:
            for wall in void.edge.wall:
                line = wall.force_2d()
                # Only the actual boundary of the surrounding room owns a
                # nested wall. A divider inside the union of two contained
                # rooms belongs to those two rooms alone.
                covered = line.intersection(inner_boundary.buffer(geom.POINT_PRECISION))
                if covered.length >= line.length - geom.POINT_PRECISION:
                    faces['MoosasWall'].append(wall)

        faces['MoosasSkylight']: list[MoosasSkylight] = []
        faces['MoosasGlazing']: list[MoosasGlazing] = []
        faces['Shading']: list[MoosasElement] = []

        for moface in faces['MoosasFloor'] + faces['MoosasCeiling']:
            faces['MoosasSkylight'] += moface.glazingElement
        for moface in faces['MoosasWall']:
            faces['MoosasGlazing'] += moface.glazingElement
        for moface in faces['MoosasGlazing']:
            faces['Shading'] += moface.shading

        for moface in faces['MoosasSkylight']:
            faces['Shading'] += moface.shading

        faces['InternalMass'] = self.internalMass

        if to_dict:
            return faces
        else:
            return [item for subList in list(faces.values()) for item in subList]

    def open_edges(self):
        """
        Return a dictionary of open edges from the geometry faces.
        
        Parameters
        ----------
        self : object
            The instance of the class containing the method. It is expected to have
            methods `getAllFaces` and access to face geometry objects with `getEdgeStr`.
        
        Returns
        -------
        dict
            A dictionary where keys are edge strings and values are the corresponding
            geometry objects (`moGeometry`) that are not shared (i.e., open edges).
        """
        edges = {}
        for moGeometry in self.getAllFaces(False):
            for edge_str in moGeometry.getEdgeStr():
                if edge_str in edges.keys():
                    edges[edge_str] = None
                else:
                    edges[edge_str] = moGeometry
        openEdges = {key: edges[key] for key in edges.keys() if edges[key] is not None}
        return openEdges

    def to_string(self, model: MoosasContainer):
        """lagacy method to print the space info"""
        string_out = 'Space' + ' ' + self.id + '\n'
        string_out += '-Area' + ' ' + str((self.area) / INCH_METER_MULTIPLIER_SQR) + '\n'
        string_out += '-Height' + ' ' + str((self.height) / INCH_METER_MULTIPLIER) + '\n'
        string_out += '-Boundary\n'
        corrdiantes = shapely.get_coordinates(shapely.force_3d(self.edge.force_2d(), z=self.floor.level), include_z=True)
        for poi in corrdiantes:
            string_out += str(poi[0] / INCH_METER_MULTIPLIER) + ' ' \
                          + str(poi[1] / INCH_METER_MULTIPLIER) + ' ' \
                          + str(poi[2] / INCH_METER_MULTIPLIER) + '\n'

        string_out += '-Floor' + ' '

        for Moosasface in self.floor.face:
            string_out += str(Moosasface.faceId) + ' '
        string_out += '\n--Area' + ' ' + str((self.area) / INCH_METER_MULTIPLIER_SQR) + '\n'
        floor_normal = shapely.get_coordinates(model.geoNormal[model.geoId.index(self.floor.face[0].faceId)],
                                              include_z=True).flatten()
        string_out += '--Normal ' + str(floor_normal[0]) + ' ' + str(floor_normal[1]) + ' ' + str(
            floor_normal[2]) + '\n'
        string_out += '--Height' + ' ' + str((self.floor.level) / INCH_METER_MULTIPLIER) + '\n'

        string_out += '-Ceiling' + ' '

        for Moosasface in self.ceiling.face:
            string_out += str(Moosasface.faceId) + ' '
        string_out += '\n--Area' + ' ' + str((self.area) / INCH_METER_MULTIPLIER_SQR) + '\n'
        floor_normal = shapely.get_coordinates(model.geoNormal[model.geoId.index(self.ceiling.face[0].faceId)],
                                              include_z=True).flatten()
        string_out += '--Normal ' + str(floor_normal[0]) + ' ' + str(floor_normal[1]) + ' ' + str(
            floor_normal[2]) + '\n'
        string_out += '--Height' + ' ' + str((self.ceiling.level) / INCH_METER_MULTIPLIER) + '\n'

        for wall, factor in zip(self.edge.wall, self.edge.FactorOfWall):
            factor = factor.array
            string_out += '-Wall' + ' '
            if type(wall.faceId) == np.ndarray:
                for indd in wall.faceId:
                    string_out += str(indd) + ' '
            else:
                string_out += str(wall.faceId) + ' '
            string_out += '\n--Area' + ' ' + str((wall.area) / INCH_METER_MULTIPLIER_SQR) + '\n'
            string_out += '--Internal' + ' ' + str(wall.isOuter) + '\n'
            string_out += '--Height' + ' ' + str((wall.level) / INCH_METER_MULTIPLIER) + '\n'
            string_out += '--Normal' + ' ' + str(factor[0]) + ' ' + str(factor[1]) + ' ' + str(0) + '\n'
            string_out += '--Glazing' + ' '
            if type(wall.glazingId) == np.ndarray:
                for indd in wall.glazingId:
                    string_out += str(indd) + ' '
            else:
                string_out += str(wall.glazingId) + ' '
            string_out += '\n--Edge\n'
            twins = shapely.get_coordinates(shapely.force_3d(wall.force_2d()), include_z=True)
            string_out += str(twins[0][0]) + ' ' + str(twins[0][1]) + ' ' + str(twins[0][2]) + '\n'
            string_out += str(twins[1][0]) + ' ' + str(twins[1][1]) + ' ' + str(twins[1][2]) + '\n'

        string_out += 'End\n'
        return string_out

    def __repr__(self):
        """Return a string representation of the object using its 'id' attribute.
        
                Parameters
                ----------
                self : object
                    The instance of the class containing the 'id' attribute.
        
                Returns
                -------
                str
                    The string value of the instance's 'id' attribute.
                """
        return self.id

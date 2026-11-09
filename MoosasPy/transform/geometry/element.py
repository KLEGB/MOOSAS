"""Building geometry and surface element definitions."""
from __future__ import annotations

import os
import re
import warnings
from typing import TYPE_CHECKING

from .geos import Projection, Vector, faceNormal, is_ccw, simplify, overlapArea, equals, selfIntersect, makeValid, bBox
from .convexify import convexify_shape
from ...utils import generate_code, searchBy, mixItemListToObject, mixItemListToList, encodeParams, GeometryError
from ...utils import shapely, np
from ...utils.tools import path
from ...utils.constant import geom

if TYPE_CHECKING:
    from ...model.boundary import MoosasFloor
    from ...model.container import MoosasContainer

# 不做inch meter转换
INCH_METER_MULTIPLIER = 1
INCH_METER_MULTIPLIER_SQR = 1


def glazing_matches_parent(face, glazing, tolerance: float | None = None) -> bool:
    """Return True when a face projection can host the glazing projection."""
    tolerance = 2 * geom.POINT_PRECISION if tolerance is None else tolerance
    try:
        face_projection = face.force_2d(region=True)
        glazing_projection = glazing.force_2d(region=True)
    except GeometryError:
        # A nearly zero-width SketchUp face can collapse to fewer than three
        # unique vertices after precision reduction. It cannot host a
        # glazing element, but it must not abort the whole model transform.
        return False
    if shapely.get_dimensions(face_projection) == shapely.get_dimensions(glazing_projection) == 1:
        face_points = shapely.get_coordinates(face_projection)
        glazing_points = shapely.get_coordinates(glazing_projection)
        if len(face_points) < 2 or len(glazing_points) < 2:
            return False
        face_axis = face_points[-1] - face_points[0]
        axis_length = np.linalg.norm(face_axis)
        if axis_length <= geom.POINT_PRECISION:
            return False
        for point in shapely.points(glazing_points):
            if shapely.distance(face_projection, point) > tolerance:
                return False

        unit_axis = face_axis / axis_length
        face_min = 0.0
        face_max = axis_length
        glazing_scalars = [
            float(np.dot(point - face_points[0], unit_axis))
            for point in glazing_points
        ]
        glazing_min = min(glazing_scalars)
        glazing_max = max(glazing_scalars)
        return glazing_min >= face_min - tolerance and glazing_max <= face_max + tolerance

    return bool(shapely.contains(face_projection, glazing_projection) or shapely.covers(face_projection, glazing_projection))


def attach_glazing_to_parent(face, glazing, tolerance: float | None = None) -> bool:
    """Attach glazing to a face when their projections satisfy the shared predicate."""
    if not glazing_matches_parent(face, glazing, tolerance=tolerance):
        return False
    face.add_glazing(glazing)
    return True


def attach_glazing_to_candidates(glazing, candidates, tolerance: float | None = None):
    """Attach glazing to the first matching candidate and return that parent."""
    for candidate in candidates:
        if attach_glazing_to_parent(candidate, glazing, tolerance=tolerance):
            return candidate
    return None


def _getElement(*key: str, dictionary: dict, strict=True) -> np.ndarray:
    """
    Get values from a dictionary corresponding to given keys and return as a numpy array.
    
    Parameters
    ----------
    *key : str
        Variable length argument list of keys to look up in the dictionary.
    dictionary : dict
        The dictionary from which to retrieve values using the provided keys.
    strict : bool, optional
        If True, raises an error when a key is not found. If False, returns an array with None if any key is missing. Default is True.
    
    Returns
    -------
    np.ndarray
        A numpy array containing the values from the dictionary corresponding to the input keys. If `strict` is False and a key is missing, returns an array with a single None value.
    """
    l = []
    for k in key:
        if k not in dictionary.keys():
            if strict:
                raise NameError(f'{k} not in the construct dictionary {dictionary}')
            else:
                return np.array([None])
        l = np.append(l, dictionary[k])
    return l


class MoosasGeometry(object):
    """protection for original geometry.
    the class object can only be created and never be changed.
    the is valid method is used to test whether this object can be used in moosas+.
    """
    __slots__ = ['__face', '__normal', '__faceId', '__category', '__holes', 'delete', 'flip']

    def __init__(self, face: shapely.Geometry | np.ndarray, faceId, normal: shapely.Geometry | Vector | np.ndarray = None,
                 category=0,
                 holes: list[shapely.Geometry | np.ndarray] = None, errors='ignore'):
        """
        Initialize a polygon object with face geometry, identifier, normal vector, and optional holes.
        
        Parameters
        ----------
        face : shapely.Geometry or np.ndarray
            The outer boundary of the polygon as a geometry object or coordinate array.
        faceId : hashable
            Identifier for the face, converted to string internally.
        normal : shapely.Geometry or Vector or np.ndarray, optional
            Normal vector of the face; if None, computed automatically using faceNormal.
        category : int, default 0
            Category label associated with the face.
        holes : list of shapely.Geometry or np.ndarray, optional
            List of inner boundaries (holes) within the face; defaults to empty list.
        errors : {'ignore', 'raise'}, default 'ignore'
            Specifies behavior when invalid geometry is detected: 'ignore' prints a warning,
            'raise' throws a GeometryError.
        
        Returns
        -------
        None
        """
        if normal is None:
            normal = faceNormal(face)
        if not holes:
            holes = []

        # test if input is valid
        if Vector(normal).length() == 0:
            raise GeometryError(face, "zero-length normal")
        self.__face: np.ndarray = self._treatFace(face)
        self.__holes: list[np.ndarray] = [self._treatFace(hole) for hole in holes]
        if self.__holes:
            # Imported outer/opening coordinates have each been rounded to
            # the millimetre grid. Fit them to the same host plane, rather
            # than independently leaving a window slightly off its wall.
            host_normal = faceNormal(shapely.Polygon(self.__face)).array
            host_origin = self.__face[0]
            for index, hole in enumerate(self.__holes):
                distance = (hole-host_origin) @ host_normal
                if np.max(np.abs(distance)) > 2*geom.POINT_PRECISION:
                    message='opening is not coplanar with its host face; ignored for this host surface'
                    if errors != 'ignore':
                        raise GeometryError(shapely.Polygon(hole),message)
                    warnings.warn(message,RuntimeWarning,stacklevel=2)
                    self.__holes[index]=None
                    continue
                if np.max(np.abs(distance)) > 1e-9:
                    self.__holes[index] = hole-distance[:,None]*host_normal
            self.__holes=[hole for hole in self.__holes if hole is not None]
        self.__normal: Vector = Vector(normal)
        self.flip = False

        self.__faceId: str = str(faceId)
        self.__category: int = category
        self.delete: bool = False

        # validation
        if self.invalid() is not None:
            if errors == 'ignore':
                print(f"******Warning: GeometryError, invalid polygon received:{self.invalid()}")
            else:
                raise GeometryError(face, f"invalid polygon received:{self.invalid()}")

    @staticmethod
    def _treatFace(face) -> np.ndarray:
        """
        Preprocess a face or hole by converting and validating its coordinates.
        
        Parameters
        ----------
        face : array-like or shapely.Geometry
            The input face or hole, either as a Shapely geometry object or a sequence of coordinate points.
            If it is a Shapely geometry, coordinates are extracted using `shapely.get_coordinates` with Z included.
        
        Returns
        -------
        numpy.ndarray
            A 2D NumPy array of shape (N, 3) containing the processed (x, y, z) coordinates of the face,
            with duplicate consecutive points removed and missing Z-coordinates filled with 0.
            Raises `GeometryError` if the resulting coordinate list has fewer than 3 unique non-collinear points.
        """
        """preprocess the face or holes."""

        # force planner by project and reproject
        face = shapely.polygons(face) if not isinstance(face, shapely.Geometry) else face
        coordinates=shapely.get_coordinates(face,include_z=True)
        normal=faceNormal(face)
        # Keep already planar source doubles exactly. A redundant inverse
        # transform can turn a shared .8 vertex into .7999999999999998,
        # destabilizing GEOS unions of otherwise exact tessellations.
        distance=(coordinates-coordinates[0])@normal.array
        if np.max(np.abs(distance))>1e-9:
            proj=Projection(origin=coordinates[0],unitZ=normal)
            face=proj.toUV(face)
            face=shapely.force_3d(shapely.force_2d(face),z=0)
            face=proj.toWorld(face)

        face = shapely.get_coordinates(face, include_z=True)
        _coordinates = []
        for point in face:
            if len(point) == 2:
                point = np.append(point, 0)
            if len(_coordinates) == 0:
                _coordinates.append(point)
            elif np.sum(np.abs(_coordinates[-1] - point)) != 0:
                _coordinates.append(point)
        if len(_coordinates) == 3:
            if np.sum(np.abs(_coordinates[-1] - _coordinates[0])) == 0:
                raise GeometryError(face, "too few points")
        if len(_coordinates) < 3:
            raise GeometryError(face, "too few points")

        return np.array(_coordinates)

    def invalid(self) -> str | None:
        """
        Check for invalid polygon geometries such as self-intersections.
        
        Parameters
        ----------
        self : object
            The instance containing the geometry data. Must have `__face` and `__holes` 
            attributes, where `__face` is the outer boundary and `__holes` is a list 
            of inner hole coordinates.
        
        Returns
        -------
        str or None
            Returns a string describing the validation error if an invalid condition 
            (e.g., self-intersection) is found; otherwise, returns None if the geometry is valid.
        """
        geos = [self.__face] + self.__holes
        for geo in geos:
            # if not shapely.points(geo[-1]) == shapely.points(geo[0]):
            #             #     return f"not a closed polygon"
            if selfIntersect(shapely.polygons(geo)):
                return f"self-intersect geo"
        # for hole in self.holes:
        #     if not shapely.contains(shapely.polygons(self.boundary), shapely.polygons(hole)):
        #         return "holes outside"
        return None

    def move_points(self, points, displacement, tolerance=geom.POINT_PRECISION) -> bool:
        """Move coordinates matching ``points`` and refresh the face normal."""
        displacement = np.asarray(displacement, dtype=float)
        changed = False
        for coordinates in [self.__face] + self.__holes:
            for index, coordinate in enumerate(coordinates):
                if any(np.all(np.abs(coordinate - point) <= tolerance) for point in points):
                    coordinates[index] = coordinate + displacement
                    changed = True
        if changed:
            self.__normal = Vector(faceNormal(self.face))
        return changed

    @property
    def face(self) -> shapely.Geometry:
        """
        Return the face geometry of the object as a polygon.
        
        Parameters
        ----------
        self : object
            The instance of the class containing the boundary and holes attributes.
        
        Returns
        -------
        shapely.Geometry
            A Shapely geometry representing the polygon formed by the boundary and optional holes.
        """
        holes = self.holes if len(self.__holes) > 0 else None
        return shapely.polygons(geometries=self.boundary, holes=holes)

    @property
    def boundary(self):
        """
        Boundary of the face as a LinearRing.
        
        Returns a LinearRing geometry representing the boundary of the face
        using the shapely.linearrings function applied to the internal face data.
        
        Parameters
        ----------
        self : object
            The instance of the class containing the `__face` attribute.
        
        Returns
        -------
        shapely.Geometry
            A LinearRing geometry representing the boundary of the face.
        """
        return shapely.linearrings(self.__face)

    @property
    def normal(self) -> shapely.Geometry:
        """
        Geometry of the normal vector, optionally flipped.
        
        Parameters
        ----------
        self : object
            The instance of the class containing this property. It is expected to have
            attributes `__normal` (with a `geometry` attribute) and `flip` (boolean).
        
        Returns
        -------
        shapely.Geometry
            The geometry of the normal vector. If `self.flip` is True, returns the negated
            normal's geometry; otherwise, returns the original normal's geometry.
        """
        if self.flip:
            return (-self.__normal).geometry
        else:
            return self.__normal.geometry

    @property
    def faceId(self) -> str:
        """
        Get the face ID as a string.
        
        Returns
        -------
        str
            The face ID.
        """
        return self.__faceId

    @property
    def category(self) -> int:
        """
        Category identifier for the surface element.
        
        Returns the category of the surface as an integer, where each value represents 
        a specific type of surface or element (e.g., opaque, translucent, shading, etc.).
        
        Returns
        -------
        int
            The category code:
            - -2: Ignore faces (excluded from calculations)
            - -1: Shading faces (included as shading elements)
            -  0: Opaque surface
            -  1: Translucent surface
            -  2: Air wall
            -  3: Wall element (MoosasWall)
            -  4: Plane element (MoosasFace)
            -  5: Glazing element (MoosasGlazing)
            -  6: Skylight element (MoosasSkylight)
        """
        return self.__category

    def setCategory(self, cat=None) -> None:
        """
        Set the category attribute based on input or predefined conditions.
        
        Parameters
        ----------
        cat : int, optional
            The category value to set. If not provided, the category is determined
            based on the current value of `self.category` using internal rules:
            - If `self.category` is 3, 4, or -1, sets `self.__category` to 0.
            - If `self.category` is greater than or equal to 5, sets `self.__category` to 1.
            Default is None.
        
        Returns
        -------
        None
        """
        if cat is not None:
            self.__category = cat
        else:
            if self.category == 3 or self.category == 4 or self.category == -1:
                self.__category = 0
            if self.category >= 5:
                self.__category = 1

    @property
    def holes(self) -> list[shapely.Geometry]:
        """
        List of hole geometries as linearrings.
        
        Returns a list of hole geometries in the object, converted to linearrings using shapely.
        
        Parameters
        ----------
        self : object
            The instance of the class containing the private attribute __holes.
        
        Returns
        -------
        list of shapely.Geometry
            A list of shapely Geometry objects representing the holes as linearrings.
        """
        return [shapely.linearrings(hole) for hole in self.__holes]

    def getEdgeStr(self) -> list[str]:
        """get a unique edge string of the boundary, ignore the direction of the edge."""
        faces = [self.boundary] + self.holes
        edge_str_s = {}
        for face in faces:
            coors = shapely.get_coordinates(face, include_z=True)

            for i in range(1, len(coors)):
                str1, str2 = '', ''
                for corDim in coors[i]:
                    str1 += f'{int(corDim * 100)}_'
                for corDim in coors[i - 1]:
                    str2 += f'{int(corDim * 100)}_'
                if str1 != str2:
                    if coors[i - 1][0] + coors[i - 1][1] + coors[i - 1][2] > coors[i][0] + coors[i][1] + coors[i][2]:
                        edge_str = f'{str1}{str2}'
                    else:
                        edge_str = f'{str2}{str1}'
                    if edge_str in edge_str_s:
                        edge_str_s[edge_str] = 1
                    else:
                        edge_str_s[edge_str] = 0

        return [edgeStr for edgeStr in edge_str_s if edge_str_s[edgeStr] == 0]


class MoosasElement(object):
    """
    Base class, which expresses all geometry, loads basic methods & basic members
    new feature:
    now the geometry will be created based on _Geometry class to ensure the consistency of id and geometry
    in this case, the model is required to input.
    in the init method we will check the consistency of id and geometry.
    besides, face, faceId and normal object will be properties and get from __geometries

    -------------------------------------------
    init params:
    faceId: geometry faceId in MoosasModel, should be in the list MoosasModel.geoId
    model: MoosasModel

    optional params:
    'level': the building floor level of the geometry
    'offset': the offset from the building level
    'glazingId': a list of glazing faceId
    'space': a list of space id this geometry belongs to

    attribute:
    'parent': The model to which the face belongs
    'isOuter': whether this face is an external faces
    'Uid': unique Id of the element
    'shading': a list of shading elements.
    'U_Value' : U-value of this element
    level,offset,space: those in optional params.

    properties:
    'face': Faces (list) of the loaded geometry
    'normal': a unique face normal
    'faceId': The identification of the face(s), defined by the id of the read geo data
    'category': category of the Geometry, 0==opaque element, 1==transparent element, 2==aperture element
    'area': total face area of the element
    'elevation': level + offset
    'wwr': Window-to-Wall ratio of this element calculated based on UV method
    'firstFaceId': the first faceId of all geometries in this element, to avoid error in some method.
    'glazingId': a list of glazing faceId
    'glazingElement': return MoosasGlazing object instead of glazingId


    -------------------------------------------

    method:
    'area3d': calculate area in 3d projection, for itself or other faces.
    'glazingUV': get UV faces of all glazing elements
    'faceUV': get UV faces of itself
    'getEdgeStr': get unique descriptions in string of all edges in this element
    'getWeightCenter': Gets the weighted center point
    'add_glazing': add glazing Element to the glazingId.

    Conceptual method:
    'force_2d': A conceptual approach to obtaining a two-dimensional representation of geometry on a floor plan
    'representation': get the simplified representation of the geometry
    'dissolve': conceptual method to merge other element
    fromDict: construct an element from a dictionary which may be given by toDictionary method from a xmlTree

    """
    __slots__ = ['__geometries', 'level', 'offset', 'Uid', '_u_value', 'settings', 'explicit_settings', '__glazingElement', 'parent', 'neighbor', 'isOuter', 'space',
                 'shading','description']

    def __init__(self, model: MoosasContainer,
                 faceId: str | list[str] | np.ndarray[str] | MoosasGeometry | list[MoosasGeometry] | np.ndarray[
                     MoosasGeometry], level: float = None,
                 offset: float = None,
                 glazingId: str | list[str] | np.ndarray[str] = None,
                 glazingElement: MoosasElement | list[MoosasElement] | np.ndarray[MoosasElement] = None, space=None,
                 uid=None):
        """
        Initialize a new instance with model, geometry, and optional parameters for shading and glazing.
        
        Parameters
        ----------
        model : MoosasContainer
            The container model that holds the geometry and other elements.
        faceId : str or list of str or numpy.ndarray of str or MoosasGeometry or list of MoosasGeometry or numpy.ndarray of MoosasGeometry
            Identifier(s) or geometry object(s) representing faces; if string, must exist in model's geoId.
        level : float, optional
            Elevation level of the face(s), by default None.
        offset : float, optional
            Offset distance from the face(s), by default None.
        glazingId : str or list of str or numpy.ndarray of str, optional
            Identifier(s) for glazing elements to be associated, by default None.
        glazingElement : MoosasElement or list of MoosasElement or numpy.ndarray of MoosasElement, optional
            Predefined glazing element(s) to be associated, by default None.
        space : list of str, optional
            List of space identifiers this object belongs to, by default None.
        uid : str, optional
            Unique identifier for the instance; if not provided, a 6-character code is generated.
        
        Returns
        -------
        None
        """
        self.parent: MoosasContainer = model  # this is MoosasModel !!!
        self.Uid: str = generate_code(6) if uid is None else uid
        self.level: float = level
        self.offset: float = offset
        self.shading = []
        # Element properties are intentionally kept separate from zone settings.
        # The legacy U_Value attribute remains a property backed by this mapping.
        self.settings = {}
        self.explicit_settings = set()
        self._u_value: float = 1.8
        self.U_Value = 1.8
        self.__glazingElement: list[MoosasElement] = mixItemListToList(
            glazingElement) if glazingElement is not None else []
        if glazingId is not None:
            self.__glazingElement += self.glazingElementFromId(glazingId)
        self.space: list[str] = mixItemListToList(space) if space is not None else []
        self.isOuter: bool = True
        self.neighbor = {}
        self.description = ""

        # get the geometry(s)
        faceId = mixItemListToList(faceId)
        self.__geometries: np.ndarray[MoosasGeometry] = np.array([])
        for idd in faceId:
            if isinstance(idd, str):
                try:
                    idd = model.geoId.index(idd)
                except ValueError as ve:
                    raise ValueError(f"index {idd} is not in the library.")
                self.__geometries: np.ndarray[MoosasGeometry] = np.append(self.__geometries, model.geometryList[idd])
            elif isinstance(idd, MoosasGeometry):
                self.__geometries: np.ndarray[MoosasGeometry] = np.append(self.__geometries, idd)
            else:
                raise TypeError("idd must be either a string or a MoosasGeometry")

    @property
    def geometry(self):
        return self.__geometries

    @property
    def U_Value(self):
        return self.settings.get('u_value', self._u_value)

    @U_Value.setter
    def U_Value(self, value):
        self._u_value = value
        if hasattr(self, 'settings'):
            self.settings['u_value'] = value

    @property
    def glazingElement(self) -> list[MoosasGlazing | MoosasSkylight]:
        """protect the __glazingElement attribute"""
        return mixItemListToList(self.__glazingElement)

    @property
    def glazingId(self):
        """get glazingId from glazingElement"""
        glsId = [ids for gls in self.glazingElement for ids in mixItemListToList(gls.Uid)]
        return glsId

    @property
    def face(self) -> shapely.Geometry | np.ndarray[shapely.Geometry]:
        """if the element only contains one face, a pygoes.Geometry will be return
        if you want to get a list anyway,
        you can call mixItemListToList() func in utils.tools.
        """
        return mixItemListToObject([geo.face for geo in self.__geometries])

    @property
    def mergedFace(self) -> shapely.Geometry:
        """return a single face merging all faces contained in this element"""
        if len(self.__geometries) == 1:
            return self.__geometries[0].face
        proj = Projection(origin=np.mean(shapely.get_coordinates(self.face, include_z=True), axis=0), unitZ=self.normal)
        UVFaces = [proj.toUV(g) for g in self.face]
        mergedUVFace = shapely.force_3d(shapely.union_all(shapely.force_2d(UVFaces)), z=0)
        if shapely.is_empty(mergedUVFace):
            return shapely.GeometryCollection()
        return proj.toWorld(mergedUVFace)

    @property
    def holes(self) -> shapely.Geometry | np.ndarray[shapely.Geometry]:
        """
        List of hole geometries from all polygons in the collection.
        
        Returns a flattened list of hole geometries extracted from each polygon 
        in the internal geometries array. Each hole is represented as a shapely Geometry object.
        
        Returns
        -------
        shapely.Geometry or numpy.ndarray of shapely.Geometry
            A list or array containing the hole geometries from all polygons.
        """
        return [h for geo in self.__geometries for h in geo.holes]

    @property
    def normal(self) -> np.ndarray:
        """if the element contains multi faces,
        the normal has the best description of the faces will be returned"""
        if len(self.__geometries) == 1:
            return Vector(self.__geometries[0].normal).unit().array

        # PCA1: get covariance matrix
        coordinates = shapely.get_coordinates([geo.face for geo in self.__geometries], include_z=True) - np.array(
            self.getWeightCenter())
        C = np.zeros((3, 3))
        for coor in coordinates:
            C += np.matmul(coor.reshape(3, 1), coor.reshape(1, 3))
        C /= len(coordinates)

        # PCA2: the covariance matrix is real and symmetric.  ``eig`` may
        # return complex-valued vectors for a nearly singular matrix (notably
        # with newer NumPy versions), which Shapely cannot consume downstream.
        # ``eigh`` guarantees real eigenpairs for this matrix type and stores
        # eigenvectors by *column*.
        eig_values, eig_vectors = np.linalg.eigh(C)
        normal = np.real_if_close(eig_vectors[:, np.argmin(eig_values)])
        if np.iscomplexobj(normal):
            raise GeometryError(self.face, "complex multi-face normal")
        normal = np.asarray(normal, dtype=float)
        length = np.linalg.norm(normal)
        if not np.isfinite(normal).all() or length < geom.POINT_PRECISION:
            raise GeometryError(self.face, "invalid multi-face normal")

        # Eigenvector signs are arbitrary.  Preserve the orientation of the
        # first constituent face so callers get a stable outward normal.
        reference = Vector(self.__geometries[0].normal).unit().array
        if np.dot(normal, reference) < 0:
            normal = -normal
        return normal / length

    @property
    def faceId(self) -> str | np.ndarray[str]:
        """if the element only contains one face, a str will be return
            if you want to get a list anyway,
            you can call mixItemListToList() func in utils.tools.
        """
        return mixItemListToObject([geo.faceId for geo in self.__geometries])

    @property
    def category(self) -> int | np.ndarray[int]:
        """if the element only contains one face, a int will be return
            if you want to get a list anyway,
            you can call mixItemListToList() func in utils.tools.
        """
        return mixItemListToList([geo.category for geo in self.__geometries])[0]

    def setCategory(self, cat=None):
        for idx, geometry in enumerate(self.__geometries):
            self.__geometries[idx].setCategory(cat)

    @property
    def area(self) -> float:
        """quick link to self.area3d"""
        return self.area3d()

    @property
    def elevation(self) -> float:
        """correct elevation of the object"""
        return self.offset + self.level

    @property
    def wwr(self) -> float:
        """
        Calculate the Window-to-Wall Ratio (WWR) based on projected areas.
        
        The WWR is computed as the ratio of glazing area to the total wall surface area,
        using 3D projections onto wall surfaces. The calculation does not use the exact
        surface area of window objects, but rather their projection on the wall.
        
        Parameters
        ----------
        self : object
            The instance of the class containing the method. Expected to have:
            - `glazingElement`: iterable of objects with a `face` attribute representing glazing geometry.
            - `face`: geometric representation of wall faces, possibly nested.
            - `area3d(faces)` method: computes the 3D area of given faces.
        
        Returns
        -------
        float
            The Window-to-Wall Ratio (WWR), defined as the ratio of projected glazing area 
            to the total projected wall surface area.
        """
        """calculate Window Wall Ratio based on self.area3d()
        the wwr is not based on exact surface area of the window object, but based on the projection on the wall surface.
        """
        gFace = []
        for glsFace in self.glazingElement:
            gFace = np.append(gFace, glsFace.face)
        areaGlazing = self.area3d(faces=gFace)
        surface = [shapely.polygons(shapely.get_exterior_ring(f)) for f in mixItemListToList(self.face)]
        areaSurface = self.area3d(faces=surface)
        if areaSurface <= 0:
            return 0.0
        return areaGlazing / areaSurface

    @property
    def firstFaceId(self):
        """
        First face ID from the object.
        
        Returns the first face ID from the object, which can accelerate calculations 
        and prevent errors in contexts where only a single face ID is expected. 
        This is particularly useful for objects containing only one face, such as 
        MoosasGlazing, MoosasSkylight, or MoosasFace, and ensures compatibility 
        with functions like `searchBy` in `utils.tools` that expect a single attribute.
        
        Parameters
        ----------
        self : object
            The instance of the class containing the `faceId` attribute. 
            It is expected to have a `faceId` attribute which is either 
            a scalar value or a numpy ndarray.
        
        Returns
        -------
        scalar or int or any
            The first face ID. If `faceId` is not a numpy array, returns `self.faceId`. 
            Otherwise, returns the first element of `self.faceId` (i.e., `self.faceId[0]`).
        """
        """give a single faceid,
        which can accelerate the calculation and avoid error sometimes.
        for example, the MoosasGlazing/MoosasSkylight/MoosasFace object are all contain only one face.
        and the searchBy() func in utils.tools only supports singe attr in searching.
        ***actually searching by list are valid in python 3.10
        """
        if type(self.faceId) != np.ndarray:
            return self.faceId
        else:
            return self.faceId[0]

    def glazingElementFromId(self, glazingIds) -> list[MoosasGlazing | MoosasSkylight]:
        """
        Get glazing elements by their IDs.
        
        Parameters
        ----------
        glazingIds : list or array-like
            List of glazing element IDs (Uid) to search for. Can be a mix of types that is converted to a flat list.
        
        Returns
        -------
        list of MoosasGlazing or MoosasSkylight
            A list of glazing objects (either MoosasGlazing or MoosasSkylight instances) matching the provided IDs.
        """
        """get the glazing object in a ndarray
        the glazing are searchBy firstFaceId, since all glazing only contains one face.
        """
        glsid = mixItemListToList(glazingIds)
        if len(glsid) == 0:
            return []
        gls = []
        gls = np.append(gls,
                        np.array(self.parent.glazingList)[searchBy('Uid', glsid, self.parent.glazingList)])
        gls = np.append(gls,
                        np.array(self.parent.skylightList)[searchBy('Uid', glsid, self.parent.skylightList)])
        return list(gls)

    def replaceGeo(self, geoId):
        """
        Replace the current geometries with new ones based on provided geometry IDs.
        
        Parameters
        ----------
        geoId : int or list of int
            Geometry ID(s) to be used for replacing the current geometries. 
            If a single integer is provided, it will be treated as a list with one element.
        
        Returns
        -------
        None
            This function does not return any value. It modifies the internal `__geometries` attribute in place.
        """
        # get the geometry(s)
        faceId = mixItemListToList(geoId)
        self.__geometries: np.ndarray[MoosasGeometry] = np.array([])
        for idd in faceId:
            try:
                idd = self.parent.geoId.index(idd)
            except:
                raise ValueError(f"index {idd} is not in the library.")
            self.__geometries: np.ndarray[MoosasGeometry] = np.append(self.__geometries, self.parent.geometryList[idd])

    def delete(self):
        """
        Delete all geometries in the object by setting their delete flag to True.
        
        Parameters
        ----------
        self : object
            The instance of the class containing the geometries to be marked for deletion.
            It is expected to have a private attribute `__geometries` which is an iterable
            of geometry objects that each support assignment to a `delete` attribute.
        
        Returns
        -------
        None
            This function does not return any value.
        """
        for geo in self.__geometries:
            geo.delete = True

    def area3d(self, faces=None, project=None) -> float:
        """Measure the projected union so merged coincident faces count once."""
        if project is None:
            trans = Projection(self.getWeightCenter(), self.normal)
        else:
            trans = project
        if faces is None:
            faces = np.array(self.face).flatten()
        # print(self.face)
        # [print(faceNormal(face), self.normal) for face in faces]
        return float(shapely.area(shapely.union_all([trans.toUV(face) for face in faces])))

    def faceUV(self, uniform=False) -> list[shapely.Geometry]:
        """
        Get the UV-projected faces of a surface, optionally normalized to a unit square.
        
        Parameters
        ----------
        uniform : bool, optional
            If True, the UV coordinates are normalized to fit within the unit square [0, 1] 
            based on the bounding box of all faces. Default is False.
        
        Returns
        -------
        list of shapely.Geometry
            A list of 2D geometries representing the UV-projected faces. If `uniform` is True,
            the coordinates are scaled to the unit square; otherwise, they are in raw UV space.
        """
        """Ver1.3 The projection class is added to perform UV expression on the surface and the glass surface
        get the UV faces
        """
        trans = Projection(self.getWeightCenter(), self.normal)
        faces = np.array(self.face).flatten()
        faces = [shapely.force_2d(trans.toUV(face)) for face in faces]
        if uniform:
            boundaryBox = shapely.get_coordinates(faces)
            boundaryBox = [[np.min(boundaryBox.T[0]), np.min(boundaryBox.T[1])],
                           [np.max(boundaryBox.T[0]), np.max(boundaryBox.T[1])]]
            uniformFaces = []
            for face in faces:
                face = shapely.get_coordinates(face)
                for i in range(len(face)):
                    face[i][0] = (face[i][0] - boundaryBox[0][0]) / (boundaryBox[1][0] - boundaryBox[0][0])
                    face[i][1] = (face[i][1] - boundaryBox[0][1]) / (boundaryBox[1][1] - boundaryBox[0][1])
                uniformFaces.append(shapely.polygons(face))

            return uniformFaces
        else:
            return faces

    def glazingUV(self, uniform=False) -> list[shapely.Geometry]:
        """
        Get UV-projected glazing faces from the surface.
        
        Parameters
        ----------
        self : object
            The instance of the class containing glazing elements and geometric data.
        uniform : bool, optional
            If True, normalizes the UV coordinates to the unit square [0, 1] based on the bounding box 
            of the input faces. Default is False.
        
        Returns
        -------
        list of shapely.Geometry
            A list of geometries representing the UV-projected glazing faces. If `uniform` is True, 
            the coordinates are normalized; otherwise, they are in raw UV space.
        """
        """Ver1.3 The projection class is added to perform UV expression on the surface and the glass surface
            get the UV glazing faces
        """
        faces = []
        gidList = []
        for gElement in self.glazingElement:
            gidList += mixItemListToList(gElement.faceId)
        for gid in gidList:
            gface = self.parent.geoFaceList[self.parent.geoId.index(gid)]
            trans = Projection(self.getWeightCenter(), self.normal)
            faces.append(trans.toUV(gface))
        if uniform:
            boundaryBox = shapely.get_coordinates(np.array(self.face).flatten())
            boundaryBox = [[np.min(boundaryBox.T[0]), np.min(boundaryBox.T[1])],
                           [np.max(boundaryBox.T[0]), np.max(boundaryBox.T[1])]]
            uniformFaces = []
            for face in faces:
                face = shapely.get_coordinates(face)
                for i in range(len(face)):
                    face[i][0] = (face[i][0] - boundaryBox[0][0]) / (boundaryBox[1][0] - boundaryBox[0][0])
                    face[i][1] = (face[i][1] - boundaryBox[0][1]) / (boundaryBox[1][1] - boundaryBox[0][1])
                uniformFaces.append(shapely.polygons(face))
            return uniformFaces
        else:
            return faces

    def getEdgeStr(self) -> list[str]:
        """get a unique edge string of the boundary, ignore the direction of the edge."""
        edge_str_s = set()
        for geo in self.__geometries:
            edge_str_s = edge_str_s | set(geo.getEdgeStr())
        return list(edge_str_s)

    def getWeightCenter(self) -> np.ndarray[np.ndarray]:
        """
        Compute the weight center (centroid) of a 3D face.
        
        Parameters
        ----------
        self : object
            The instance of the class containing the `face` attribute, which is a geometric object 
            supported by shapely representing a 2D or 3D polygonal face.
        
        Returns
        -------
        np.ndarray
            A 1D numpy array of shape (3,) containing the x, y, and z coordinates of the centroid, 
            computed as the mean of the face's vertex coordinates.
        """
        point_list = shapely.get_coordinates(self.face, include_z=True)[:-1]
        return np.array([np.mean(point_list.T[0]), np.mean(point_list.T[1]), np.mean(point_list.T[2])])

    def add_glazing(self, glazingObject: MoosasGlazing | MoosasSkylight):
        """
        Add a glazing object to the current element.
        
        Parameters
        ----------
        glazingObject : MoosasGlazing or MoosasSkylight
            The glazing object to be added. This object will be appended to the 
            internal list of glazing elements and its parentFace attribute will 
            be set to the current instance.
        
        Returns
        -------
        None
            This function does not return any value.
        """
        self.__glazingElement = list(np.append(self.__glazingElement, glazingObject))
        glazingObject.parentFace = self

    def dissolve(self, others):
        """method to merge multiple elements"""
        if not (isinstance(others, list) or isinstance(others, np.ndarray)):
            others = [others]
        others = list(others)
        target = None
        for o in others:
            # find face with coEdge and merge it
            edgeStr = set(self.getEdgeStr()) & set(o.getEdgeStr())
            if len(edgeStr) > 0:
                target = o
                break
        if target:
            self._merge(target)
            # continue to merge others
            others.remove(target)
            self.dissolve(others)
        else:
            return

    def _merge(self, other):
        """
        Merge another object into this one by aligning face normals and combining geometries.
        
        Parameters
        ----------
        other : object
            Another object to merge with this one. Must have methods `getEdgeStr`, 
            `getWeightCenter`, and attributes `__geometries`, `normal`, `offset`, 
            `level`, and `glazingElement`. The `getEdgeStr` method should return a 
            list of edge strings, and `getWeightCenter` should return the center point.
        
        Returns
        -------
        None
            This function modifies the current instance in place by merging geometries, 
            adjusting offset and level, flipping normals if necessary, and adding 
            glazing elements from the other object.
        """
        """flip all face to the same side normal"""
        edgeStr = list(set(self.getEdgeStr()) & set(other.getEdgeStr()))[0]
        edgeStr = np.array([int(dim) / 100 for dim in edgeStr.split('_')[:-1]])

        poi1, poi2 = edgeStr[:3], edgeStr[3:]
        trans = Projection(poi1, poi2 - poi1)
        vectors = [
            self.getWeightCenter(),
            other.getWeightCenter(),
            self.normal,
            other.normal
        ]
        vectors = [trans.toUV(Vector(v).geometry) for v in vectors]
        unitxSelf, unitxOther, unitySelf, unityOther = vectors
        if Vector.dot(Vector.cross(unitxSelf, unitySelf), Vector.cross(unitxOther, unityOther)) > 0:
            for g in range(len(other.__geometries)):
                other.__geometries[g].flip = True

        """method to merge one other elements"""
        self.__geometries = np.append(self.__geometries, other.__geometries)
        self.offset = min(self.offset + self.level, other.offset + other.level) - min(self.level, other.level)
        self.level = min(self.level, other.level)
        for gls in other.glazingElement:
            self.add_glazing(gls)

    def force_2d(self) -> shapely.Geometry:
        """return a linestring formatted in shapely,or an array vector object"""
        raise NotImplementedError("force_2d method should be implemented in child class")

    def representation(self) -> shapely.Geometry:
        """return a simplified representation for the geometry"""
        raise NotImplementedError("representation method should be implemented in child class")

    @classmethod
    def fromDict(cls, elementDict, model: MoosasContainer):
        """construct an element from a dictionary
        if the faceId record in the dictionary is already occurred in the model,
        the MoosasElement contains that faceId will be returned directly.
        """
        faceId = _getElement('faceId', dictionary=elementDict)[0]
        if not hasattr(model.builtData, 'elements') or not hasattr(model.builtData, 'glazing'):
            model.update()
        if faceId in model.builtData.elements:
            return model.builtData.elements[faceId]
        else:
            element = cls(
                faceId=_getElement('faceId', dictionary=elementDict)[0].split(" "),
                model=model,
                level=_getElement('level', dictionary=elementDict, strict=False)[0],
                offset=_getElement('offset', dictionary=elementDict, strict=False)[0],
                space=_getElement('space', dictionary=elementDict, strict=False)[0],
            )
            for ids in _getElement('faceId', dictionary=elementDict)[0].split(" "):
                model.builtData.elements[ids] = element
            glazingId: str = _getElement('glazingId', dictionary=elementDict, strict=False)[0]
            if glazingId is not None:
                for ids in glazingId.split(" "):
                    if ids not in model.builtData.glazing:
                        glsGeometry: MoosasGeometry = model.findFace(ids)[0]
                        if np.abs(Vector.dot(glsGeometry.normal, Vector([0, 0, 1]))) >= geom.HORIZONTAL_ANGLE_THRESHOLD:
                            glsElement = MoosasSkylight(model, glsGeometry.faceId)
                            model.skylightList.append(glsElement)
                        else:
                            glsElement = MoosasGlazing(model, glsGeometry.faceId)
                            model.glazingList.append(glsElement)
                        element.add_glazing(glsElement)
                        model.builtData.glazing[ids] = glsElement
                    else:
                        element.add_glazing(model.builtData.glazing[ids])


class MoosasFace(MoosasElement):
    """
    The base class, which records the horizontal face
    since we also have MoosasFloor to record multi-surface,
    we strictly require the face attribute only contain one geometry
    """
    __slots__ = ['parentFloors']

    def __init__(self, model: MoosasContainer, faceId: str | MoosasGeometry, level: float = None,
                 offset: float = None, glazingId=None,
                 glazingElement: MoosasElement | list[MoosasElement] | np.ndarray[MoosasElement] = None, space=None,
                 uid=None):
        """
        Initialize a MoosasFace object with geometric and structural properties.
        
        Parameters
        ----------
        model : MoosasContainer
            The container model to which the face belongs.
        faceId : str or MoosasGeometry
            Identifier or geometry object representing the face. Must not be a list or array.
        level : float, optional
            The level (elevation) of the face. If not provided, inferred from geometry.
        offset : float, optional
            Vertical offset of the face relative to its level. Calculated if not provided.
        glazingId : Any, optional
            Identifier for glazing associated with the face. Default is None.
        glazingElement : MoosasElement or list[MoosasElement] or np.ndarray[MoosasElement], optional
            Glazing element(s) attached to the face. Default is None.
        space : Any, optional
            Spatial context or zone to which the face belongs. Default is None.
        uid : str, optional
            Unique identifier for the face. Generated if not provided.
        
        Returns
        -------
        None
            This constructor does not return a value.
        """
        if isinstance(faceId, list) or isinstance(faceId, np.ndarray):
            raise ValueError("MoosasFace should only contain one geometry")
        if isinstance(faceId, MoosasGeometry):
            uid = f"face_{faceId.faceId}" if uid is None else uid
        else:
            uid = f"face_{mixItemListToList(faceId)[0]}" if uid is None else uid
        super(MoosasFace, self).__init__(model, faceId, level=level, offset=offset, glazingElement=glazingElement,
                                         space=space, glazingId=glazingId, uid=uid)
        self.parentFloors: list[MoosasFloor] = []
        coordinates_z = shapely.get_coordinates(self.face, include_z=True)[:, 2]
        inclined = abs(float(self.normal[2])) < 0.99
        reference_height = float(np.round(
            np.min(coordinates_z) if inclined else np.mean(coordinates_z), 3
        ))
        if not np.isfinite(reference_height):
            raise GeometryError(self.face, "invalid geometry")

        if self.level is None:
            nearest = min(model.levelList, key=lambda z: (abs(z - reference_height), z)) if model.levelList else None
            tolerance = geom.LEVEL_MAX_OFFSET if inclined else geom.POINT_PRECISION
            frozen = getattr(model, "_levels_frozen", False)
            if nearest is not None and (abs(nearest - reference_height) <= tolerance + 1e-9 or (frozen and not inclined)):
                self.level = nearest
            elif frozen:
                raise GeometryError(self.face, "inclined face has no storey within LEVEL_MAX_OFFSET")
            else:
                self.level = reference_height
                model.levelList.append(self.level)
                model.levelList.sort()
        if offset is None:
            self.offset = reference_height - self.level

    @classmethod
    def fromDict(cls, elementDict, model: MoosasContainer):
        """
        Create a MoosasFace instance from a dictionary representation.
        
        Parameters
        ----------
        elementDict : dict
            Dictionary containing the element data.
        model : MoosasContainer
            Model container that holds the built data and manages elements.
        
        Returns
        -------
        MoosasFace
            An instance of MoosasFace initialized with data from elementDict and associated with the given model.
        """
        element = super(MoosasFace, cls).fromDict(elementDict, model)
        faceElement = cls(model, element.faceId, element.glazingId)
        for fid in mixItemListToList(element.faceId):
            model.builtData.elements[fid] = faceElement
        return faceElement

    def force_2d(self, region=True) -> shapely.Geometry:
        """
        Force the geometry into 2 dimensions.
        
        Parameters
        ----------
        region : bool, optional
            Placeholder argument for consistency; has no effect on the operation.
        
        Returns
        -------
        shapely.Geometry
            The input geometry converted to 2D.
        """
        # region is an useless arg to ensure consistency

        footprint = shapely.force_2d(self.face)
        if not shapely.is_valid(footprint):
            # Coordinate snapping can leave a backtracking edge or split a
            # ring at a touching vertex. Keep every repaired polygonal part;
            # zero-area lines must not enter floor-area overlay operations.
            repaired = shapely.make_valid(footprint)
            parts = [part for part in shapely.get_parts(repaired)
                     if shapely.get_dimensions(part) == 2]
            footprint = shapely.union_all(parts)
        return footprint


    def dissolve(self, wall):
        """
        Dissolves the specified wall from the structure.
        
        Parameters
        ----------
        self : object
            The instance of the class calling this method.
        wall : str or int
            Identifier for the wall to be dissolved. Can be a name (string) or index (integer).
        
        Returns
        -------
        None
            This function does not return any value but raises an exception when called.
        """
        raise Exception("MoosasFace cannot used to dissolve")

    def representation(self) -> shapely.Geometry:
        """
        Return a 3D geometric representation of the object with specified elevation.
        
        Parameters
        ----------
        self : object
            The instance of the class containing `force_2d` and `elevation` attributes.
            Must have methods `force_2d()` and attribute `elevation` defined.
        
        Returns
        -------
        shapely.Geometry
            A 3D geometry created by converting the 2D forced geometry to 3D using the given elevation.
        """
        return shapely.force_3d(self.force_2d(), z=self.elevation)


class MoosasSkylight(MoosasFace):
    '''
        一个特别简单的glazing类，只为与Moosasface区分开
        '''
    __slots__ = ['parentFace']

    def __init__(self, model: MoosasContainer, faceId: str | MoosasGeometry, level: float = None,
                 offset: float = None, glazingId=None,
                 glazingElement: MoosasElement | list[MoosasElement] | np.ndarray[MoosasElement] = None, space=None,
                 uid=None, operable: float = 0.5):
        """
        Initialize a MoosasSkylight object.
        
        Parameters
        ----------
        model : MoosasContainer
            The container model to which the skylight belongs.
        faceId : str or MoosasGeometry
            Identifier or geometry object representing the skylight's face. Must not be a list.
        level : float, optional
            Elevation level of the skylight. Default is None.
        offset : float, optional
            Vertical offset from the base level. Default is None.
        glazingId : object, optional
            Identifier for the glazing material or component. Default is None.
        glazingElement : MoosasElement or list of MoosasElement or np.ndarray of MoosasElement, optional
            Glazing element(s) associated with the skylight. Default is None.
        space : object, optional
            Space to which the skylight belongs. Default is None.
        uid : str, optional
            Unique identifier for the skylight. If not provided, it is generated based on `faceId`.
        
        Returns
        -------
        None
        """
        if isinstance(faceId, list):
            raise ValueError("MoosasFace should only contain one geometry")
        if isinstance(faceId, MoosasGeometry):
            uid = f"sky_{faceId.faceId}" if uid is None else uid
        else:
            uid = f"sky_{mixItemListToList(faceId)[0]}" if uid is None else uid
        super(MoosasSkylight, self).__init__(model, faceId, level=level, offset=offset, glazingElement=glazingElement,
                                             space=space, glazingId=glazingId, uid=uid)
        self.parentFace: MoosasFace | None = None
        self.SHGC = None
        self.operable = operable

    @property
    def SHGC(self):
        return self.settings['shgc']

    @SHGC.setter
    def SHGC(self, value):
        self.settings['shgc'] = value

    @property
    def operable(self):
        return self.settings['operable']

    @operable.setter
    def operable(self, value):
        self.settings['operable'] = value

    @property
    def orientation(self):
        """
        Return the orientation vector based on the normal.
        
        Parameters
        ----------
        self : object
            The instance of the class containing the `normal` attribute.
        
        Returns
        -------
        Vector
            A Vector object created from the `normal` attribute of the instance.
        """
        return Vector(self.normal)

    def apply_to_face(self, face: MoosasFace):
        """
        Apply the glazing to a given face.
        
        Parameters
        ----------
        face : MoosasFace
            The face object to which the glazing will be added.
        
        Returns
        -------
        None
            This function does not return any value.
        """
        face.add_glazing(self)
        # self.parentFace = face
        # face.glazingId.append(self.Uid)


class MoosasWall(MoosasElement):
    """
    The basic class, which expresses the read vertical face, has the following new members:
        Bottom Data (Unique):
        '__botProjection': The bottom projection line of the wall, which will be used to identify the closed area, represented by a sequence of dots, is automatically generated
        Top Data (Unique):
        '__topProjection': The projection line on the top surface of the wall, represented by a sequence of dots, is automatically generated
        'toplevel': The top elevation of the wall, defined by the floor, with an elevation difference greater than 1.5 meters, is automatically generated
        'topoffset': The elevation offset of the top surface of the wall, which is less than plus or minus 1.5 meters, is automatically generated
    """
    __slots__ = ['__botProjection', '__topProjection', 'toplevel', 'topoffset', 'orientation']

    def __init__(self, model: MoosasContainer, faceId: str | list[str] | np.ndarray[str], level: float = None,
                 offset: float = None, glazingId=None,
                 glazingElement: MoosasElement | list[MoosasElement] | np.ndarray[MoosasElement] = None, space=None,
                 uid=None):
        """
        Initialize a MoosasWall instance with geometric and spatial properties.
        
        Parameters
        ----------
        model : MoosasContainer
            The container model that holds the wall and associated level information.
        faceId : str or list of str or numpy.ndarray of str
            Identifier(s) for the face(s) representing the wall geometry.
        level : float, optional
            The base level (elevation) of the wall. If not provided, inferred from geometry and model levels.
        offset : float, optional
            Vertical offset from the base level. If not provided, calculated from geometry.
        glazingId : object, optional
            Identifier for glazing elements associated with the wall. Default is None.
        glazingElement : MoosasElement or list of MoosasElement or numpy.ndarray of MoosasElement, optional
            Glazing element(s) attached to the wall. Default is None.
        space : object, optional
            Spatial context or enclosure to which the wall belongs. Default is None.
        uid : str, optional
            Unique identifier for the wall. If not provided, generated based on faceId.
        
        Returns
        -------
        None
        """
        if isinstance(faceId, MoosasGeometry):
            uid = f"wall_{faceId.faceId}" if uid is None else uid
        else:
            uid = f"wall_{mixItemListToList(faceId)[0]}" if uid is None else uid
        super(MoosasWall, self).__init__(model, faceId, level=level, offset=offset, glazingElement=glazingElement,
                                         space=space, glazingId=glazingId, uid=uid)
        pointlist = shapely.get_coordinates(self.face, include_z=True)
        self.toplevel = None
        self.isOuter = True
        self.orientation: Vector = Vector(self.normal)

        # find the bottom height of the wall
        coordinates_z = pointlist[:, 2]
        botheight = np.round(np.min(coordinates_z), 3)
        topheight = np.round(np.max(coordinates_z), 3)

        for i in range(len(model.levelList) - 1):
            # best match: the wall located between two level
            if model.levelList[i] <= botheight and model.levelList[i + 1] >= topheight:
                self.level = model.levelList[i]
                self.toplevel = model.levelList[i + 1]
                break
            # second match: the wall cross a level
            if botheight <= model.levelList[i] <= topheight:
                if model.levelList[i] - botheight < topheight - model.levelList[i]:
                    self.level = model.levelList[i]
                    self.toplevel = model.levelList[i + 1]
                else:
                    self.toplevel = model.levelList[i]
                    if i == 0:
                        self.level = model.levelList[i]
                    else:
                        self.level = model.levelList[i - 1]
                break

        # Worst match: the wall lies below/above the known levels. A level may
        # already have been supplied by RDF while its matching top level was
        # not, so resolve the two values independently before computing offsets.
        if self.level is None:
            if topheight <= model.levelList[0]:
                self.level = model.levelList[0]
            else:
                self.level = model.levelList[-1]
        if self.toplevel is None:
            # Preserve the real upper geometry when no building level brackets
            # the wall. This allows partially specified RDF to be exported to
            # IDF instead of failing with ``topheight - None``.
            self.toplevel = topheight

        self.offset = botheight - self.level
        self.topoffset = topheight - self.toplevel

        # prepare the projection for the force_2d method
        self.prepareProjection()

    @classmethod
    def fromDict(cls, elementDict, model: MoosasContainer):
        """
        Create a MoosasWall instance from a dictionary representation.
        
        Parameters
        ----------
        elementDict : dict
            Dictionary containing the element's data.
        model : MoosasContainer
            Model container to which the element belongs.
        
        Returns
        -------
        MoosasWall
            A new MoosasWall instance initialized from the provided dictionary and model.
        """
        element = super(MoosasWall, cls).fromDict(elementDict, model)
        faceElement = cls(model, element.faceId, element.glazingId)
        for fid in mixItemListToList(element.faceId):
            model.builtData.elements[fid] = faceElement
        return faceElement

    @classmethod
    def fromProjection(cls, prjLine: shapely.Geometry, bottom: float, top: float, model: MoosasContainer,
                       airBoundary=False):
        """
        Create a wall or glazing object from a 2D projection line and elevation bounds.
        
        Parameters
        ----------
        prjLine : shapely.Geometry
            A 2D line geometry representing the projection of the wall.
        bottom : float
            The bottom elevation (z-coordinate) of the wall.
        top : float
            The top elevation (z-coordinate) of the wall.
        model : MoosasContainer
            The model container to which the geometry will be added.
        airBoundary : bool, optional
            If True, creates a category-2 air-boundary wall; otherwise creates an opaque wall.
        
        Returns
        -------
        wall : cls
            An instance of the class (e.g., Wall) created from the projected geometry and added to the model.
        """
        stPoint, edPoint = shapely.get_coordinates(prjLine)
        airBound = [
            np.append(stPoint, bottom),
            np.append(edPoint, bottom),
            np.append(edPoint, top),
            np.append(stPoint, top),
            np.append(stPoint, bottom),
        ]

        idx = model.includeGeo(shapely.polygons(airBound), cat=2 if airBoundary else 0)
        return cls(model, idx)

    @property
    def is_air_boundary(self) -> bool:
        """Whether this wall is a virtual air boundary instead of an opaque wall."""
        return self.category == 2

    @classmethod
    def break_(cls, wall: MoosasWall, breakPoints: list[shapely.Geometry] | shapely.Geometry):
        """
        Break a wall into multiple segments at specified break points.
        
        Parameters
        ----------
        cls : type
            The class instance (used as part of a classmethod).
        wall : MoosasWall
            The wall object to be broken into segments. Must have 2D geometry and level/top-level attributes.
        breakPoints : list[shapely.Geometry] or shapely.Geometry
            A single point or a list of shapely geometry points where the wall should be broken.
        
        Returns
        -------
        list
            A list of new wall objects (type determined by `cls`) created by breaking the original wall at the specified points.
            If insufficient break points are provided, returns a list containing the original unbroken wall.
        """
        twins = shapely.get_coordinates(wall.force_2d())
        if len(twins) < 2:
            return [wall]
        bottom = wall.level + wall.offset
        top = wall.toplevel + wall.topoffset
        unit = Vector(twins[1] - twins[0]).unit()
        breakPoints = np.array([breakPoints]).flatten()
        breakPoints = np.append(shapely.points(twins), breakPoints)
        for i, bp in enumerate(breakPoints):
            vecBp = Vector(shapely.get_coordinates(bp)[0] - twins[0])
            breakPoints[i] = Vector(unit * Vector.dot(vecBp, unit) + Vector(twins[0])).geometry
        # breakPoints = [breakP for breakP in breakPoints if shapely.contains(wall.force_2d(), breakP)]
        breakPoints = shapely.force_2d(breakPoints)

        coor = shapely.get_coordinates(breakPoints)
        argIdx = np.lexsort((coor[:, 0], coor[:, 1]))
        st, ed = list(argIdx).index(0), list(argIdx).index(1)
        argIdx = argIdx[min(st, ed):max(st, ed) + 1]
        breakPoints = breakPoints[argIdx]

        if len(breakPoints) < 3:
            # dont need to break
            return [wall]
        # print("\nbreakFunction",cls)
        newWalls = cls.fromSeriesPoint(breakPoints, bottom, top, wall.glazingElement, wall.parent)
        for descendant in newWalls:
            descendant.settings = dict(wall.settings)
            descendant.explicit_settings = set(wall.explicit_settings)
            descendant.setCategory(wall.category)
            descendant.space = list(wall.space)
            descendant.isOuter = wall.isOuter
            descendant.shading = list(wall.shading)
            descendant.description = wall.description
        return newWalls

    @classmethod
    def fromSeriesPoint(cls, breakPoints: list[shapely.Geometry] | shapely.Geometry, bottom: float, top: float,
                        gls: list[MoosasGlazing], model: MoosasContainer) -> list[MoosasWall]:
        """
        Partition walls based on break points and reassign glazing elements.
        
        Parameters
        ----------
        breakPoints : list[shapely.Geometry] or shapely.Geometry
            A geometry or list of geometries representing the points where walls are to be split.
        bottom : float
            The bottom elevation for the generated wall segments.
        top : float
            The top elevation for the generated wall segments.
        gls : list[MoosasGlazing]
            List of glazing elements to be reassigned to the new wall segments after partitioning.
        model : MoosasContainer
            The model container that holds the glazing list and other contextual data.
        
        Returns
        -------
        list[MoosasWall]
            A list of newly created wall segments formed by partitioning at the given break points,
            with glazing elements appropriately reassigned based on spatial containment.
        """
        """partition the walls by sorting their coordinates and making polygon using the top and bottom boundaries
        the glazing of all walls will be collected and try to attach to the new wall again.
        """
        coor = list(shapely.get_coordinates(breakPoints))
        coor.sort(key=lambda x: (x[0], x[1]))

        wallNew: list[MoosasWall] = []
        for thisPoi, nextPoi in zip(coor[:-1], coor[1:]):
            if Vector(thisPoi - nextPoi).length() > geom.POINT_PRECISION:
                edges = shapely.linestrings([thisPoi, nextPoi])
                wallNew.append(cls.fromProjection(edges, bottom, top, model))

        inherited_glazing = []
        for glazing in gls:
            split_glazing = MoosasGlazing.break_(glazing, breakPoints)
            inherited_glazing.extend(split_glazing)

        for glazing in inherited_glazing:
            if glazing is not None:
                if not glazing in model.glazingList:
                    model.glazingList = list(np.append(model.glazingList, glazing))

                # Prefer the split descendants of the original parent wall first.
                parent = attach_glazing_to_candidates(glazing, wallNew)
                if parent is not None:
                    continue

                # Fall back to the rest of the model only if the inherited descendants fail.
                other_walls = [wall for wall in mixItemListToList(model.wallList) if wall not in wallNew]
                attach_glazing_to_candidates(glazing, other_walls)

        return wallNew

    @property
    def height(self):
        """
        Height of the element including glazing elements.
        
        Calculates the total height by finding the difference between the maximum top level 
        (including top offset and toplevel) and the minimum bottom level (including offset and level) 
        across the main element and all associated glazing elements.
        
        Parameters
        ----------
        self : object
            The instance of the class containing the height property. Must have attributes
            `toplevel`, `topoffset`, `level`, `offset`, and `glazingElement`. The `glazingElement`
            attribute should be an iterable of objects with `toplevel`, `topoffset`, `level`, and `offset` attributes.
        
        Returns
        -------
        float or int
            The calculated height as the difference between the highest top and lowest bottom position.
        """
        top = [self.toplevel + self.topoffset] + [g.toplevel + g.topoffset for g in self.glazingElement]
        bot = [self.level + self.offset] + [g.level + g.offset for g in self.glazingElement]
        return np.max(top) - np.min(bot)

    def prepareProjection(self):
        """
        Prepare top and bottom projections of the face geometry.
        
        Parameters
        ----------
        self : object
            The instance of the class containing the face attribute and methods.
            Must have a `face` attribute accessible via `self.face` that represents
            a geometry object compatible with shapely, and a `geom.POINT_PRECISION`
            constant for precision control.
        
        Returns
        -------
        None
            This function does not return any value. It modifies the instance attributes
            `__botProjection` and `__topProjection` by setting them to lists of projected
            2D points (with Z-coordinate filtered) at specified precision.
        """
        pointlist = shapely.get_coordinates(self.face, include_z=True)
        bottom = np.min(pointlist[:, 2])
        above = np.max(pointlist[:, 2])
        self.__botProjection = []
        for _point in pointlist:
            if np.abs(_point[2] - bottom) < geom.POINT_PRECISION:
                self.__botProjection.append(shapely.set_precision(shapely.points(_point), geom.POINT_PRECISION))
        self.__topProjection = []
        for _point in pointlist:
            if np.abs(_point[2] - above) < geom.POINT_PRECISION:
                self.__topProjection.append(shapely.set_precision(shapely.points(_point), geom.POINT_PRECISION))

    # conceptual method in the based class
    def force_2d(self, top=False, region=False) -> shapely.Geometry | None:
        """
        Project the 3D geometry into a 2D representation based on top or bottom projections.
        
        Parameters
        ----------
        top : bool
            If True, use the top projection of the geometry; otherwise, use the bottom projection.
        region : bool
            If True, combine top and bottom projections to form a 2D region (e.g., polygon or closed line);
            if False, return the 2D representation of the specified projection (top or bottom).
        
        Returns
        -------
        shapely.Geometry or None
            A 2D geometric object representing the projected line, point, or polygon;
            returns None if the projection cannot be computed.
        """
        if region:
            lBot, lTop = self.force_2d(False, False), self.force_2d(True, False)
            if not shapely.disjoint(lBot, lTop):
                return lBot
            lBot = shapely.get_coordinates(lBot).tolist()
            lTop = shapely.get_coordinates(lTop).tolist()
            if len(lTop) == 1 and len(lBot) == 1:
                return shapely.linestrings(list(lBot) + list(lTop))
            lTop.reverse()
            lbound = list(lBot) + list(lTop)
            for i in range(2, len(lbound)):
                # detect the co-linear projections
                if not Vector.parallel(Vector(Vector(lbound[1]) - Vector(lbound[0])),
                                       Vector(Vector(lbound[i]) - Vector(lbound[0]))):
                    return simplify(shapely.polygons(lbound + [lbound[0]]))
            return shapely.linestrings([lbound[0], lbound[-1]])

        else:
            if top:
                target = self.__topProjection
            else:
                target = self.__botProjection

            # invalid projection, try to use the bottom projection
            if len(target) < 2:
                if top:
                    return self.force_2d(False, False)
                else:
                    return target[0]
            botx = np.array([shapely.get_x(poi) for poi in target])
            boty = np.array([shapely.get_y(poi) for poi in target])
            if np.max(botx) == np.min(botx):
                p1 = np.array([botx[np.argmin(boty)], boty[np.argmin(boty)]])
                p2 = np.array([botx[np.argmax(boty)], boty[np.argmax(boty)]])
            else:
                p1 = np.array([botx[np.argmin(botx)], boty[np.argmin(botx)]])
                p2 = np.array([botx[np.argmax(botx)], boty[np.argmax(botx)]])
            if np.linalg.norm(p1 - p2) >= geom.POINT_PRECISION:
                return shapely.linestrings([p1, p2])
            else:
                return shapely.points(p1)


    def dissolve(self, wall: MoosasWall | list[MoosasWall]):
        """
        Merge the current MoosasWall with one or more other MoosasWall objects into a single entity.
        
        Parameters
        ----------
        wall : MoosasWall or list of MoosasWall
            The wall or walls to be merged with the current instance. If a single MoosasWall is provided,
            it is converted into a list for uniform processing.
        
        Returns
        -------
        None
            This function does not return any value.
        """
        if isinstance(wall, MoosasWall):
            wall = [wall]
        if len(wall) == 0:
            return
        """merge two faces into one MoosasWall object"""
        super().dissolve(wall)
        toplevel = max(self.toplevel, np.max([w.toplevel for w in wall]))
        self.topoffset = max(self.topoffset + self.toplevel,
                             np.max([w.topoffset for w in wall]) + np.max([w.toplevel for w in wall])) - toplevel
        self.toplevel = toplevel
        self.prepareProjection()

    def representation(self) -> shapely.Geometry:
        """
        Return a 3D polygon representation of the glazing element.
        
        Parameters
        ----------
        self : MoosasGlazing
            The instance of MoosasGlazing containing geometric and level data for generating the 3D polygon.
            Must have methods `force_2d`, `level`, `offset`, `toplevel`, and `topoffset`.
        
        Returns
        -------
        shapely.Geometry
            A 3D shapely polygon representing the vertical extrusion of the glazing element,
            constructed from bottom and top boundary coordinates.
        """
        lBot = shapely.force_3d(self.force_2d(False, False), z=self.level + self.offset)
        lTop = shapely.force_3d(self.force_2d(False, False), z=self.toplevel + self.topoffset)
        # lTop = shapely.force_3d(self.force_2d(True, False), z=self.toplevel + self.topoffset)

        lBot = shapely.get_coordinates(lBot, include_z=True).tolist()
        lTop = shapely.get_coordinates(lTop, include_z=True).tolist()
        lTop.reverse()

        return shapely.polygons(list(lBot) + list(lTop) + [lBot[0]])


class MoosasGlazing(MoosasWall):
    """
    glazing element based on MoosasWall.
    this element should only contain one geometry.

    attribute:
    parentFace: the Uid of parent MoosasWall element
    orientation: normal facing outside.
    """
    __slots__ = ['parentFace', '_shgc', '_operable']

    def __init__(self, model: MoosasContainer, faceId: str | list[str] | np.ndarray[str], level: float = None,
                 offset: float = None, glazingId=None,
                 glazingElement: MoosasElement | list[MoosasElement] | np.ndarray[MoosasElement] = None, space=None,
                 operable=0.5,
                 uid=None):
        """
        Initialize a MoosasGlazing object with geometric and structural properties.
        
        Parameters
        ----------
        model : MoosasContainer
            The container model to which the glazing belongs.
        faceId : str or list of str or numpy.ndarray of str
            Identifier(s) for the associated face(s). If a MoosasGeometry object is passed,
            its faceId is used.
        level : float, optional
            Elevation level of the bottom of the glazing. If not provided, defaults to None.
        offset : float, optional
            Vertical offset from the base level. If not provided, defaults to None.
        glazingId : any, optional
            User-defined identifier for the glazing element. Defaults to None.
        glazingElement : MoosasElement or list of MoosasElement or numpy.ndarray of MoosasElement, optional
            The glazing element(s) defining the geometry and properties. Defaults to None.
        space : any, optional
            Associated space for the glazing. Defaults to None.
        uid : str, optional
            Unique identifier for the glazing. If not provided, it is generated based on faceId.
        
        Returns
        -------
        None
            This constructor does not return a value.
        """
        if isinstance(faceId, MoosasGeometry):
            uid = f"gls_{faceId.faceId}" if uid is None else uid
        else:
            uid = f"gls_{mixItemListToList(faceId)[0]}" if uid is None else uid
        super(MoosasGlazing, self).__init__(model, faceId, level=level, offset=offset, glazingElement=glazingElement,
                                            space=space, glazingId=glazingId, uid=uid)
        self.parentFace: MoosasWall | None = None
        self._shgc: float | None = None
        self._operable: float = operable
        self.SHGC = None
        self.operable = operable
        if self.offset < -0.2:
            new_level = model.levelList[model.levelList.index(self.level) - 1]
            # print('\nMOVE!!!!!!!!!!!!!!!!!!!!!!!!!!!',new_level)
            self.offset = self.level + self.offset - new_level
            self.level = new_level
        if self.topoffset < -0.1:
            new_level = model.levelList[model.levelList.index(self.toplevel) - 1]
            self.topoffset = self.toplevel + self.topoffset - new_level
            self.toplevel = new_level

    @property
    def SHGC(self):
        return self.settings.get('shgc', self._shgc)

    @SHGC.setter
    def SHGC(self, value):
        self._shgc = value
        self.settings['shgc'] = value

    @property
    def operable(self):
        return self.settings.get('operable', self._operable)

    @operable.setter
    def operable(self, value):
        self._operable = value
        self.settings['operable'] = value

    @classmethod
    def fromProjection(cls, prjLine: shapely.Geometry, bottom: float, top: float, model: MoosasContainer,
                       airBoundary=False):
        """
        Create an instance from a projection line with defined bottom and top elevations.
        
        Parameters
        ----------
        prjLine : shapely.Geometry
            A linestring geometry representing the projection; must have sufficient length.
        bottom : float
            The bottom elevation (z-coordinate) of the generated geometry.
        top : float
            The top elevation (z-coordinate) of the generated geometry.
        model : MoosasContainer
            The model container used to include the generated geometry.
        airBoundary : bool, optional
            If True, includes an air boundary; defaults to False.
        
        Returns
        -------
        gls : cls or None
            An instance of the class created from the projection, or None if the input line is too short.
        """
        if shapely.length(prjLine) < geom.POINT_PRECISION:
            return None
        stPoint, edPoint = shapely.get_coordinates(prjLine)
        airBound = [
            np.append(stPoint, bottom),
            np.append(edPoint, bottom),
            np.append(edPoint, top),
            np.append(stPoint, top),
            np.append(stPoint, bottom),
        ]
        idx = model.includeGeo(shapely.polygons(airBound), cat=0)
        gls = cls(model, idx)
        return gls

    def force_2d(self, top=False, region=False):
        """
        Force the geometry into a 2D representation.
        
        Parameters
        ----------
        top : bool, optional
            If True, project to the top view. Default is False.
        region : bool, optional
            If True, return as a 2D region. Default is False.
        
        Returns
        -------
        object
            A 2D representation of the geometry, type depends on implementation in parent class.
        """
        return super(MoosasGlazing, self).force_2d(top, region)

"""
    All constant we use in Moosas+
"""
from .support import np


class buildingType:
    RESIDENTIAL = "居住建筑"
    OFFICE = "办公建筑"
    HOTEL = "酒店建筑"
    SCHOOL = "学校建筑"
    COMMERCIAL = "商场建筑"
    OPERA = "剧院建筑"
    HOSPITAL = "医院建筑"
    GB_T51350_2019 = "近零能耗建筑技术标准 GB/T 51350-2019"


class geom:
    LEVEL_MIN_AREA = 5.0  # minimum total floor area
    CURTAIN_MIN_OFFSET = 0.1  # Identify thresholds for curtain walls, which can speed up curtain wall identification
    HORIZONTAL_ANGLE_THRESHOLD = 45
    HORIZONTAL_ANGLE_THRESHOLD = np.cos(HORIZONTAL_ANGLE_THRESHOLD / 180 * np.pi)  # Because the point multiplication of
    # the normal vector is used to determine whether it is a horizontal plane or not, it is necessary to take sin
    PATH_MAX_DEPTH = 50  # Deep search stack is deep, too deep will affect the speed, too shallow is easy to identify
    # failure

    # Coordinate quantization and geometric recognition precision (meters).
    POINT_PRECISION = 0.001
    AREA_PRECISION = 1.0

    INCH_METER_MULTIPLIER = 0.0254
    INCH_METER_MULTIPLIER_SQR = 0.0254 * 0.0254
    # Maximum offset height (m),
    # which can also be understood as the minimum floor height
    LEVEL_MAX_OFFSET = 1.5
    LEVEL_MIN_HEIGHT = 2.2  # minimum spacing between retained storey elevations
    # validation of the room
    ROOM_MIN_AREA = 1.0
    ROOM_MIN_DIMENSION = 0.9
    PARTITION_SLIVER_MAX_WIDTH = 0.1
    PARTITION_SLIVER_MIN_ASPECT_RATIO = 20.0
    CORE_OFFSET_DEPTH = 5.0
    CORE_MIN_WIDTH = 2.0

    # angle threshold for space regulation
    REGULATION_ANGEL_THRESHOLD = 15
    REGULATION_ANGEL_THRESHOLD = REGULATION_ANGEL_THRESHOLD / 180 * np.pi

    @staticmethod
    def round(num,precision):
        """
        Round the input number or array to the specified precision using floor rounding.
        
        Parameters
        ----------
        num : array-like or scalar
            The number or array of numbers to be rounded.
        precision : float
            The precision to which the numbers are to be rounded down.
        
        Returns
        -------
        float or numpy.ndarray
            The rounded number or array, where each element is rounded down to the nearest multiple of precision.
        """
        return np.floor(np.array(num)/precision)*precision

class settings:
    PLUGIN_MENU_STRING = "Moosas"

    PLUGIN_DEBUG = True

    PAYLOAD_DELIMITER = "|"
    PAYLOAD_PARAMS_DELIMITER = "~"


class ui:
    WIN_UI_WIDTH = 2000
    WIN_UI_HEIGHT = 2000
    WIN_REPORT_UI_WIDTH = 600
    WIN_REPORT_UI_HEIGHT = 800

    MAC_UI_WIDTH = 800
    MAC_UI_HEIGHT = 900
    MAC_REPORT_UI_WIDTH = 600
    MAC_REPORT_UI_HEIGHT = 800

    UI_X = 1100
    UI_Y = 100


class meter:
    INCH_METER_MULTIPLIER = 0.0254
    INCH_METER_MULTIPLIER_SQR = 0.0254 * 0.0254
    MATERIAL_ALPHA_THRESHOLD = 0.99


class entity:
    ENTITY_WALL = 0
    ENTITY_INTERNAL_WALL = 3
    ENTITY_GLAZING = 1
    ENTITY_INTERNAL_GLAZING = 5
    ENTITY_SKY_GLAZING = 6
    ENTITY_ROOF = 2
    ENTITY_FLOOR = 4
    ENTITY_GROUND_FLOOR = 8
    ENTITY_SHADING = 16
    ENTITY_PARTY_WALL = 32
    ENTITY_DOOR = 64
    ENTITY_IGNORE = -2
    ENTITY_SURROUNDING = -1


class orientation:
    ORIENTATION_SOUTH = 0
    ORIENTATION_WEST = 1
    ORIENTATION_NORTH = 2
    ORIENTATION_EAST = 3
    ORIENTATION_ROOF = 4
    ORIENTATION_ALL = 8


class dateSetting:
    MONTH_DAY = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
    MONTH_DAY_LEAP = (31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
    MONTH_NAME = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep',
                  'Oct', 'Nov', 'Dec')


class rad:
    GROUND_REFLECTION = 0.2
    CONTENT_REFLECTION = 0.65
    DEFAULT_SHGC = 0.7


class afn:
    """Default numerical controls for the airflow and 5R1C coupling layer.

    These are solver controls rather than building properties.  Physical
    construction data belongs to Moosas elements and 5R1C coefficients belong
    to the thermal backend parameters.
    """
    TIME_STEP_SECONDS = 3600.0
    INITIAL_TEMPERATURE_C = 23.0
    PREHEAT_ROUNDS = 10
    COUPLING_MAX_ITERATIONS = 10
    TEMPERATURE_TOLERANCE_C = 0.02
    FLOW_TOLERANCE = 1e-3
    RELAXATION = 0.5
    INTERZONE_MAX_ITERATIONS = 80
    INTERZONE_TOLERANCE_C = 1e-5
    MASS_BALANCE_TOLERANCE_KG_S = 1e-4
    OCCUPANCY_ACTIVE_THRESHOLD = 1e-9

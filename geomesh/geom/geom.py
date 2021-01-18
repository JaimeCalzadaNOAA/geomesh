from geomesh.raster import Raster
from geomesh.geom.base import BaseGeom
from geomesh.geom.raster import RasterGeom


class Geom:
    """
    Factory class that creates and returns correct object type
    based on the input type
    """

    def __new__(cls, geom, **kwargs):
        """
        Input parameters
        ----------------
        geom:
            Object to use as input to compute the output mesh hull.
        """

        if isinstance(geom, Raster):
            return RasterGeom(geom, **kwargs)

        else:
            raise NotImplementedError(
                f'Argument geom must be of type {BaseGeom} or a derived type, '
                f'not type {type(geom)}.')

    @staticmethod
    def is_valid_type(geom_object):
        return isinstance(geom_object, BaseGeom)

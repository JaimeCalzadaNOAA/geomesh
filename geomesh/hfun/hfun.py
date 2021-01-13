from functools import lru_cache
import logging
import pathlib
import tempfile


from ..raster import Raster
from . import types


class Hfun:

    def __new__(cls,
                hfun, 
                hmin=None,
                hmax=None,
                ellipsoid=None,
                verbosity=0,
                interface='cmdsaw',
                nprocs=None):
        """
        Input parameters
        ----------------
        hfun: Object defining mesh size, right now only raster is
              supported
        hmin: minimum size of mesh cell
        hmax: maximum size of mesh cell
        ellipsoid:
            None, False, True, 'WGS84' or '??'
        verbosity: logger/output verbosity settings
        interface: 'cmdsaw' or 'libsaw' to be used in calling jigsawpy
        nprocs: number of processors to be used in parallel sections
                of the code
        """

        if isinstance(hfun, Raster):
            return (types._HfunRaster(hfun, hmin, hmax,
                                      nprocs, interface))
        else:
            raise NotImplementedError(
                f"Size function type {type(hfun)} is not supported!")

    @staticmethod
    def is_valid_type(hfun_object):
        # TODO: Find a cleaner way to check (combine with check in
        # __new__ and use a dict perhaps?
        valid_types = (types._HfunRaster, )
        return isinstance(hfun_object, valid_types)

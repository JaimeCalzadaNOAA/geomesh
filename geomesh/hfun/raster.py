import gc
from multiprocessing import cpu_count, Pool
import pathlib
from time import time
import warnings

import geopandas as gpd  # type: ignore[import]
from jigsawpy import jigsaw_msh_t, jigsaw_jig_t  # type: ignore[import]
from jigsawpy.libsaw import jigsaw  # type: ignore[import]
import matplotlib.pyplot as plt  # type: ignore[import]
# from matplotlib.tri import Triangulation  # type: ignore[import]
import numpy as np  # type: ignore[import]
from pyproj import CRS, Transformer  # type: ignore[import]
import rasterio  # type: ignore[import]
from scipy.spatial import cKDTree  # type: ignore[import]
from shapely import ops  # type: ignore[import]
from shapely.geometry import (  # type: ignore[import]
    LineString, MultiLineString, box, GeometryCollection)
import tempfile
from typing import Union
import utm  # type: ignore[import]

from geomesh.hfun.base import BaseHfun
from geomesh.raster import Raster, get_iter_windows
from geomesh.geom.shapely import PolygonGeom
from geomesh import utils

# supress feather warning
warnings.filterwarnings(
    'ignore', message='.*initial implementation of Parquet.*')


class HfunInputRaster:

    def __set__(self, obj, raster: Raster):
        if not isinstance(raster, Raster):
            raise TypeError(f'Argument raster must be of type {Raster}, not '
                            f'type {type(raster)}.')
        # init output raster file
        tmpfile = tempfile.NamedTemporaryFile()
        with rasterio.open(raster.tmpfile) as src:
            if raster.chunk_size is not None:
                windows = get_iter_windows(
                    src.width, src.height, chunk_size=raster.chunk_size)
            else:
                windows = [rasterio.windows.Window(
                    0, 0, src.width, src.height)]
            meta = src.meta.copy()
            meta.update({'driver': 'GTiff', 'dtype': np.float32})
            with rasterio.open(tmpfile, 'w', **meta,) as dst:
                for window in windows:
                    values = src.read(window=window).astype(np.float32)
                    values[:] = np.finfo(np.float32).max
                    dst.write(values, window=window)
        obj.__dict__['raster'] = raster
        obj._tmpfile = tmpfile
        obj._chunk_size = raster.chunk_size
        obj._overlap = raster.overlap

    def __get__(self, obj, val) -> Raster:
        return obj.__dict__['raster']


class HfunRaster(BaseHfun, Raster):

    _raster = HfunInputRaster()

    def __init__(self, raster: Raster, hmin: float = None, hmax: float = None,
                 verbosity=0):
        self._raster = raster
        self._hmin = hmin
        self._hmax = hmax
        self._verbosity = verbosity

    def msh_t(self, window: rasterio.windows.Window = None,
              verbosity=None) -> jigsaw_msh_t:

        if window is None:
            iter_windows = list(self.iter_windows())
        else:
            iter_windows = [window]
        if len(iter_windows) > 1:
            raise NotImplementedError(
                'iter_windows > 1, need to collect hfuns')
        dst_crs = None
        for window in iter_windows:

            hmat = jigsaw_msh_t()
            hmat.ndims = +2

            xgrid = np.array(
                self.get_x(window=window),
                dtype=jigsaw_msh_t.REALS_t)
            ygrid = np.array(
                self.get_y(window=window),
                dtype=jigsaw_msh_t.REALS_t)
            x0, y0, x1, y1 = self.get_window_bounds(window)

            kwargs = {}

            if self.crs.is_geographic:
                _, _, number, letter = utm.from_latlon(
                    (y0 + y1)/2, (x0 + x1)/2)
                ellipsoid = {
                    'GRS 1980': 'GRS80',
                    'WGS 84': 'WGS84'
                }[self.crs.ellipsoid.name]
                dst_crs = CRS(
                    proj='utm',
                    zone=f'{number}{letter}',
                    ellps=ellipsoid
                )
                _rast = Raster(self.tmpfile, chunk_size=self.chunk_size)
                _rast.warp(dst_crs)
                xgrid, ygrid = _rast.get_x(window), _rast.get_y(window)
                x0, y0, x1, y1 = _rast.get_window_bounds(window)
                del _rast

            self.logger.info('Forming initial hmat (euclidean-grid).')
            start = time()
            hmat.mshID = 'euclidean-grid'
            hmat.xgrid = np.array(
                xgrid,
                dtype=jigsaw_msh_t.REALS_t)
            hmat.ygrid = np.array(
                np.flip(ygrid),
                dtype=jigsaw_msh_t.REALS_t)
            hmat.value = np.array(
                np.flipud(self.get_values(window=window, band=1)),
                dtype=jigsaw_msh_t.REALS_t)
            kwargs = {'kx': 1, 'ky': 1}  # type: ignore[dict-item]
            del xgrid, ygrid
            gc.collect()
            geom = PolygonGeom(box(x0, y0, x1, y1)).geom

            self.logger.info(
                f'Initial hmat generation took {time()-start}.')

            self.logger.info('Configuring jigsaw...')

            opts = jigsaw_jig_t()

            # additional configuration options
            opts.mesh_dims = +2
            opts.hfun_scal = 'absolute'
            # no need to optimize for size function generation
            opts.optm_tria = False

            opts.hfun_hmin = np.min(hmat.value) if self.hmin is None else \
                self.hmin
            opts.hfun_hmax = np.max(hmat.value) if self.hmax is None else \
                self.hmax
            opts.verbosity = self.verbosity if verbosity is None else \
                verbosity

            # output mesh
            output_mesh = jigsaw_msh_t()

            # call jigsaw to create local mesh
            jigsaw(
                opts,
                geom,
                output_mesh,
                hfun=hmat
            )

            # do post processing
            utils.cleanup_isolates(output_mesh)
            utils.interpolate(hmat, output_mesh, **kwargs)

            if dst_crs is not None:
                utils.reproject(output_mesh, dst_crs, self.crs)

        return output_mesh

    def add_contour(
            self,
            level: float,
            expansion_rate: float,
            target_size: float = None,
            nprocs: int = None,
    ):
        """ See https://outline.com/YU7nSM for an excellent explanation about
        tree algorithms.
        """
        # check target size
        target_size = self.hmin if target_size is None else target_size
        if target_size is None:
            raise ValueError('Argument target_size must be specified if no '
                             'global hmin has been set.')
        if target_size <= 0:
            raise ValueError("Argument target_size must be greater than zero.")
        contours = self._get_raster_contours(level)
        if isinstance(contours, GeometryCollection):
            self.logger.info('No contours found...')
            return
        self.logger.info('Adding contours as features...')
        self.add_feature(contours, target_size, expansion_rate, nprocs)

    def add_feature(
            self,
            feature: Union[LineString, MultiLineString],
            target_size: float,
            expansion_rate: float,
            nprocs=None,
    ):
        '''Adds a linear distance size function constraint to the mesh.

        Arguments:
            feature: shapely.geometryLineString or MultiLineString

        https://gis.stackexchange.com/questions/214261/should-we-always-calculate-length-and-area-in-lat-lng-to-get-accurate-sizes-leng

        "Creating a local projection allowed us to have similar area/length
        calculations as if we was using great circle calculations."

        TODO: Consider using BallTree with haversine or Vincenty metrics
        instead of a locally projected window.
        '''

        # Check nprocs
        nprocs = -1 if nprocs is None else nprocs
        nprocs = cpu_count() if nprocs == -1 else nprocs
        self.logger.debug(f'Using nprocs={nprocs}')
        if not isinstance(feature, (LineString, MultiLineString)):
            raise TypeError(
                f'Argument feature must be of type {LineString} or '
                f'{MultiLineString}, not type {type(feature)}.')

        if target_size <= 0.:
            raise ValueError('Argument target_size must be > 0.')
        ellipsoid = {
            'GRS 1980': 'GRS80',
            'WGS 84': 'WGS84'
            }[self.crs.ellipsoid.name]
        tmpfile = tempfile.NamedTemporaryFile()
        meta = self.src.meta.copy()
        meta.update({'driver': 'GTiff'})
        with rasterio.open(tmpfile, 'w', **meta,) as dst:
            iter_windows = list(self.iter_windows())
            tot = len(iter_windows)
            for i, window in enumerate(iter_windows):
                self.logger.debug(f'Processing window {i+1}/{tot}.')
                if self.crs.is_geographic:
                    x0, y0, x1, y1 = self.get_window_bounds(window)
                    _, _, number, letter = utm.from_latlon(
                        (y0+y1)/2, (x0+x1)/2)
                    dst_crs = CRS(
                        proj='utm',
                        zone=f'{number}{letter}',
                        ellps=ellipsoid
                    )
                else:
                    dst_crs = None

                # transformed_features = box(*bounds).intersection(feature)
                self.logger.info('Resampling features...')
                start = time()
                with Pool(processes=nprocs) as pool:
                    transformed_features = pool.starmap(
                        transform_features,
                        [(feat, target_size, self.src.crs, dst_crs) for
                         feat in feature]
                    )
                self.logger.info(f'Resampling features took {time()-start}.')
                self.logger.info('Concatenating points...')
                start = time()
                points = []
                for geom in transformed_features:
                    if isinstance(geom, LineString):
                        points.extend(geom.coords)
                    elif isinstance(geom, MultiLineString):
                        for linestring in geom:
                            points.extend(linestring.coords)
                self.logger.info(f'Point concatenation took {time()-start}.')

                self.logger.info('Generating KDTree...')
                start = time()
                tree = cKDTree(np.array(points))
                self.logger.info(f'Generating KDTree took {time()-start}.')
                xy = self.get_xy(window)
                self.logger.info('Transform points to local CRS...')
                start = time()
                if dst_crs is not None:
                    transformer = Transformer.from_crs(
                        self.src.crs, dst_crs, always_xy=True)
                    xy = np.vstack(transformer.transform(xy[:, 0], xy[:, 1])).T
                self.logger.info(f'Transforming points took {time()-start}.')
                self.logger.info('Querying KDTree...')
                start = time()
                distances, _ = tree.query(xy, n_jobs=nprocs)
                self.logger.info(f'Querying KDTree took {time()-start}.')
                values = expansion_rate*target_size*distances + target_size
                values = values.reshape(window.height, window.width).astype(
                        self.dtype(1))
                if self.hmin is not None:
                    values[np.where(values < self.hmin)] = self.hmin
                if self.hmax is not None:
                    values[np.where(values > self.hmax)] = self.hmax
                values = np.minimum(self.get_values(window=window), values)
                self.logger.info(f'Write array to file {tmpfile.name}...')
                start = time()
                dst.write_band(1, values, window=window)
                self.logger.info(f'Write array to file took {time()-start}.')
        self._tmpfile = tmpfile

    def add_subtidal_flow_limiter(
            self,
            hmin=None,
            hmax=None,
            upper_bound=None,
            lower_bound=None
    ):
        raise NotImplementedError(
            'Needs revision for consistency with updated API.')
        hmin = np.finfo(np.float32).eps if hmin is None else hmin
        if not self._src.crs.is_geographic:
            dx = np.abs(self._src.dx)
            dy = np.abs(self._src.dy)

        meta = self._src._src.meta.copy()

        tmpfile = tempfile.NamedTemporaryFile(prefix=str(tmpdir) + '/')

        with rasterio.open(tmpfile.name, 'w', **meta) as dst:
            for window, bounds in self._src:
                topobathy = self._raster.get_values(band=1, window=window)
                if self._src.crs.is_geographic:
                    west, south, east, north = bounds
                    _bounds = np.array([[east, south], [west, north]])
                    _x, _y = utm.from_latlon(_bounds[:, 1], _bounds[:, 0])[:2]
                    dx = np.diff(np.linspace(_x[0], _x[1], window.width))[0]
                    dy = np.diff(np.linspace(_y[0], _y[1], window.height))[0]
                _dx, _dy = np.gradient(topobathy, dx, dy)
                with warnings.catch_warnings():
                    # in case self._src.values is a masked array
                    warnings.simplefilter("ignore", category=RuntimeWarning)
                    dh = np.sqrt(_dx**2 + _dy**2)
                dh = np.ma.masked_equal(dh, 0.)
                values = np.abs((1./3.)*(topobathy/dh))
                values = values.filled(np.max(values))

                if upper_bound is not None:
                    values[np.where(
                        topobathy > upper_bound)] = self._src.nodata
                if lower_bound is not None:
                    values[np.where(
                        topobathy < lower_bound)] = self._src.nodata
                values[np.where(values < hmin)] = hmin
                if self._hmin is not None:
                    values[np.where(values < self._hmin)] = self._hmin
                if self._hmax is not None:
                    values[np.where(values > self._hmax)] = self._hmax
                values = np.minimum(
                    self._src.get_values(band=1, window=window),
                    values).reshape((1, *values.shape)).astype(meta['dtype'])
                dst.write(values, window=window)
        self._tmpfile = tmpfile

    def _get_raster_contours(
            self,
            level: float,
            window: rasterio.windows.Window = None
    ):
        self.logger.debug(
            f'RasterHfun.get_raster_contours(level={level}, window={window})')
        tmpdir = tempfile.TemporaryDirectory()
        self.logger.info(f'Opened temporary directory: {tmpdir.name}')
        if window is None:
            iter_windows = list(self.raster.iter_windows())
        else:
            iter_windows = [window]
        feathers = []
        total_windows = len(iter_windows)
        self.logger.debug(f'Total windows to process: {total_windows}.')
        for i, window in enumerate(iter_windows):
            self.logger.debug(f'Processing window {i+1}/{total_windows}.')
            if self.crs.is_geographic:
                _, _, number, letter = utm.from_latlon(
                    (y0 + y1)/2, (x0 + x1)/2)
                ellipsoid = {
                    'GRS 1980': 'GRS80',
                    'WGS 84': 'WGS84'
                }[self.crs.ellipsoid.name]
                dst_crs = CRS(
                    proj='utm',
                    zone=f'{number}{letter}',
                    ellps=ellipsoid
                )
                _rast = Raster(self.tmpfile, chunk_size=self.chunk_size)
                _rast.warp(dst_crs)
                x, y = _rast.get_x(window), _rast.get_y(window)
                x0, y0, x1, y1 = _rast.get_window_bounds(window)
                del _rast
            features = []
            values = self.raster.get_values(band=1, window=window)
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', UserWarning)
                self.logger.debug('Computing contours...')
                start = time()
                ax = plt.contour(x, y, values, levels=[level])
                self.logger.debug(f'Took {time()-start}...')
                plt.close(plt.gcf())
            for path_collection in ax.collections:
                for path in path_collection.get_paths():
                    try:
                        features.append(LineString(path.vertices))
                    except ValueError:
                        # LineStrings must have at least 2 coordinate tuples
                        pass
            if len(features) > 0:
                tmpfile = pathlib.Path(tmpdir.name) / pathlib.Path(
                        tempfile.NamedTemporaryFile(suffix='.feather').name
                        ).name
                self.logger.debug('Saving feather.')
                gpd.GeoDataFrame(
                    [{'geometry': ops.linemerge(features)}]
                    ).to_feather(tmpfile)
                feathers.append(tmpfile)
        self.logger.debug('Concatenating feathers.')
        features = []
        out = gpd.GeoDataFrame()
        for feather in feathers:
            out = out.append(gpd.read_feather(feather), ignore_index=True)
            feather.unlink()
            for geometry in out.geometry:
                if isinstance(geometry, LineString):
                    geometry = MultiLineString([geometry])
            for linestring in geometry:
                features.append(linestring)
        self.logger.debug('Merging features.')
        return ops.linemerge(features)

    @property
    def raster(self):
        return self._raster

    @property
    def output(self):
        return self

    @property
    def hmin(self):
        return self._hmin

    @property
    def hmax(self):
        return self._hmax

    @property
    def verbosity(self):
        return self._verbosity

    @verbosity.setter
    def verbosity(self, verbosity: int):
        self._verbosity = verbosity


def transform_features(
    feature: Union[LineString, MultiLineString],
    target_size: float,
    src_crs: CRS = None,
    dst_crs: CRS = None
):
    if isinstance(feature, LineString):
        feature = MultiLineString([feature])
    features = []
    for linestring in feature:
        distances = [0.]
        if dst_crs is not None:
            transformer = Transformer.from_crs(
                src_crs, dst_crs, always_xy=True)
            linestring = ops.transform(transformer.transform, linestring)
        while distances[-1] + target_size < linestring.length:
            distances.append(distances[-1] + target_size)
        distances.append(linestring.length)
        linestring = LineString([
            linestring.interpolate(distance)
            for distance in distances
            ])
        features.append(linestring)
    return ops.linemerge(features)


















# def transform_raster_to_utm(raster, nprocs=-1):
#     x0 = np.min(raster.get_x())
#     y0 = np.min(raster.get_y())
#     _, _, number, letter = utm.from_latlon(x0, y0)
#     _raster = Raster(raster.path)
#     _raster.chunk_size = raster.chunk_size
#     _raster.overlap = raster.overlap
#     _raster.warp(
#         f"+proj=utm +zone={number}{letter}, "
#         "+ellps=WGS84 +datum=WGS84 +units=m +no_defs",
#         nprocs=nprocs
#         )
#     return _raster

# meta = self.raster.meta.copy()
# if self._src.crs.is_geographic:
#     with rasterio.open(tmpfile.name, 'w', **meta) as dst:
#         for window in self._src.iter_windows():
#             xy = self._src.get_xy(window)
#             _tx, _ty, zone, _ = utm.from_latlon(xy[:, 1], xy[:, 0])
#             dst_crs = CRS(proj='utm', zone=zone, ellps='WGS84')
#             transformer = Transformer.from_crs(
#                 self._src.crs, dst_crs, always_xy=True)
#             res = []
#             for linestring in feature:
#                 distances = [0]
#                 linestring = transform(
#                     transformer.transform, linestring)
#                 while distances[-1] + target_size < linestring.length:
#                     distances.append(distances[-1] + target_size)
#                 distances.append(linestring.length)
#                 linestring = LineString([
#                     linestring.interpolate(distance)
#                     for distance in distances
#                     ])
#                 res.extend(linestring.coords)
#             tree = cKDTree(np.vstack(res))
#             values = tree.query(
#                 np.vstack([_tx, _ty]).T, n_jobs=self._nprocs)[0]
#             values = expansion_rate*target_size*values + target_size
#             values = values.reshape(
#                 (1, window.height, window.width)).astype(meta['dtype'])
#             if self._hmin is not None:
#                 values[np.where(values < self._hmin)] = self._hmin
#             if self._hmax is not None:
#                 values[np.where(values > self._hmax)] = self._hmax
#             values = np.minimum(
#                 self._src.get_values(window=window), values)
#             dst.write(values, window=window)

#     else:  # is not geographic

#         # resample linestrings
#         res = []
#         for linestring in feature:
#             distances = [0]
#             while distances[-1] + target_size < linestring.length:
#                 distances.append(distances[-1] + target_size)
#             distances.append(linestring.length)
#             linestring = LineString([
#                 linestring.interpolate(distance)
#                 for distance in distances
#                 ])
#             res.extend(linestring.coords)
#         # DO KDTree
#         tree = cKDTree(np.vstack(res))
#         with rasterio.open(tmpfile.name, 'w', **meta) as dst:
#             for i, window in enumerate(self._src.iter_windows()):
#                 values = tree.query(
#                     self._src.get_xy(window),
#                     n_jobs=self._nprocs)[0]
#                 values = expansion_rate*target_size*values + target_size
#                 dst.write(
#                     np.minimum(
#                         self._src.get_values(window=window),
#                         values.reshape(
#                             (1, window.height, window.width))
#                         ).astype(meta['dtype']),
#                     window=window)
#     self._tmpfile = tmpfile

# from multiprocessing import Pool, cpu_count
# import pathlib
# import tempfile
# import warnings

# from jigsawpy import jigsaw_msh_t, jigsaw_jig_t
# from jigsawpy import libsaw
# import matplotlib as mpl
# import matplotlib.pyplot as plt
# import numpy as np
# from pyproj import CRS
# from pyproj.transformer import Transformer
# import rasterio
# from scipy.spatial import cKDTree
# from shapely.ops import transform
# from shapely.geometry import LineString, MultiLineString
# import utm

# from geomesh.figures import figure as _figure
# from geomesh.raster import Raster
# from geomesh.geom import Geom
# from geomesh.hfun.base import BaseHfun


# tmpdir = pathlib.Path(tempfile.gettempdir()+'/geomesh') / 'hfun'
# tmpdir.mkdir(parents=True, exist_ok=True)


# def _jigsaw_hmat_worker(path, window, hmin, hmax, geom):

#     # TODO: Check for is_geographic on crs before passing to utm package
#     raise NotImplementedError

#     geom = None
#     raster = Raster(path)

#     x = raster.get_x(window)
#     y = raster.get_y(window)
#     _y = np.repeat(np.min(y), len(x))
#     _x = np.repeat(np.min(x), len(y))
#     _tx = utm.from_latlon(_y, x)[0]
#     _ty = np.flip(utm.from_latlon(y, _x)[1])
#     hmat = jigsaw_msh_t()
#     hmat.mshID = "euclidean-grid"
#     hmat.ndims = +2
#     hmat.xgrid = _tx.astype(jigsaw_msh_t.REALS_t)
#     hmat.ygrid = _ty.astype(jigsaw_msh_t.REALS_t)
#     # TODO: We always get band = 1, so we should make sure the raster's
#     # gaussian_filter will write the filtered band into band 1
#     hmat.value = np.flipud(
#             raster.get_values(band=1, window=window)
#             ).astype(jigsaw_msh_t.REALS_t)

#     # init opts
#     opts = jigsaw_jig_t()

#     # additional configuration options
#     opts.verbosity = 1
#     opts.mesh_dims = 2
#     opts.hfun_scal = 'absolute'
#     opts.optm_tria = True

#     if hmin is not None:
#         opts.hfun_hmin = hmin
#     else:
#         opts.hfun_hmin = np.min(hmat.value)

#     if hmax is not None:
#         opts.hfun_hmax = hmax
#     else:
#         opts.hfun_hmax = np.max(hmat.value)

#     # output mesh
#     mesh = jigsaw_msh_t()

#     # call jigsaw to create local mesh
#     libsaw.jigsaw(
#         opts,
#         geom,
#         mesh,
#         hfun=hmat
#     )
#     breakpoint()
#     return mesh


# class HfunRaster(BaseHfun):

#     def __init__(self,
#                  raster,
#                  hmin=None,
#                  hmax=None,
#                  nprocs=None,
#                  interface='cmdsaw'):

#         self._raster = raster
#         self._nprocs = nprocs
#         self._hmin = hmin
#         self._hmax = hmax

#     def __iter__(self):
#         for i, window in enumerate(self._src.iter_windows()):
#             x = self._src.get_x(window)
#             y = self._src.get_y(window)
#             values = self._src.get_values(window=window)
#             yield x, y, values

#     @_figure
#     def contourf(self, *args, **kwargs):
#         plt.contourf(self._src.x, self._src.y, self._src.values)

#     def get_mesh(self, geom=None):
#         if geom is not None:
#             if not isinstance(geom, Geom):
#                 raise TypeError(f"geom must be of type {Geom}")

#         mesh = _jigsaw_hmat_worker(
#             self._src._tmpfile,
#             list(self._src.iter_windows())[0],
#             self._hmin,
#             self._hmax,
#             geom.geom
#             )
#         exit()
#         # vert2 = list()
#         # tria3 = list()
#         # value = list()

#         # if self._nprocs > 1:
#         #     _job_args = []
#         #     for window in self._src.iter_windows():
#         #         _args = []
#         #         _args.append(self._src._tmpfile)
#         #         _args.append(window)
#         #         _args.append(self._hmin)
#         #         _args.append(self._hmax)
#         #         _args.append(geom)
#         #         _job_args.append(_args)
#         #     print(len(_job_args))
#         #     with Pool(processes=self._nprocs) as pool:
#         #         res = pool.starmap(_jigsaw_hmat_worker, _job_args)
#         #     pool.join()
#         #     for mesh in res:
#         #         print(mesh)
#         #     breakpoint()
#         #     exit()

#         # for i in range(len(self.raster_collection)):
#         #     if self._interface == 'libsaw':
#         #         # libsaw segfaults randomly when passing hmat.
#         #         # cause is unknown. Set self._interface = 'cmdsaw'
#         #         # to avoid this issue.
#         #         mesh = self._generate_raster_hfun_libsaw(i)
#         #     elif self._interface == 'cmdsaw':
#         #         mesh = self._generate_raster_hfun_cmdsaw(i)
#         #     for index, id_tag in mesh.tria3:
#         #         tria3.append(((index + len(vert2)), id_tag))
#         #     for coord, id_tag in mesh.vert2:
#         #         vert2.append((coord, id_tag))
#         #     for val in mesh.value:
#         #         value.append(val)
#         # hfun = jigsaw_msh_t()
#         # hfun.ndims = +2
#         # hfun.mshID = "euclidean-mesh"
#         # hfun.vert2 = np.array(vert2, dtype=jigsaw_msh_t.VERT2_t)
#         # hfun.tria3 = np.array(tria3, dtype=jigsaw_msh_t.TRIA3_t)
#         # hfun.value = np.array(
#         #     # np.array(value).reshape(len(value), 1),
#         #     np.array(value),
#         #     dtype=jigsaw_msh_t.REALS_t)
#         # return hfun

#     @property
#     def _src(self):
#         try:
#             return self.__src
#         except AttributeError:
#             pass
#         raster = Raster(
#             self._raster._tmpfile,
#             chunk_size=self._raster.chunk_size)
#         raster.overlap = 0
#         tmpfile = tempfile.NamedTemporaryFile(prefix=str(tmpdir) + '/')
#         meta = raster._src.meta.copy()
#         nodata = np.finfo(rasterio.float32).max
#         meta.update({
#             "dtype": rasterio.float32,
#             "nodata": nodata
#             })
#         with rasterio.open(tmpfile.name, 'w', **meta) as dst:
#             for i, window in enumerate(raster.iter_windows()):
#                 dst.write(
#                     np.full((1, window.height, window.width), nodata),
#                     window=window)
#         self._tmpfile = tmpfile
#         return self.__src

#     @property
#     def hfun(self):
#         '''Return a jigsaw_msh_t object representing the mesh size'''
#         raster = self._src
#         x = raster.get_x()
#         y = np.flip(raster.get_y())
#         _tx = x
#         _ty = y
#         if self._src.crs.is_geographic:
#             _y = np.repeat(np.min(y), len(x))
#             _x = np.repeat(np.min(x), len(y))
#             _tx = utm.from_latlon(_y, x)[0]
#             _ty = utm.from_latlon(y, _x)[1]

#         hmat = jigsaw_msh_t()
#         hmat.mshID = "euclidean-grid"
#         hmat.ndims = +2
#         hmat.xgrid = _tx.astype(jigsaw_msh_t.REALS_t)
#         hmat.ygrid = _ty.astype(jigsaw_msh_t.REALS_t)

#         # TODO: Values of band=1 are only used. Make sure the
#         # raster's gaussian_filter writes the relevant values into
#         # band 1, or change the hardcoded band id here
#         hmat.value = np.flipud(
#                 raster.get_values(band=1)
#                 ).astype(jigsaw_msh_t.REALS_t)

#         return hmat

#     @property
#     def _raster(self):
#         return self.__raster

#     @_raster.setter
#     def _raster(self, raster):
#         assert isinstance(raster,  Raster)
#         self.__raster = raster

#     @property
#     def _tmpfile(self):
#         return self.__tmpfile

#     @_tmpfile.setter
#     def _tmpfile(self, tmpfile):
#         try:
#             del(self.__src)
#         except AttributeError:
#             pass
#         self.__src = Raster(tmpfile.name, chunk_size=self._raster.chunk_size)
#         self.__tmpfile = tmpfile

# res = []
# if nprocs > 1:
#     # _old_backend = mpl.get_backend()
#     # mpl.use('agg')
#     _job_args = []
#     for window in self._src.iter_windows():
#         _job_args.append(
#             (self._raster._tmpfile, window, level, target_size))
#     with Pool(processes=self._nprocs) as pool:
#         _res = pool.starmap(get_raster_contours, _job_args)
#     pool.join()
#     # mpl.use(_old_backend)
#     for items in _res:
#         res.extend(items)
# else:
#     for window in self._src.iter_windows():
#         res.extend(
#             get_raster_contours(
#                 self._raster._tmpfile, window, level, target_size))

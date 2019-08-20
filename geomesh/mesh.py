import numpy as np
import os
import uuid
from copy import deepcopy
from pathlib import Path
from collections import OrderedDict
from scipy.interpolate import griddata
from osgeo import osr, gdal, ogr
import matplotlib.pyplot as plt
from matplotlib.tri import Triangulation
from scipy.interpolate import RectBivariateSpline
from geomesh import gdal_tools
from geomesh.pslg import PlanarStraightLineGraph \
    as _PlanarStraightLineGraph


class UnstructuredMesh:

    def __init__(self, vertices, elements, values=None, SpatialReference=None):
        self._vertices = vertices
        self._elements = elements
        self._SpatialReference = SpatialReference
        self._values = values
        self.__ocean_boundaries = OrderedDict()
        self.__land_boundaries = OrderedDict()
        self.__inner_boundaries = OrderedDict()
        self.__inflow_boundaries = OrderedDict()
        self.__outflow_boundaries = OrderedDict()
        self.__weir_boundaries = OrderedDict()
        self.__culvert_boundaries = OrderedDict()

    def get_x(self, SpatialReference=None):
        """ """
        return self.get_xy(SpatialReference)[:, 0]

    def get_y(self, SpatialReference=None):
        """ """
        return self.get_xy(SpatialReference)[:, 1]

    def get_xy(self, SpatialReference=None):
        return self.transform_vertices(self.xy, self.SpatialReference,
                                       SpatialReference)

    def get_xyz(self, SpatialReference=None):
        xy = self.transform_vertices(self.xy, self.SpatialReference,
                                     SpatialReference)
        return np.hstack([xy, self.values])

    def get_extent(self, SpatialReference=None):
        xy = self.get_xy(SpatialReference)
        return (np.min(xy[:, 0]), np.max(xy[:, 0]),
                np.min(xy[:, 1]), np.max(xy[:, 1]))

    def add_attribute(self, name):
        if self.has_attribute(name):
            raise AttributeError(
                'Non-unique attribute name: '
                + 'Attribute attribute name already exists.')
        else:
            self.__attributes[name] = None

    def has_attribute(self, name):
        if name in self.__attributes.keys():
            return True
        else:
            return False

    def get_attribute(self, name):
        if not self.has_attribute(name):
            raise AttributeError('Attribute {} not set.'.format(name))
        return self.__attributes[name]

    def set_attribute(self, name, values, elements=False):
        if name not in self.get_attribute_names():
            raise AttributeError(
                'Cannot set attribute: {} is not an attribute.'.format(name))
        values = np.asarray(values)
        assert isinstance(elements, bool)
        if elements:
            assert values.shape[0] == self.elements.shape[0]
        else:
            assert values.shape[0] == self.vertices.shape[0]

        self.__attributes[name] = values

    def remove_attribute(self, name):
        if name in self.get_attribute_names():
            self.__attributes.pop(name)
        else:
            raise AttributeError(
                'Cannot remove attribute: attribute does not exist.')

    def get_attribute_names(self):
        return list(self.__attributes.keys())

    def transform_vertices(
        self,
        vertices,
        input_SpatialReference,
        output_SpatialReference=None
    ):
        if isinstance(input_SpatialReference, int):
            EPSG = input_SpatialReference
            input_SpatialReference = osr.SpatialReference()
            input_SpatialReference.ImportFromEPSG(EPSG)
        assert isinstance(input_SpatialReference, osr.SpatialReference)
        if output_SpatialReference is None:
            if self.SpatialReference is not None:
                output_SpatialReference = self.SpatialReference
            else:
                raise Exception('Cannot transform vertices, mesh has not spatial reference!')
        assert isinstance(output_SpatialReference, osr.SpatialReference)
        if not output_SpatialReference.IsSame(input_SpatialReference):
            CoordinateTransform = osr.CoordinateTransformation(
                                                    input_SpatialReference,
                                                    output_SpatialReference)
            vertices = [(x, y) for x, y in vertices]
            vertices = CoordinateTransform.TransformPoints(vertices)
            vertices = np.asarray([(x, y) for x, y, _ in vertices])
        return vertices

    def interpolate(self, Dataset):
        assert isinstance(Dataset, gdal.Dataset)
        if not self.SpatialReference.IsSame(
                    gdal_tools.get_SpatialReference(Dataset)):
            Dataset = gdal_tools.Warp(Dataset, dstSRS=self.SpatialReference)
        x, y, z = gdal_tools.get_arrays(Dataset)
        bbox = gdal_tools.get_Bbox(Dataset)
        f = RectBivariateSpline(x, y, z.T, bbox=[bbox.xmin, bbox.xmax,
                                                 bbox.ymin, bbox.ymax])
        idxs = np.where(np.logical_and(
                            np.logical_and(
                                bbox.xmin <= self.vertices[:, 0],
                                bbox.xmax >= self.vertices[:, 0]),
                            np.logical_and(
                                bbox.ymin <= self.vertices[:, 1],
                                bbox.ymax >= self.vertices[:, 1])))[0]
        values = f.ev(self.vertices[idxs, 0], self.vertices[idxs, 1])
        new_values = self.values.copy()
        for i, idx in enumerate(idxs):
            new_values[idx] = values[i]
        self._values = new_values

    def compute_planar_straight_line_graph(self):
        unique_edges = list()
        for i, elements in enumerate(self.mpl_tri.neighbors):
            for j, element_idx in enumerate(elements):
                if element_idx == -1:
                    unique_edges.append((self.mpl_tri.triangles[i, j],
                                         self.mpl_tri.triangles[i, (j+1) % 3]))
        # sort the unique edges into a collection of rings.
        ring_collection = list()
        ring = [unique_edges.pop(0)]
        while len(unique_edges) > 0:
            idx = np.where(ring[-1][1] == np.asarray(unique_edges)[:, 0])
            try:
                ring.append(unique_edges.pop(idx[0][0]))
            except IndexError:
                ring_collection.append(np.asarray(ring))
                ring = [unique_edges.pop(0)]
        # sort between outer and inner vertices
        geom_collection = list()
        for ring in ring_collection:
            _geom = ogr.Geometry(ogr.wkbLinearRing)
            _geom.AssignSpatialReference(self.SpatialReference)
            for idx in ring:
                _geom.AddPoint_2D(self.x[idx[0]], self.y[idx[0]])
            geom_collection.append(_geom)
        lengths = [_geom.Length() for _geom in geom_collection]
        outer_edges = ring_collection.pop(
                np.where(np.max(lengths) == lengths)[0][0])
        inner_edges = ring_collection
        outer_vertices = self.vertices[outer_edges[:, 0]]
        inner_vertices = [self.vertices[ring[:, 0]] for ring in inner_edges]
        return _PlanarStraightLineGraph(
                self.SpatialReference, outer_vertices, *inner_vertices,
                outer_edges=outer_edges, inner_edges=inner_edges)

    def has_invalid(self):
        return np.any(np.isnan(self.values))

    def fix_invalid(self, method='nearest'):
        if self.has_invalid():
            if method == 'nearest':
                idx = np.where(~np.isnan(self.values))
                _idx = np.where(np.isnan(self.values))
                values = griddata(
                    (self.x[idx], self.y[idx]), self.values[idx],
                    (self.x[_idx], self.y[_idx]), method='nearest')
                new_values = self.values.copy()
                for i, idx in enumerate(_idx):
                    new_values[idx] = values[i]
                self._values = new_values
                return self.values
            else:
                raise NotImplementedError

    def write_gr3(self, path, overwrite=False):
        if path is not None:
            path = str(Path(path))
            if os.path.isfile(path) and not overwrite:
                raise Exception(
                    'File exists, pass overwrite=True to allow overwrite.')
            else:
                with open(path, 'w') as f:
                    f.write(self.gr3)
        else:
            print(self.gr3)

    def make_plot(self, show=False, levels=256):
        z = np.ma.masked_invalid(self.values)
        vmin, vmax = z.min(), z.max()
        z = z.filled(fill_value=-99999.)
        if isinstance(levels, int):
            levels = np.linspace(vmin, vmax, levels)
        plt.tricontourf(self.mpl_tri, z, levels=levels)
        plt.gca().axis('scaled')
        if show:
            plt.show()
        plt.gca().axis('scaled')
        return plt.gca()

    @property
    def vertices(self):
        return self.__vertices

    @property
    def elements(self):
        return self.__elements

    @property
    def values(self):
        if not hasattr(self, "__attributes"):
            self.__attributes = dict()
        if not self.has_attribute("__values"):
            self.add_attribute("__values")
            self.set_attribute("__values", self.__values)
        return self.__attributes["__values"]

    @property
    def x(self):
        return self.vertices[:, 0]

    @property
    def y(self):
        return self.vertices[:, 1]

    @property
    def xy(self):
        return self.vertices

    @property
    def xyz(self):
        return self.get_xyz()

    @property
    def planar_straight_line_graph(self):
        try:
            return self.__planar_straight_line_graph
        except AttributeError:
            self.__planar_straight_line_graph \
                = self.compute_planar_straight_line_graph()
            return self.__planar_straight_line_graph

    @property
    def mpl_tri(self):
        try:
            return self.__mpl_tri
        except AttributeError:
            self.__mpl_tri = Triangulation(self.x, self.y, self.elements)
            return self.__mpl_tri

    @property
    def SpatialReference(self):
        return self.__SpatialReference

    @property
    def ndim(self):
        return 2

    @property
    def num_elements(self):
        return self.elements.shape[0]

    @property
    def num_nodes(self):
        return self.vertices.shape[0]

    @property
    def node_id(self):
        if not hasattr(self, "__node_id"):
            self.__node_id = np.arange(1, len(self.values)+1)
        return self.__node_id

    @property
    def element_id(self):
        if not hasattr(self, "__element_id"):
            self.__element_id = np.arange(1, len(self.elements)+1)
        return self.__element_id

    @property
    def ocean_boundary(self):
        ocean_boundary = list()
        for boundary in self.ocean_boundaries.values():
            for idx in boundary:
                ocean_boundary.append(idx)
        return ocean_boundary

    @property
    def land_boundary(self):
        land_boundary = list()
        for boundary in self.land_boundaries.values():
            for idx in boundary['indexes']:
                land_boundary.append(idx)
        return land_boundary

    @property
    def inner_boundary(self):
        inner_boundary = list()
        for key, boundary in self.inner_boundaries.items():
            for idx in boundary:
                inner_boundary.append(idx)
        return inner_boundary

    @property
    def inflow_boundary(self):
        inflow_boundary = list()
        for key, boundary in self.inflow_boundaries.items():
            for idx in boundary:
                inflow_boundary.append(idx)
        return inflow_boundary

    @property
    def outflow_boundary(self):
        outflow_boundary = list()
        for key, boundary in self.outflow_boundaries.items():
            for idx in boundary:
                outflow_boundary.append(idx)
        return outflow_boundary

    @property
    def weir_boundary(self):
        weir_boundary = list()
        for key, boundary in self.weir_boundaries.items():
            for idx in boundary:
                weir_boundary.append(idx)
        return weir_boundary

    @property
    def culvert_boundary(self):
        culvert_boundary = list()
        for key, boundary in self.culvert_boundaries.items():
            for idx in boundary:
                culvert_boundary.append(idx)
        return culvert_boundary

    @property
    def ocean_boundaries(self):
        ocean_boundaries = dict()
        __ocean_boundaries = deepcopy(self.__ocean_boundaries)
        for key, _ in __ocean_boundaries.items():
            indexes = np.where(_.pop('__bool'))[0].tolist()
            ocean_boundaries[key] = self.__get_ordered_indexes(indexes)
        return ocean_boundaries

    @property
    def land_boundaries(self):
        land_boundaries = dict()
        __land_boundaries = deepcopy(self.__land_boundaries)
        for key, _ in __land_boundaries.items():
            land_boundaries[key] = {
                'indexes': list(np.where(_.pop('__bool'))[0]), **_}
        return land_boundaries

    @property
    def inner_boundaries(self):
        inner_boundaries = dict()
        __inner_boundaries = deepcopy(self.__inner_boundaries)
        for key, _ in __inner_boundaries.items():
            inner_boundaries[key] = {
                'indexes': list(np.where(_.pop('__bool'))[0]), **_}
        return inner_boundaries

    @property
    def inflow_boundaries(self):
        inflow_boundaries = dict()
        __inflow_boundaries = deepcopy(self.__inflow_boundaries)
        for key, _ in __inflow_boundaries.items():
            inflow_boundaries[key] = {
                'indexes': list(np.where(_.pop('__bool'))[0]), **_}
        return inflow_boundaries

    @property
    def outflow_boundaries(self):
        outlow_boundaries = dict()
        __outflow_boundaries = deepcopy(self.__outflow_boundaries)
        for key, _ in __outflow_boundaries.items():
            outlow_boundaries[key] = {
                'indexes': list(np.where(_.pop('__bool'))[0]), **_}
        return outlow_boundaries

    @property
    def weir_boundaries(self):
        weir_boundaries = dict()
        __weir_boundaries = deepcopy(self.__weir_boundaries)
        for key, _ in __weir_boundaries.items():
            front_face = list(np.where(_.pop('__bool_front_face'))[0])
            back_face = list(np.where(_.pop('__bool_back_face'))[0])
            weir_boundaries[key] = {'front_face_indexes': front_face,
                                    'back_face_indexes': back_face, **_}
        return weir_boundaries

    @property
    def culvert_boundaries(self):
        culvert_boundaries = dict()
        __culvert_boundaries = deepcopy(self.__culvert_boundaries)
        for key, _ in __culvert_boundaries.items():
            front_face = list(np.where(_.pop('__bool_front_face'))[0])
            back_face = list(np.where(_.pop('__bool_back_face'))[0])
            culvert_boundaries[key] = {'front_face_indexes': front_face,
                                       'back_face_indexes': back_face, **_}
        return culvert_boundaries

    @property
    def description(self):
        try:
            return self.__description
        except AttributeError:
            return uuid.uuid4().hex[:8]

    @property
    def gr3(self):
        f = "{}\n".format(self.description)
        f += "{}  {}\n".format(self.num_elements, self.num_nodes)
        for i in range(self.num_nodes):
            f += "{:d} ".format(self.node_id[i]+1)
            f += "{:<.16E} ".format(self.x[i])
            f += " {:<.16E} ".format(self.y[i])
            f += "{:<.16E}\n".format(-self.values[i])
        for i in range(self.num_elements):
            f += "{:d} ".format(self.element_id[i]+1)
            f += "{:d} ".format(3)
            f += "{:d} ".format(self.elements[i, 0]+1)
            f += "{:d} ".format(self.elements[i, 1]+1)
            f += "{:d}\n".format(self.elements[i, 2]+1)
        f += "{:d} ! total number of ocean boundaries\n".format(
            len(self.ocean_boundaries.keys()))
        f += "{:d} ! total number of ocean boundary nodes\n".format(
            len(self.ocean_boundary))
        for key, indexes in self.ocean_boundaries.items():
            f += "{:d}".format(len(indexes))
            f += " ! number of nodes for ocean_boundary_"
            f += "{}\n".format(key)
            for idx in indexes:
                f += "{:d}\n".format(idx+1)
        f += "{:d}".format(
            len(self.land_boundaries.keys()) +
            len(self.inner_boundaries.keys()) +
            len(self.inflow_boundaries.keys()) +
            len(self.outflow_boundaries.keys()) +
            len(self.weir_boundaries.keys()) +
            len(self.culvert_boundaries.keys()))
        f += " ! total number of non-ocean boundaries\n"
        f += "{:d}".format(
            len(self.land_boundary) +
            len(self.inner_boundary) +
            len(self.inflow_boundary) +
            len(self.outflow_boundary) +
            len(self.weir_boundary) +
            len(self.culvert_boundary))
        f += " ! total number of non-ocean boundary nodes\n"
        for key, _ in self.land_boundaries.items():
            f += "{:d} ".format(len(_['indexes']))
            f += "{:d} ".format(_['ibtype'])
            f += "! number of nodes and ibtype for land_boundary_"
            f += "{}\n".format(key)
            for idx in _['indexes']:
                f += "{:d}\n".format(idx+1)
        for key, _ in self.inner_boundaries.items():
            f += "{:d} ".format(len(_['indexes']))
            f += "{:d} ".format(_['ibtype'])
            f += "! number of nodes and ibtype for inner_boundary_"
            f += "{}\n".format(key)
            for idx in _['indexes']:
                f += "{:d}\n".format(idx+1)
        for key, _ in self.inflow_boundaries.items():
            f += "{:d} ".format(len(_['indexes']))
            f += "{:d} ".format(_['ibtype'])
            f += "! number of nodes and ibtype for inflow_boundary_"
            f += "{}\n".format(key)
            for idx in _['indexes']:
                f += "{:d}\n".format(idx+1)
        for key, _ in self.outflow_boundaries.items():
            f += "{:d} ".format(len(_['indexes']))
            f += "{:d} ".format(_['ibtype'])
            f += "! number of nodes and ibtype for outflow_boundary_"
            f += "{}\n".format(key)
            for i in range(len(_['indexes'])):
                f += "{:d} ".format(_['indexes'][i]+1)
                f += "{:<.16E} ".format(_["barrier_heights"][i])
                f += "{:<.16E} ".format(
                        _["subcritical_flow_coefficients"][i])
                f += "\n"
        for key, _ in self.weir_boundaries.items():
            f += "{:d} ".format(len(_['front_face_indexes']))
            f += "{:d} ".format(_['ibtype'])
            f += "! number of nodes and ibtype for weir_boundary_"
            f += "{}\n".format(key)
            for i in range(len(_['front_face_indexes'])):
                f += "{:d} ".format(_['front_face_indexes'][i]+1)
                f += "{:d} ".format(_['back_face_indexes'][i]+1)
                f += "{:<.16E} ".format(_["barrier_heights"][i])
                f += "{:<.16E} ".format(
                        _["subcritical_flow_coefficients"][i])
                f += "{:<.16E} ".format(
                        _["supercritical_flow_coefficients"][i])
                f += "\n"
        for key, _ in self.culvert_boundaries.items():
            f += "{:d} ".format(len(_['indexes']))
            f += "{:d} ".format(_['ibtype'])
            f += "! number of nodes and ibtype for culvert_boundary_"
            f += "{}\n".format(key)
            for i in range(len(_['front_face_indexes'])):
                f += "{:d} ".format(_['front_face_indexes'][i]+1)
                f += "{:d} ".format(_['back_face_indexes'][i]+1)
                f += "{:<.16E} ".format(_["barrier_heights"][i])
                f += "{:<.16E} ".format(_["subcritical_flow_coefficients"][i])
                f += "{:<.16E} ".format(
                    _["supercritical_flow_coefficients"][i])
                f += "{:<.16E} ".format(_["cross_barrier_pipe_heights"][i])
                f += "{:<.16E} ".format(_["friction_factors"][i])
                f += "{:<.16E} ".format(_["pipe_diameters"][i])
                f += "\n"
        f += "{}\n".format(self.SpatialReference.ExportToWkt())
        return f

    @description.setter
    def description(self, description):
        self.__decription = str(description)

    @values.setter
    def values(self, values):
        if self.has_attribute("__values"):
            self.remove_attribute("__values")
        self._values = values

    @SpatialReference.setter
    def SpatialReference(self, SpatialReference):
        if self.__SpatialReference is None:
            raise Exception(
                'Cannot transform SpatialReference of object: initial '
                + 'SpatialReference is not set.')
        if SpatialReference is None:
            SpatialReference = osr.SpatialReference()
        elif isinstance(SpatialReference, int):
            EPSG = SpatialReference
            SpatialReference = osr.SpatialReference()
            SpatialReference.ImportFromEPSG(EPSG)
        assert isinstance(SpatialReference, osr.SpatialReference)
        if not self.SpatialReference.IsSame(SpatialReference):
            if hasattr(self, "__planar_straight_line_graph"):
                self.planar_straight_line_graph.SpatialReference \
                    = SpatialReference
            print(self.get_xy(SpatialReference))
            self._vertices = self.get_xy(SpatialReference)
            self._SpatialReference = SpatialReference

    @property
    def _vertices(self):
        return self.__vertices

    @property
    def _elements(self):
        return self.__elements

    @property
    def _values(self):
        return self.__values

    @property
    def _SpatialReference(self):
        return self.__SpatialReference

    @_vertices.setter
    def _vertices(self, vertices):
        vertices = np.asarray(vertices)
        assert vertices.shape[1] == self.ndim
        self.__vertices = vertices

    @_elements.setter
    def _elements(self, elements):
        elements = np.asarray(elements)
        assert elements.shape[1] == 3
        self.__elements = elements

    @_values.setter
    def _values(self, values):
        if values is None:
            values = np.full((self.vertices.shape[0],), np.nan)
        values = np.asarray(values)
        assert values.shape[0] == self.vertices.shape[0]
        self.__values = values

    @_SpatialReference.setter
    def _SpatialReference(self, SpatialReference):
        if SpatialReference is None:
            pass
        elif isinstance(SpatialReference, int):
            EPSG = SpatialReference
            SpatialReference = osr.SpatialReference()
            SpatialReference.ImportFromEPSG(EPSG)
        else:
            assert isinstance(SpatialReference, osr.SpatialReference)
        self.__SpatialReference = SpatialReference
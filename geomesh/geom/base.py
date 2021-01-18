from abc import ABC, abstractmethod

from jigsawpy import jigsaw_msh_t  # type: ignore[import]
import numpy as np  # type: ignore[import]


class BaseGeom(ABC):

    @abstractmethod
    def get_multipolygon(self, **kwargs):
        raise NotImplementedError

    @property
    def multipolygon(self):
        return self.get_multipolygon()

    @property
    def geom(self):
        '''Return a jigsaw_msh_t object representing the geometry'''
        vert2 = list()
        for polygon in self.multipolygon:
            if np.all(
                    np.asarray(
                        polygon.exterior.coords).flatten() == float('inf')):
                raise NotImplementedError("ellispoidal-mesh")
            for x, y in polygon.exterior.coords[:-1]:
                vert2.append(((x, y), 0))
            for interior in polygon.interiors:
                for x, y in interior.coords[:-1]:
                    vert2.append(((x, y), 0))
        vert2 = np.asarray(vert2, dtype=jigsaw_msh_t.VERT2_t)
        # edge2
        edge2 = list()
        for polygon in self.multipolygon:
            polygon = [polygon.exterior, *polygon.interiors]
            for linear_ring in polygon:
                _edge2 = list()
                for i in range(len(linear_ring.coords)-2):
                    _edge2.append((i, i+1))
                _edge2.append((_edge2[-1][1], _edge2[0][0]))
                edge2.extend(
                    [(e0+len(edge2), e1+len(edge2))
                        for e0, e1 in _edge2])
        edge2 = np.asarray(
            [((e0, e1), 0) for e0, e1 in edge2],
            dtype=jigsaw_msh_t.EDGE2_t)
        # geom
        geom = jigsaw_msh_t()
        geom.ndims = +2
        geom.mshID = 'euclidean-mesh'
        # TODO: Does raster mean it's not ellipsoid?
#        geom.mshID = 'euclidean-mesh' if self._ellipsoid is None \
#            else 'ellipsoidal-mesh'
        geom.vert2 = vert2
        geom.edge2 = edge2
        return geom

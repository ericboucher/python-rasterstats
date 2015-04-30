# -*- coding: utf-8 -*-
from __future__ import unicode_literals
import json
import math


class RasterStatsError(Exception):
    pass


class OGRError(Exception):
    pass


def bbox_to_pixel_offsets(gt, bbox, rsize):
    originX = gt[0]
    originY = gt[3]
    pixel_width = gt[1]
    pixel_height = gt[5]

    x1 = int(math.floor((bbox[0] - originX) / pixel_width))
    x2 = int(math.ceil((bbox[2] - originX) / pixel_width))

    y1 = int(math.floor((bbox[3] - originY) / pixel_height))
    y2 = int(math.ceil((bbox[1] - originY) / pixel_height))

    # "Clip" the geometry bounds to the overall raster bounding box
    # This should avoid any rasterIO errors for partially overlapping polys
    if x1 < 0:
        x1 = 0
    if x2 > rsize[0]:
        x2 = rsize[0]
    if y1 < 0:
        y1 = 0
    if y2 > rsize[1]:
        y2 = rsize[1]

    xsize = x2 - x1
    ysize = y2 - y1

    return (x1, y1, xsize, ysize)

def pixel_offsets_to_window(offsets):
    """
    Convert (x1, y1, xsize, ysize)
    to a rasterio-compatible window
    https://github.com/mapbox/rasterio/blob/master/docs/windowed-rw.rst#windows
    """
    if len(offsets) != 4:
        raise RasterStatsError("offset should be a 4-element tuple")
    x1, y1, xsize, ysize = offsets
    return ((y1, y1 + ysize), (x1, x1 + xsize))

def raster_extent_as_bounds(gt, shape):
    x1 = gt[0]
    x2 = gt[0] + (gt[1] * shape[0])
    y1 = gt[3] + (gt[5] * shape[1])
    y2 = gt[3]
    return (x1, y1, x2, y2)


def get_percentile(stat):
    if not stat.startswith('percentile_'):
        raise ValueError
    qstr = stat.replace("percentile_", '')
    q = float(qstr)
    if q > 100.0:
        raise ValueError
    if q < 0.0:
        raise ValueError
    return q


def feature_to_geojson(feature):
    """ This duplicates the feature.ExportToJson ogr method
    but is safe across gdal versions since it was fixed only in 1.8+
    see http://trac.osgeo.org/gdal/ticket/3870"""

    geom = feature.GetGeometryRef()
    if geom is not None:
        geom_json_string = geom.ExportToJson()
        geom_json_object = json.loads(geom_json_string)
    else:
        geom_json_object = None

    output = {
        'type': 'Feature',
        'geometry': geom_json_object,
        'properties': {}
    }

    fid = feature.GetFID()
    if fid:
        output['id'] = fid

    for key in list(feature.keys()):
        output['properties'][key] = feature.GetField(key)

    return output


def shapely_to_ogr_type(shapely_type):
    from osgeo import ogr
    if shapely_type == "Polygon":
        return ogr.wkbPolygon
    elif shapely_type == "LineString":
        return ogr.wkbLineString
    elif shapely_type == "MultiPolygon":
        return ogr.wkbMultiPolygon
    elif shapely_type == "MultiLineString":
        return ogr.wkbLineString
    raise TypeError("shapely type %s not supported" % shapely_type)


def parse_geo(thing):
    """ Given a python object, try to get a geo-json like mapping from it
    """
    from shapely.geos import ReadingError
    from shapely import wkt, wkb

    # object implementing geo_interface
    try:
        geo = thing.__geo_interface__
        return geo
    except AttributeError:
        pass

    # wkb
    try:
        shape = wkb.loads(thing)
        return shape.__geo_interface__
    except (ReadingError, TypeError):
        pass

    # wkt
    try:
        shape = wkt.loads(thing)
        return shape.__geo_interface__
    except (ReadingError, TypeError, AttributeError):
        pass

    # geojson-like python mapping
    valid_types = ["Feature", "Point", "LineString", "Polygon",
                   "MultiPoint", "MultiLineString", "MultiPolygon"]
    try:
        assert thing['type'] in valid_types
        return thing
    except (AssertionError, TypeError):
        pass

    # geojson string
    try:
        maybe_geo = json.loads(thing)
        assert maybe_geo['type'] in valid_types + ["FeatureCollection"]
        return maybe_geo
    except (ValueError, AssertionError, TypeError):
        pass

    raise RasterStatsError("Can't parse %s as a geo-like object" % thing)


def get_ogr_ds(vds):
    from osgeo import ogr
    if not isinstance(vds, str):
        raise OGRError("OGR cannot open %r: not a string" % vds)

    ds = ogr.Open(vds)
    if not ds:
        raise OGRError("OGR cannot open %r" % vds)

    return ds


def ogr_srs(vector, layer_num):
    ds = get_ogr_ds(vector)
    layer = ds.GetLayer(layer_num)
    return layer.GetSpatialRef()


def ogr_records(vector, layer_num=0):
    ds = get_ogr_ds(vector)
    layer = ds.GetLayer(layer_num)
    if layer.GetFeatureCount() == 0:
        raise OGRError("No Features")
    feature = layer.GetNextFeature()
    while feature is not None:
        yield feature_to_geojson(feature)
        feature = layer.GetNextFeature()


def geo_records(vectors):
    for vector in vectors:
        yield parse_geo(vector)


def get_features(vectors, layer_num=0):
    from osgeo import osr
    spatial_ref = osr.SpatialReference()
    if isinstance(vectors, str):
        try:
            # either an OGR layer ...
            get_ogr_ds(vectors)
            features_iter = ogr_records(vectors, layer_num)
            spatial_ref = ogr_srs(vectors, layer_num)
            strategy = "ogr"
        except (OGRError, AttributeError):
            # ... or a single string to be parsed as wkt/wkb/json
            feat = parse_geo(vectors)
            features_iter = [feat]
            strategy = "single_geo"
    elif isinstance(vectors, bytes):
        # wkb
        feat = parse_geo(vectors)
        features_iter = [feat]
        strategy = "single_geo"
    elif hasattr(vectors, '__geo_interface__'):
        geotype = vectors.__geo_interface__['type']
        if geotype.lower() == 'featurecollection':
            # ... a featurecollection
            features_iter = geo_records(vectors.__geo_interface__['features'])
            strategy = "geo_featurecollection"
        else:
            # ... or an single object
            feat = parse_geo(vectors)
            features_iter = [feat]
            strategy = "single_geo"
    elif isinstance(vectors, dict):
        # ... or an python mapping
        feat = parse_geo(vectors)
        features_iter = [feat]
        strategy = "single_geo"
    else:
        # ... or an iterable of objects
        features_iter = geo_records(vectors)
        strategy = "iter_geo"

    return features_iter, strategy, spatial_ref


def rasterize_geom(geom, src_offset, new_gt, all_touched):
    from rasterio import features
    geoms = [(geom, 1)]
    rv_array = features.rasterize(
        geoms,
        out_shape=(src_offset[3], src_offset[2]),
        transform=new_gt,
        fill=0,
        all_touched=all_touched)
    return rv_array


def rasterize_geom_gdal(geom, src_offset, new_gt, all_touched, spatial_ref):
    """
    deprecated, used rasterize_geom instead
    keeping this around as a reference for other gdal->rasterio efforts
    and because it's roughly 2x faster than rasterio implementation
    """
    from osgeo import ogr, gdal
    ogr_geom_type = shapely_to_ogr_type(geom.type)
    # Create a temporary vector layer in memory
    mem_drv = ogr.GetDriverByName(str("Memory"))
    mem_ds = mem_drv.CreateDataSource(str('out'))
    mem_layer = mem_ds.CreateLayer(str('out'), spatial_ref, ogr_geom_type)
    ogr_feature = ogr.Feature(feature_def=mem_layer.GetLayerDefn())
    ogr_geom = ogr.CreateGeometryFromWkt(geom.wkt)
    ogr_feature.SetGeometryDirectly(ogr_geom)
    mem_layer.CreateFeature(ogr_feature)

    # Rasterize it
    driver = gdal.GetDriverByName(str('MEM'))
    rvds = driver.Create(str('rvds'), src_offset[2], src_offset[3], 1, gdal.GDT_Byte)
    rvds.SetGeoTransform(new_gt)

    if all_touched:
        gdal.RasterizeLayer(rvds, [1], mem_layer, None, None,
                            burn_values=[1],
                            options=['ALL_TOUCHED=True'])
    else:
        gdal.RasterizeLayer(rvds, [1], mem_layer, None, None,
                            burn_values=[1],
                            options=['ALL_TOUCHED=False'])

    rv_array = rvds.ReadAsArray()
    return rv_array

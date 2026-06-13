# ============================================================
# BHOOMITRA SURVEY — GeoPDF Conversion Server
# Converts GeoPDF to GeoTIFF using GDAL
# Returns converted image + full metadata as multipart response
# ============================================================

import os
import uuid
import json
import logging
import tempfile
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from osgeo import gdal, osr

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

gdal.UseExceptions()

MAX_FILE_SIZE = 99 * 1024 * 1024  # 99 MB

# ============================================================
# HEALTH CHECK
# ============================================================
@app.route('/health', methods=['GET'])
def health():
    return jsonify({
        'status': 'ok',
        'service': 'bhoomitra-geopdf-server',
        'gdal_version': gdal.VersionInfo()
    }), 200

# ============================================================
# EXTRACT METADATA FROM DATASET
# ============================================================
def extract_metadata(dataset):
    try:
        # Get geotransform
        gt = dataset.GetGeoTransform()
        width = dataset.RasterXSize
        height = dataset.RasterYSize
        bands = dataset.RasterCount

        # Calculate bounding box in source projection
        min_x = gt[0]
        max_x = gt[0] + width * gt[1]
        max_y = gt[3]
        min_y = gt[3] + height * gt[5]

        # Get source projection
        proj_wkt = dataset.GetProjection()
        source_srs = osr.SpatialReference()
        source_srs.ImportFromWkt(proj_wkt)

        # Get EPSG code
        source_srs.AutoIdentifyEPSG()
        epsg_code = source_srs.GetAuthorityCode(None)
        projection = f"EPSG:{epsg_code}" if epsg_code else "UNKNOWN"

        # Get datum name
        datum = source_srs.GetAttrValue("DATUM") or "UNKNOWN"

        # Reproject bbox to WGS84 if needed
        target_srs = osr.SpatialReference()
        target_srs.ImportFromEPSG(4326)
        target_srs.SetAxisMappingStrategy(
            osr.OAMS_TRADITIONAL_GIS_ORDER
        )

        if not source_srs.IsSame(target_srs):
            # Need reprojection
            transform = osr.CoordinateTransformation(
                source_srs, target_srs
            )
            # Transform all 4 corners
            corners = [
                (min_x, min_y),
                (max_x, min_y),
                (max_x, max_y),
                (min_x, max_y),
            ]
            transformed = [
                transform.TransformPoint(x, y)
                for x, y in corners
            ]
            lons = [p[0] for p in transformed]
            lats = [p[1] for p in transformed]
            min_lon = min(lons)
            max_lon = max(lons)
            min_lat = min(lats)
            max_lat = max(lats)
        else:
            # Already WGS84
            min_lon = min_x
            max_lon = max_x
            min_lat = min_y
            max_lat = max_y

        return {
            'minLon': min_lon,
            'maxLon': max_lon,
            'minLat': min_lat,
            'maxLat': max_lat,
            'projection': projection,
            'datum': datum,
            'imageWidth': width,
            'imageHeight': height,
            'bands': bands,
            'projectionWkt': proj_wkt,
        }

    except Exception as e:
        logger.error(f'Metadata extraction error: {str(e)}')
        return None

# ============================================================
# VALIDATE GEOGRAPHIC REFERENCE
# ============================================================
def has_georef(dataset):
    gt = dataset.GetGeoTransform()
    proj = dataset.GetProjection()
    default_gt = (0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
    return bool(proj) and gt != default_gt

# ============================================================
# CLEANUP HELPER
# ============================================================
def cleanup(*paths):
    for path in paths:
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except Exception:
            pass

# ============================================================
# CONVERT GEOPDF TO CLOUD OPTIMIZED GEOTIFF
# ============================================================
@app.route('/convert', methods=['POST'])
def convert():
    input_path = None
    output_tif_path = None
    output_cog_path = None

    try:
        # ── Validate request ──────────────────────────────
        if 'file' not in request.files:
            return jsonify({'error': 'NO FILE PROVIDED'}), 400

        file = request.files['file']

        if not file.filename:
            return jsonify({'error': 'NO FILE SELECTED'}), 400

        filename = file.filename.lower()
        if not filename.endswith('.pdf'):
            return jsonify({'error': 'ONLY PDF FILES ARE ACCEPTED'}), 400

        # ── Check file size ───────────────────────────────
        file.seek(0, 2)
        file_size = file.tell()
        file.seek(0)

        if file_size > MAX_FILE_SIZE:
            return jsonify({
                'error': f'FILE SIZE {file_size/(1024*1024):.1f} MB EXCEEDS 99 MB LIMIT'
            }), 400

        if file_size == 0:
            return jsonify({'error': 'FILE IS EMPTY'}), 400

        logger.info(f'Processing: {file.filename} ({file_size/(1024*1024):.1f} MB)')

        # ── Save uploaded file ────────────────────────────
        temp_id = str(uuid.uuid4())
        temp_dir = tempfile.gettempdir()
        input_path = os.path.join(temp_dir, f'{temp_id}_input.pdf')
        output_tif_path = os.path.join(temp_dir, f'{temp_id}_output.tif')
        output_cog_path = os.path.join(temp_dir, f'{temp_id}_cog.tif')

        file.save(input_path)
        logger.info(f'File saved to: {input_path}')

        # ── Open with GDAL ────────────────────────────────
        dataset = gdal.Open(input_path, gdal.GA_ReadOnly)
        if dataset is None:
            cleanup(input_path)
            return jsonify({
                'error': 'COULD NOT OPEN FILE. PLEASE ENSURE IT IS A VALID GEOREFERENCED GEOPDF.'
            }), 400

        # ── Validate georeference ─────────────────────────
        if not has_georef(dataset):
            dataset = None
            cleanup(input_path)
            return jsonify({
                'error': 'FILE HAS NO GEOGRAPHIC REFERENCE. PLEASE USE A GEOREFERENCED GEOPDF.'
            }), 400

        # ── Extract metadata ──────────────────────────────
        metadata = extract_metadata(dataset)
        if not metadata:
            dataset = None
            cleanup(input_path)
            return jsonify({
                'error': 'COULD NOT EXTRACT GEOGRAPHIC METADATA FROM FILE.'
            }), 400

        logger.info(f'Metadata extracted: {json.dumps(metadata, indent=2)}')

        # ── Convert to GeoTIFF ────────────────────────────
        translate_options = gdal.TranslateOptions(
            format='GTiff',
            creationOptions=[
                'COMPRESS=LZW',
                'TILED=YES',
                'BLOCKXSIZE=256',
                'BLOCKYSIZE=256',
                'BIGTIFF=IF_NEEDED',
                'INTERLEAVE=BAND',
            ]
        )

        result = gdal.Translate(output_tif_path, dataset, options=translate_options)
        dataset = None
        result = None

        if not os.path.exists(output_tif_path):
            cleanup(input_path)
            return jsonify({'error': 'GEOTIFF CONVERSION FAILED'}), 500

        logger.info(f'GeoTIFF created: {os.path.getsize(output_tif_path)/(1024*1024):.1f} MB')

        # ── Convert to Cloud Optimized GeoTIFF (COG) ─────
        cog_options = gdal.TranslateOptions(
            format='GTiff',
            creationOptions=[
                'COMPRESS=LZW',
                'TILED=YES',
                'BLOCKXSIZE=256',
                'BLOCKYSIZE=256',
                'COPY_SRC_OVERVIEWS=YES',
                'BIGTIFF=IF_NEEDED',
            ]
        )

        # Add overviews for multi-zoom support
        ds_for_cog = gdal.Open(output_tif_path, gdal.GA_Update)
        if ds_for_cog:
            ds_for_cog.BuildOverviews(
                'AVERAGE', [2, 4, 8, 16, 32]
            )
            ds_for_cog = None

        cog_result = gdal.Translate(
            output_cog_path,
            output_tif_path,
            options=cog_options
        )
        cog_result = None

        cleanup(input_path, output_tif_path)

        if not os.path.exists(output_cog_path):
            cleanup(output_cog_path)
            return jsonify({'error': 'COG CONVERSION FAILED'}), 500

        cog_size = os.path.getsize(output_cog_path)
        logger.info(f'COG created: {cog_size/(1024*1024):.1f} MB')
        logger.info(f'Bounds: {metadata["minLon"]},{metadata["minLat"]} to {metadata["maxLon"]},{metadata["maxLat"]}')

        # ── Return COG file with metadata in header ───────
        response = send_file(
            output_cog_path,
            mimetype='image/tiff',
            as_attachment=True,
            download_name=f'converted_{temp_id}.tif',
            max_age=0,
        )

        # Attach metadata as response headers
        response.headers['X-Min-Lon'] = str(metadata['minLon'])
        response.headers['X-Max-Lon'] = str(metadata['maxLon'])
        response.headers['X-Min-Lat'] = str(metadata['minLat'])
        response.headers['X-Max-Lat'] = str(metadata['maxLat'])
        response.headers['X-Projection'] = metadata['projection']
        response.headers['X-Datum'] = metadata['datum']
        response.headers['X-Image-Width'] = str(metadata['imageWidth'])
        response.headers['X-Image-Height'] = str(metadata['imageHeight'])
        response.headers['X-Bands'] = str(metadata['bands'])
        response.headers['Access-Control-Expose-Headers'] = (
            'X-Min-Lon, X-Max-Lon, X-Min-Lat, X-Max-Lat, '
            'X-Projection, X-Datum, X-Image-Width, '
            'X-Image-Height, X-Bands'
        )

        return response

    except Exception as e:
        logger.error(f'Conversion error: {str(e)}')
        cleanup(input_path, output_tif_path, output_cog_path)
        return jsonify({'error': f'SERVER ERROR: {str(e)}'}), 500


# ============================================================
# METADATA ONLY ENDPOINT
# For checking file without full conversion
# ============================================================
@app.route('/metadata', methods=['POST'])
def get_metadata():
    input_path = None
    try:
        if 'file' not in request.files:
            return jsonify({'error': 'NO FILE PROVIDED'}), 400

        file = request.files['file']
        temp_id = str(uuid.uuid4())
        temp_dir = tempfile.gettempdir()
        input_path = os.path.join(temp_dir, f'{temp_id}_meta.pdf')
        file.save(input_path)

        dataset = gdal.Open(input_path, gdal.GA_ReadOnly)
        if dataset is None:
            cleanup(input_path)
            return jsonify({'error': 'COULD NOT OPEN FILE'}), 400

        if not has_georef(dataset):
            dataset = None
            cleanup(input_path)
            return jsonify({
                'error': 'FILE HAS NO GEOGRAPHIC REFERENCE'
            }), 400

        metadata = extract_metadata(dataset)
        dataset = None
        cleanup(input_path)

        if not metadata:
            return jsonify({
                'error': 'COULD NOT EXTRACT METADATA'
            }), 400

        return jsonify({'success': True, 'metadata': metadata}), 200

    except Exception as e:
        cleanup(input_path)
        return jsonify({'error': str(e)}), 500


# ============================================================
# RUN
# ============================================================
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
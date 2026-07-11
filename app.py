# ============================================================
# BHOOMITRA SURVEY — GeoPDF Conversion Server
# Converts GeoPDF to GeoTIFF using GDAL
# NEW: /tiles endpoint generates offline tile pyramid
# ============================================================

import os
import uuid
import json
import logging
import tempfile
import zipfile
import subprocess
import shutil
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
# HEALTH CHECK (unchanged)
# ============================================================
@app.route('/health', methods=['GET'])
def health():
    return jsonify({
        'status': 'ok',
        'service': 'bhoomitra-geopdf-server',
        'gdal_version': gdal.VersionInfo()
    }), 200

# ============================================================
# EXTRACT METADATA FROM DATASET (unchanged)
# ============================================================
def extract_metadata(dataset):
    try:
        gt = dataset.GetGeoTransform()
        width = dataset.RasterXSize
        height = dataset.RasterYSize
        bands = dataset.RasterCount

        min_x = gt[0]
        max_x = gt[0] + width * gt[1]
        max_y = gt[3]
        min_y = gt[3] + height * gt[5]

        proj_wkt = dataset.GetProjection()
        source_srs = osr.SpatialReference()
        source_srs.ImportFromWkt(proj_wkt)

        source_srs.AutoIdentifyEPSG()
        epsg_code = source_srs.GetAuthorityCode(None)
        projection = f"EPSG:{epsg_code}" if epsg_code else "UNKNOWN"
        datum = source_srs.GetAttrValue("DATUM") or "UNKNOWN"

        target_srs = osr.SpatialReference()
        target_srs.ImportFromEPSG(4326)
        target_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)

        if not source_srs.IsSame(target_srs):
            transform = osr.CoordinateTransformation(source_srs, target_srs)
            corners = [
                (min_x, min_y), (max_x, min_y),
                (max_x, max_y), (min_x, max_y),
            ]
            transformed = [transform.TransformPoint(x, y) for x, y in corners]
            lons = [p[0] for p in transformed]
            lats = [p[1] for p in transformed]
            min_lon = min(lons)
            max_lon = max(lons)
            min_lat = min(lats)
            max_lat = max(lats)
        else:
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
# VALIDATE GEOGRAPHIC REFERENCE (unchanged)
# ============================================================
def has_georef(dataset):
    gt = dataset.GetGeoTransform()
    proj = dataset.GetProjection()
    default_gt = (0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
    return bool(proj) and gt != default_gt

# ============================================================
# CLEANUP HELPER (updated to handle folders too)
# ============================================================
def cleanup(*paths):
    for path in paths:
        try:
            if path and os.path.exists(path):
                if os.path.isdir(path):
                    shutil.rmtree(path)
                else:
                    os.remove(path)
        except Exception:
            pass

# ============================================================
# CONVERT GEOPDF TO CLOUD OPTIMIZED GEOTIFF (unchanged)
# ============================================================
@app.route('/convert', methods=['POST'])
def convert():
    input_path = None
    output_tif_path = None
    output_cog_path = None

    try:
        if 'file' not in request.files:
            return jsonify({'error': 'NO FILE PROVIDED'}), 400

        file = request.files['file']

        if not file.filename:
            return jsonify({'error': 'NO FILE SELECTED'}), 400

        filename = file.filename.lower()
        if not filename.endswith('.pdf'):
            return jsonify({'error': 'ONLY PDF FILES ARE ACCEPTED'}), 400

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

        temp_id = str(uuid.uuid4())
        temp_dir = tempfile.gettempdir()
        input_path = os.path.join(temp_dir, f'{temp_id}_input.pdf')
        output_tif_path = os.path.join(temp_dir, f'{temp_id}_output.tif')
        output_cog_path = os.path.join(temp_dir, f'{temp_id}_cog.tif')

        file.save(input_path)
        logger.info(f'File saved to: {input_path}')

        dataset = gdal.Open(input_path, gdal.GA_ReadOnly)
        if dataset is None:
            cleanup(input_path)
            return jsonify({'error': 'COULD NOT OPEN FILE. PLEASE ENSURE IT IS A VALID GEOREFERENCED GEOPDF.'}), 400

        if not has_georef(dataset):
            dataset = None
            cleanup(input_path)
            return jsonify({'error': 'FILE HAS NO GEOGRAPHIC REFERENCE. PLEASE USE A GEOREFERENCED GEOPDF.'}), 400

        metadata = extract_metadata(dataset)
        if not metadata:
            dataset = None
            cleanup(input_path)
            return jsonify({'error': 'COULD NOT EXTRACT GEOGRAPHIC METADATA FROM FILE.'}), 400

        logger.info(f'Metadata extracted: {json.dumps(metadata, indent=2)}')

        translate_options = gdal.TranslateOptions(
            format='GTiff',
            creationOptions=[
                'COMPRESS=LZW', 'TILED=YES',
                'BLOCKXSIZE=256', 'BLOCKYSIZE=256',
                'BIGTIFF=IF_NEEDED', 'INTERLEAVE=BAND',
            ]
        )

        result = gdal.Translate(output_tif_path, dataset, options=translate_options)
        dataset = None
        result = None

        if not os.path.exists(output_tif_path):
            cleanup(input_path)
            return jsonify({'error': 'GEOTIFF CONVERSION FAILED'}), 500

        logger.info(f'GeoTIFF created: {os.path.getsize(output_tif_path)/(1024*1024):.1f} MB')

        cog_options = gdal.TranslateOptions(
            format='GTiff',
            creationOptions=[
                'COMPRESS=LZW', 'TILED=YES',
                'BLOCKXSIZE=256', 'BLOCKYSIZE=256',
                'COPY_SRC_OVERVIEWS=YES', 'BIGTIFF=IF_NEEDED',
            ]
        )

        ds_for_cog = gdal.Open(output_tif_path, gdal.GA_Update)
        if ds_for_cog:
            ds_for_cog.BuildOverviews('AVERAGE', [2, 4, 8, 16, 32])
            ds_for_cog = None

        cog_result = gdal.Translate(output_cog_path, output_tif_path, options=cog_options)
        cog_result = None

        cleanup(input_path, output_tif_path)

        if not os.path.exists(output_cog_path):
            cleanup(output_cog_path)
            return jsonify({'error': 'COG CONVERSION FAILED'}), 500

        cog_size = os.path.getsize(output_cog_path)
        logger.info(f'COG created: {cog_size/(1024*1024):.1f} MB')
        logger.info(f'Bounds: {metadata["minLon"]},{metadata["minLat"]} to {metadata["maxLon"]},{metadata["maxLat"]}')

        response = send_file(
            output_cog_path,
            mimetype='image/tiff',
            as_attachment=True,
            download_name=f'converted_{temp_id}.tif',
            max_age=0,
        )

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
# NEW: GENERATE OFFLINE TILE PYRAMID
# Accepts a GeoTIFF, reprojects to EPSG:3857, runs gdal2tiles
# to generate XYZ PNG tiles at zoom levels 10-18, zips them
# and returns the zip. App downloads once, works offline after.
# ============================================================
@app.route('/tiles', methods=['POST'])
def generate_tiles():
    input_path = None
    warped_path = None
    tiles_dir = None
    zip_path = None

    try:
        if 'file' not in request.files:
            return jsonify({'error': 'NO FILE PROVIDED'}), 400

        file = request.files['file']

        if not file.filename:
            return jsonify({'error': 'NO FILE SELECTED'}), 400

        file.seek(0, 2)
        file_size = file.tell()
        file.seek(0)

        if file_size > MAX_FILE_SIZE:
            return jsonify({'error': f'FILE TOO LARGE: {file_size/(1024*1024):.1f} MB'}), 400

        temp_id = str(uuid.uuid4())
        temp_dir = tempfile.gettempdir()
        input_path = os.path.join(temp_dir, f'{temp_id}_input.tif')
        warped_path = os.path.join(temp_dir, f'{temp_id}_warped.tif')
        tiles_dir = os.path.join(temp_dir, f'{temp_id}_tiles')
        zip_path = os.path.join(temp_dir, f'{temp_id}_tiles.zip')

        file.save(input_path)
        logger.info(f'Tile generation started: {file_size/(1024*1024):.1f} MB')

        # ── Open and validate ─────────────────────────────
        dataset = gdal.Open(input_path, gdal.GA_ReadOnly)
        if dataset is None:
            cleanup(input_path)
            return jsonify({'error': 'COULD NOT OPEN GEOTIFF FILE'}), 400

        if not has_georef(dataset):
            dataset = None
            cleanup(input_path)
            return jsonify({'error': 'FILE HAS NO GEOGRAPHIC REFERENCE'}), 400

        metadata = extract_metadata(dataset)
        image_width = dataset.RasterXSize
        image_height = dataset.RasterYSize
        dataset = None

        if not metadata:
            cleanup(input_path)
            return jsonify({'error': 'COULD NOT EXTRACT METADATA'}), 400

        logger.info(f'Bounds: {metadata["minLon"]:.4f},{metadata["minLat"]:.4f} to {metadata["maxLon"]:.4f},{metadata["maxLat"]:.4f}')

        # ── Reproject to EPSG:3857 (web mercator) ────────
        logger.info('Reprojecting to EPSG:3857...')
        warp_result = subprocess.run([
            'gdalwarp',
            '-t_srs', 'EPSG:3857',
            '-r', 'lanczos',
            '-co', 'COMPRESS=LZW',
            '-co', 'TILED=YES',
            '-co', 'BLOCKXSIZE=256',
            '-co', 'BLOCKYSIZE=256',
            '-dstalpha',
            input_path,
            warped_path
        ], capture_output=True, text=True, timeout=300)

        if warp_result.returncode != 0:
            logger.error(f'gdalwarp failed: {warp_result.stderr}')
            cleanup(input_path, warped_path)
            return jsonify({'error': f'REPROJECTION FAILED: {warp_result.stderr}'}), 500

        logger.info('Reprojection complete')

        # ── Calculate appropriate zoom range ──────────────
        import math
        lon_span = metadata['maxLon'] - metadata['minLon']
        lat_span = metadata['maxLat'] - metadata['minLat']
        px_per_deg = max(
            image_width / lon_span if lon_span > 0 else 0,
            image_height / lat_span if lat_span > 0 else 0
        )
        if px_per_deg > 0:
            native_zoom = math.log2(px_per_deg * 360 / 256)
            max_zoom = min(18, max(14, math.ceil(native_zoom)))
        else:
            max_zoom = 16

        min_zoom = 10
        logger.info(f'Generating tiles zoom {min_zoom}-{max_zoom}')

        # ── Generate XYZ tiles using gdal2tiles ───────────
        os.makedirs(tiles_dir, exist_ok=True)

        tiles_result = subprocess.run([
            'gdal2tiles.py',
            '--profile=mercator',
            f'--zoom={min_zoom}-{max_zoom}',
            '--resampling=lanczos',
            '--processes=2',
            '--webviewer=none',
            '--tmscompatible',
            warped_path,
            tiles_dir
        ], capture_output=True, text=True, timeout=600)

        if tiles_result.returncode != 0:
            logger.error(f'gdal2tiles failed: {tiles_result.stderr}')
            cleanup(input_path, warped_path, tiles_dir)
            return jsonify({'error': f'TILE GENERATION FAILED: {tiles_result.stderr}'}), 500

        tile_count = sum(len(files) for _, _, files in os.walk(tiles_dir))
        logger.info(f'Generated {tile_count} tiles')

        # ── Write metadata.json into tile folder ──────────
        metadata_path = os.path.join(tiles_dir, 'metadata.json')
        with open(metadata_path, 'w') as f:
            json.dump({
                'minLon': metadata['minLon'],
                'maxLon': metadata['maxLon'],
                'minLat': metadata['minLat'],
                'maxLat': metadata['maxLat'],
                'minZoom': min_zoom,
                'maxZoom': max_zoom,
                'projection': metadata['projection'],
                'datum': metadata['datum'],
                'imageWidth': image_width,
                'imageHeight': image_height,
                'bands': metadata['bands'],
                'tileCount': tile_count,
            }, f)

        # ── Zip everything ────────────────────────────────
        logger.info('Zipping tiles...')
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for root, dirs, files in os.walk(tiles_dir):
                for file_name in files:
                    file_full_path = os.path.join(root, file_name)
                    arc_name = os.path.relpath(file_full_path, tiles_dir)
                    zf.write(file_full_path, arc_name)

        zip_size = os.path.getsize(zip_path)
        logger.info(f'Zip: {zip_size/(1024*1024):.1f} MB, {tile_count} tiles')

        cleanup(input_path, warped_path, tiles_dir)

        response = send_file(
            zip_path,
            mimetype='application/zip',
            as_attachment=True,
            download_name=f'tiles_{temp_id}.zip',
            max_age=0,
        )

        response.headers['X-Min-Lon'] = str(metadata['minLon'])
        response.headers['X-Max-Lon'] = str(metadata['maxLon'])
        response.headers['X-Min-Lat'] = str(metadata['minLat'])
        response.headers['X-Max-Lat'] = str(metadata['maxLat'])
        response.headers['X-Min-Zoom'] = str(min_zoom)
        response.headers['X-Max-Zoom'] = str(max_zoom)
        response.headers['X-Tile-Count'] = str(tile_count)
        response.headers['Access-Control-Expose-Headers'] = (
            'X-Min-Lon, X-Max-Lon, X-Min-Lat, X-Max-Lat, '
            'X-Min-Zoom, X-Max-Zoom, X-Tile-Count'
        )

        return response

    except subprocess.TimeoutExpired:
        cleanup(input_path, warped_path, tiles_dir, zip_path)
        return jsonify({'error': 'TILE GENERATION TIMED OUT. FILE MAY BE TOO LARGE.'}), 500

    except Exception as e:
        logger.error(f'Tile generation error: {str(e)}')
        cleanup(input_path, warped_path, tiles_dir, zip_path)
        return jsonify({'error': f'SERVER ERROR: {str(e)}'}), 500


# ============================================================
# METADATA ONLY ENDPOINT (unchanged)
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
            return jsonify({'error': 'FILE HAS NO GEOGRAPHIC REFERENCE'}), 400

        metadata = extract_metadata(dataset)
        dataset = None
        cleanup(input_path)

        if not metadata:
            return jsonify({'error': 'COULD NOT EXTRACT METADATA'}), 400

        return jsonify({'success': True, 'metadata': metadata}), 200

    except Exception as e:
        cleanup(input_path)
        return jsonify({'error': str(e)}), 500


# ============================================================
# NEW: RECTIFY / CONVERT GEOTIFF
# ------------------------------------------------------------
# Separate from /convert (GeoPDF). Used as a FALLBACK when the
# app's on-device pure-JS reader can't find simple grid
# georeferencing (ModelTransformation, or ModelPixelScale +
# ModelTiepoint) in a GeoTIFF — most commonly because the file
# uses Ground Control Points (GCPs) or RPC coefficients instead.
#
# gdal.Warp (unlike gdal.Translate, used in /convert) actually
# resolves GCPs/RPCs into a genuine rectified grid, producing an
# output file with a normal, simple affine transform the app CAN
# read on-device from then on (we cache the rectified result).
#
# This route is intentionally independent of has_georef() /
# extract_metadata() usage patterns in /convert, so nothing here
# can change GeoPDF behavior.
# ============================================================
@app.route('/convert-tiff', methods=['POST'])
def convert_tiff():
    input_path = None
    output_path = None

    try:
        if 'file' not in request.files:
            return jsonify({'error': 'NO FILE PROVIDED'}), 400

        file = request.files['file']

        if not file.filename:
            return jsonify({'error': 'NO FILE SELECTED'}), 400

        filename = file.filename.lower()
        if not (filename.endswith('.tif') or filename.endswith('.tiff')):
            return jsonify({'error': 'ONLY GEOTIFF FILES ARE ACCEPTED'}), 400

        file.seek(0, 2)
        file_size = file.tell()
        file.seek(0)

        if file_size > MAX_FILE_SIZE:
            return jsonify({
                'error': f'FILE SIZE {file_size/(1024*1024):.1f} MB EXCEEDS 99 MB LIMIT'
            }), 400

        if file_size == 0:
            return jsonify({'error': 'FILE IS EMPTY'}), 400

        logger.info(f'Rectifying GeoTIFF: {file.filename} ({file_size/(1024*1024):.1f} MB)')

        temp_id = str(uuid.uuid4())
        temp_dir = tempfile.gettempdir()
        input_path = os.path.join(temp_dir, f'{temp_id}_input.tif')
        output_path = os.path.join(temp_dir, f'{temp_id}_rectified.tif')

        file.save(input_path)

        dataset = gdal.Open(input_path, gdal.GA_ReadOnly)
        if dataset is None:
            cleanup(input_path)
            return jsonify({'error': 'COULD NOT OPEN FILE. FILE MAY BE CORRUPTED OR NOT A VALID GEOTIFF.'}), 400

        # A file is rectifiable here if it has EITHER a normal
        # geotransform OR ground control points. (Plain has_georef()
        # alone would reject GCP-only files, which are exactly the
        # ones this route exists to handle.)
        gcp_count = dataset.GetGCPCount()
        has_affine = has_georef(dataset)

        if not has_affine and gcp_count == 0:
            dataset = None
            cleanup(input_path)
            return jsonify({
                'error': 'FILE HAS NO GEOGRAPHIC REFERENCE AT ALL (NO GEOTRANSFORM AND NO GROUND CONTROL POINTS). CANNOT RECTIFY.'
            }), 400

        warp_kwargs = dict(
            format='GTiff',
            dstSRS='EPSG:4326',
            resampleAlg='bilinear',
            creationOptions=[
                'COMPRESS=LZW', 'TILED=YES',
                'BLOCKXSIZE=256', 'BLOCKYSIZE=256',
                'BIGTIFF=IF_NEEDED',
            ],
        )
        # GCP-only files (no usable geotransform) need a
        # thin-plate-spline warp driven by the GCPs themselves.
        if gcp_count > 0 and not has_affine:
            warp_kwargs['tps'] = True

        warp_options = gdal.WarpOptions(**warp_kwargs)

        try:
            warped = gdal.Warp(output_path, dataset, options=warp_options)
        except Exception as warp_error:
            dataset = None
            cleanup(input_path, output_path)
            return jsonify({
                'error': f'RECTIFICATION FAILED: {str(warp_error)}. FILE MAY USE AN UNSUPPORTED GEOREFERENCING METHOD.'
            }), 500

        dataset = None

        if warped is None or not os.path.exists(output_path):
            cleanup(input_path, output_path)
            return jsonify({'error': 'RECTIFICATION FAILED. FILE MAY USE AN UNSUPPORTED GEOREFERENCING METHOD.'}), 500

        warped.BuildOverviews('AVERAGE', [2, 4, 8, 16, 32])
        metadata = extract_metadata(warped)
        warped = None

        cleanup(input_path)

        if not metadata:
            cleanup(output_path)
            return jsonify({'error': 'COULD NOT EXTRACT METADATA AFTER RECTIFICATION.'}), 500

        logger.info(f'Rectified: {os.path.getsize(output_path)/(1024*1024):.1f} MB')
        logger.info(f'Bounds: {metadata["minLon"]},{metadata["minLat"]} to {metadata["maxLon"]},{metadata["maxLat"]}')

        response = send_file(
            output_path,
            mimetype='image/tiff',
            as_attachment=True,
            download_name=f'rectified_{temp_id}.tif',
            max_age=0,
        )

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
        logger.error(f'GeoTIFF rectification error: {str(e)}')
        cleanup(input_path, output_path)
        return jsonify({'error': f'SERVER ERROR: {str(e)}'}), 500


# ============================================================
# RUN
# ============================================================
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)

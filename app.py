# ============================================================
# BHOOMITRA SURVEY — GeoPDF Conversion Server
# Converts GeoPDF to GeoTIFF using GDAL
# ============================================================

import os
import uuid
import logging
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from osgeo import gdal
import tempfile

# ── Setup ─────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

gdal.UseExceptions()

# Max file size: 99 MB
MAX_FILE_SIZE = 99 * 1024 * 1024

# ── Health Check ──────────────────────────────────────────
@app.route('/health', methods=['GET'])
def health():
    return jsonify({
        'status': 'ok',
        'service': 'bhoomitra-geopdf-server',
        'gdal_version': gdal.VersionInfo()
    }), 200

# ── Convert GeoPDF to GeoTIFF ────────────────────────────
@app.route('/convert', methods=['POST'])
def convert():
    try:
        # ── Validate file ─────────────────────────────────
        if 'file' not in request.files:
            return jsonify({'error': 'NO FILE PROVIDED'}), 400

        file = request.files['file']

        if file.filename == '':
            return jsonify({'error': 'NO FILE SELECTED'}), 400

        filename = file.filename.lower()
        if not filename.endswith('.pdf'):
            return jsonify({'error': 'ONLY PDF FILES ARE ACCEPTED'}), 400

        # ── Check file size ───────────────────────────────
        file.seek(0, 2)
        file_size = file.tell()
        file.seek(0)

        if file_size > MAX_FILE_SIZE:
            return jsonify({'error': 'FILE SIZE EXCEEDS 99 MB LIMIT'}), 400

        if file_size == 0:
            return jsonify({'error': 'FILE IS EMPTY'}), 400

        logger.info(f'Converting file: {file.filename}, size: {file_size} bytes')

        # ── Save uploaded PDF to temp file ────────────────
        temp_id = str(uuid.uuid4())
        temp_dir = tempfile.gettempdir()
        input_path = os.path.join(temp_dir, f'{temp_id}_input.pdf')
        output_path = os.path.join(temp_dir, f'{temp_id}_output.tif')

        file.save(input_path)

        # ── Open with GDAL ────────────────────────────────
        dataset = gdal.Open(input_path, gdal.GA_ReadOnly)
        if dataset is None:
            cleanup(input_path, output_path)
            return jsonify({'error': 'COULD NOT OPEN FILE. IS IT A VALID GEOPDF?'}), 400

        # ── Check georeference ────────────────────────────
        geotransform = dataset.GetGeoTransform()
        projection = dataset.GetProjection()

        if not projection or geotransform == (0.0, 1.0, 0.0, 0.0, 0.0, 1.0):
            dataset = None
            cleanup(input_path, output_path)
            return jsonify({
                'error': 'FILE HAS NO GEOGRAPHIC REFERENCE. PLEASE USE A GEOREFERENCED GEOPDF.'
            }), 400

        # ── Get bounding box ──────────────────────────────
        width = dataset.RasterXSize
        height = dataset.RasterYSize
        gt = geotransform

        min_x = gt[0]
        max_x = gt[0] + width * gt[1]
        max_y = gt[3]
        min_y = gt[3] + height * gt[5]

        # ── Convert to GeoTIFF ────────────────────────────
        translate_options = gdal.TranslateOptions(
            format='GTiff',
            creationOptions=[
                'COMPRESS=LZW',
                'TILED=YES',
                'BIGTIFF=IF_NEEDED'
            ]
        )

        result = gdal.Translate(output_path, dataset, options=translate_options)
        dataset = None
        result = None

        if not os.path.exists(output_path):
            cleanup(input_path, output_path)
            return jsonify({'error': 'CONVERSION FAILED'}), 500

        output_size = os.path.getsize(output_path)
        logger.info(f'Conversion successful. Output size: {output_size} bytes')
        logger.info(f'Bounds: min_x={min_x}, min_y={min_y}, max_x={max_x}, max_y={max_y}')

        # ── Clean input, send output ──────────────────────
        os.remove(input_path)

        return send_file(
            output_path,
            mimetype='image/tiff',
            as_attachment=True,
            download_name=f'converted_{temp_id}.tif',
            max_age=0
        )

    except Exception as e:
        logger.error(f'Conversion error: {str(e)}')
        return jsonify({'error': f'SERVER ERROR: {str(e)}'}), 500

# ── Cleanup helper ────────────────────────────────────────
def cleanup(*paths):
    for path in paths:
        try:
            if os.path.exists(path):
                os.remove(path)
        except:
            pass

# ── Run ───────────────────────────────────────────────────
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)

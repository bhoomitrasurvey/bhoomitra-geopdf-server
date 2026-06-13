FROM ghcr.io/osgeo/gdal:ubuntu-small-3.6.4

RUN apt-get update && apt-get install -y \
    python3-pip \
    python3-dev \
    python3-numpy \
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install --break-system-packages --no-cache-dir \
    Flask==3.0.3 \
    flask-cors==4.0.1 \
    gunicorn==22.0.0 \
    Pillow==10.3.0 \
    requests==2.32.3

WORKDIR /app

COPY . .

EXPOSE 5000

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--timeout", "300", "--workers", "1", "app:app"]

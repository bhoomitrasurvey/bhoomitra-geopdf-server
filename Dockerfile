FROM ghcr.io/osgeo/gdal:ubuntu-small-3.6.4

RUN apt-get update && apt-get install -y \
    python3-pip \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .

RUN pip3 install --break-system-packages \
    Flask==3.0.3 \
    flask-cors==4.0.1 \
    gunicorn==22.0.0 \
    numpy==1.26.4 \
    Pillow==10.3.0 \
    requests==2.32.3

COPY . .

EXPOSE 5000

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--timeout", "300", "--workers", "1", "app:app"]

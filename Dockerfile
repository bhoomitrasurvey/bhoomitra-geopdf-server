FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    gdal-bin \
    libgdal-dev \
    python3-gdal \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

ENV GDAL_VERSION=3.6.2
ENV CPLUS_INCLUDE_PATH=/usr/include/gdal
ENV C_INCLUDE_PATH=/usr/include/gdal

RUN pip install --no-cache-dir \
    Flask==3.0.3 \
    flask-cors==4.0.1 \
    gunicorn==22.0.0 \
    numpy==1.26.4 \
    Pillow==10.3.0 \
    requests==2.32.3 \
    GDAL==3.6.2

WORKDIR /app

COPY . .

EXPOSE 5000

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--timeout", "900", "--workers", "1", "app:app"]

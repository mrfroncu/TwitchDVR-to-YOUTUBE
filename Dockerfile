FROM python:3.12-slim

WORKDIR /srv

COPY requirements.txt web-requirements.txt ./
RUN pip install --no-cache-dir -r web-requirements.txt

COPY app/ app/
COPY web/ web/
COPY run_web.py VERSION ./

ENV APP_DIR=/config \
    VODS_DIR=/vods \
    PORT=4091

VOLUME ["/config", "/vods"]
EXPOSE 4091

CMD ["python", "run_web.py"]

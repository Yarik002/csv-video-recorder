# Image of the "web" service: Apache + mod_wsgi + the Flask video service.
# The "worker" service (zoom_worker.py, Internship task 2) runs from the same image.
#
# mod_wsgi must be built for the same Python that runs the app. The image has
# Python 3.12, and mod_wsgi is built for it with pip; "mod_wsgi-express module-config"
# then prints the LoadModule line for Apache. Two stages: compilers and headers stay
# in the first one, the final image gets only the ready venv.

FROM python:3.12-slim-bookworm AS build
RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc libc6-dev apache2-dev libpq-dev \
 && rm -rf /var/lib/apt/lists/*
RUN python -m venv /opt/venv
COPY requirements-docker.txt /tmp/
RUN /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements-docker.txt


FROM python:3.12-slim-bookworm
# apache2: the web server; libpq5: the PostgreSQL client library psycopg2 needs;
# curl: the healthcheck and "docker compose exec web curl http://localhost/videos";
# ffmpeg (has zoompan and libvpx-vp9): the zoom post-processing (Internship task 2).
RUN apt-get update \
 && apt-get install -y --no-install-recommends apache2 libpq5 curl ffmpeg \
 && rm -rf /var/lib/apt/lists/*

COPY --from=build /opt/venv /opt/venv
RUN /opt/venv/bin/mod_wsgi-express module-config > /etc/apache2/mods-available/wsgi.load \
 && a2enmod wsgi \
 && echo "ServerName localhost" > /etc/apache2/conf-available/servername.conf \
 && a2enconf servername \
 && a2dissite 000-default \
 && a2disconf other-vhosts-access-log \
 && ln -sf /dev/stdout /var/log/apache2/access.log \
 && ln -sf /dev/stderr /var/log/apache2/error.log

COPY apache/video-service.conf /etc/apache2/sites-available/
RUN a2ensite video-service

WORKDIR /app
COPY web_service.py web_service.wsgi apply_zoom.py zoom_worker.py ./
# The app runs as www-data and writes the videos here. A new named volume mounted
# on /app/uploads copies this owner, so the app can write to it.
RUN mkdir -p /app/uploads && chown www-data:www-data /app/uploads

COPY docker/apache-foreground.sh /usr/local/bin/
EXPOSE 80
# SIGWINCH = graceful stop: Apache finishes the requests in progress (an upload) and exits.
STOPSIGNAL SIGWINCH
CMD ["sh", "/usr/local/bin/apache-foreground.sh"]

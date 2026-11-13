FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN groupadd --gid 2000 nightwatch \
    && useradd --uid 10000 --gid nightwatch --create-home moderator \
    && for n in 1 2 3 4 5 6 7; do useradd --uid $((10000+n)) --gid nightwatch --create-home p$n; done \
    && mkdir -p /run/nightwatch /var/lib/nightwatch \
    && chown moderator:nightwatch /run/nightwatch /var/lib/nightwatch \
    && chmod 750 /run/nightwatch \
    && chmod 700 /var/lib/nightwatch
WORKDIR /app
COPY comms.py checkpoint.py game.py server.py client.py tests.py /app/
RUN chmod 644 /app/*.py
USER moderator
CMD ["python3", "server.py"]

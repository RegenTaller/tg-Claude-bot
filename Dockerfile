FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
# Бот работает не от root: если что-то пойдёт не так, процесс в контейнере не сможет навредить системе.
# UID 10001 фиксированный — на сервере папке data нужно дать этого владельца (см. docs/deploy.md).
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin bot \
    && mkdir -p /data && chown bot /data
COPY bot ./bot
USER bot
VOLUME /data
CMD ["python", "-m", "bot.main"]

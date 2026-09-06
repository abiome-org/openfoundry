# syntax=docker/dockerfile:1.7
FROM python:3.11-slim-bookworm AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
WORKDIR /src
COPY pyproject.toml README.md LICENSE requirements.build.lock requirements.runtime.lock ./
COPY factory ./factory
RUN python -m pip install --only-binary=:all: --require-hashes -r requirements.build.lock \
    && python -m pip wheel --only-binary=:all: --require-hashes \
        --wheel-dir /wheels -r requirements.runtime.lock \
    && python -m pip wheel --no-build-isolation --no-deps --wheel-dir /wheels .

FROM python:3.11-slim-bookworm AS runtime

ARG OPENFOUNDRY_UID=10001
ARG OPENFOUNDRY_GID=10001
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN groupadd --gid ${OPENFOUNDRY_GID} openfoundry \
    && useradd --uid ${OPENFOUNDRY_UID} --gid ${OPENFOUNDRY_GID} --create-home openfoundry
COPY --from=build /wheels /wheels
COPY requirements.runtime.lock /requirements.runtime.lock
RUN python -m pip install --no-index --find-links /wheels --require-hashes \
        -r /requirements.runtime.lock \
    && python -m pip install --no-index --no-deps /wheels/openfoundry-*.whl \
    && rm -rf /wheels
WORKDIR /workspace
USER openfoundry
EXPOSE 8080
ENTRYPOINT ["openfoundry"]
CMD ["--help"]

# The Shiny app of the dashboard-chat recipe as one container image.
# Build from the recipe folder, so that main/ is in the build context:
#   docker build -f ext-shiny-app/Dockerfile -t recipe-chat-app:local .
# fargate/deploy.sh does this build for you. Structure adapted from the CPSC 436C L15 querychat demo.
FROM python:3.12-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /usr/local/bin/uv
# a C compiler in the build stage only: a package without a wheel for this machine type is compiled here
RUN apt-get update && apt-get install -y --no-install-recommends build-essential && rm -rf /var/lib/apt/lists/*
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
# the recipe's packages and the app group (shiny, querychat, chatlas, pandas); no test packages
RUN uv sync --frozen --no-dev --group app --no-install-project

FROM python:3.12-slim-bookworm
RUN useradd --create-home --uid 1000 app
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY main/ main/
COPY ext-shiny-app/app.py ext-shiny-app/chatformat.py ext-shiny-app/client.py ext-shiny-app/source.py \
     ext-shiny-app/stub_gateway.py ext-shiny-app/stub_replies.json ext-shiny-app/
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1
# The shop's two tables are written into the image at build time (main/data.py), so the container needs no data
# volume and no AWS permission. With your own data: copy your Parquet files to /app/data here instead.
RUN python -c "from main.data import make_data; make_data()" && chown -R app /app/data
USER app
EXPOSE 8000
CMD ["shiny", "run", "ext-shiny-app/app.py", "--host", "0.0.0.0", "--port", "8000"]

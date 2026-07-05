FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY awgfleet ./awgfleet
RUN pip install --no-cache-dir .

# Runs the steering controller. Mount state.json and pass CF_API_TOKEN.
ENTRYPOINT ["awgfleet"]
CMD ["run"]

FROM python:3.11.10-slim@sha256:840e180ebcc6e5c8efab209c43f5e40fd2af98cb49db5c7103c90539c56bb30e
WORKDIR /app
COPY . .
ENV PYTHONDONTWRITEBYTECODE=1
CMD ["python", "-m", "analysis_app", "summary"]

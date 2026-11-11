FROM python:3.11.10-slim
WORKDIR /app
COPY . .
ENV PYTHONDONTWRITEBYTECODE=1
CMD ["python", "-m", "analysis_app", "summary"]

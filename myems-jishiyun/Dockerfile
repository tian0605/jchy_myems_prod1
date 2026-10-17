FROM python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY myems_jishiyun ./myems_jishiyun
COPY verify.py ./
USER 10001:10001
ENTRYPOINT ["python", "verify.py"]
CMD ["--help"]

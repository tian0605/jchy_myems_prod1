FROM python:3.10-slim

# RUN apt update && apt install -y nano telnet

# todo: share upload folder with admin container on Docker

RUN mkdir -p /var/www/myems-admin/upload

WORKDIR /app
COPY myems-api /app
COPY myems-jishiyun/pyproject.toml /opt/jishiyun/
COPY myems-jishiyun/myems_jishiyun /opt/jishiyun/myems_jishiyun

# use PyPI mirror to avoid slow upstream installs during image builds
RUN pip install --no-cache-dir -r requirements.txt -i http://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com
RUN pip install --no-cache-dir --no-build-isolation --no-deps /opt/jishiyun

EXPOSE 8000
CMD ["gunicorn", "app:api", "-b", "0.0.0.0:8000", "--timeout", "600", "--workers=4"]
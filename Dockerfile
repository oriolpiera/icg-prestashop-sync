FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y 
gcc 
g++ 
unixodbc-dev 
curl 
&& rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml .
COPY apps apps
COPY config config
COPY manage.py .

RUN pip install --upgrade pip
RUN pip install .

EXPOSE 8000

CMD ["gunicorn", "config.wsgi:application", "-b", "0.0.0.0:8000", "--workers=3"]


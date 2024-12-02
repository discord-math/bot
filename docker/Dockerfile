FROM python:3.9-alpine
RUN apk add --no-cache gcc g++ musl-dev git
COPY requirements_core.txt /
RUN pip install --use-pep517 --requirement /requirements_core.txt \
    && rm requirements_core.txt

WORKDIR /opt/bot/
CMD ["python", "main.py"]

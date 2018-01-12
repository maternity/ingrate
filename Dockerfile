FROM python:3.6

WORKDIR /ingrate/
ENTRYPOINT python3 ingrate.py "$0" "$@"

COPY ./ /ingrate/
RUN pip install --no-cache-dir -r /ingrate/requirements.txt

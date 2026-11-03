FROM python:3.12-slim

# This image exists only for the Linux CI harness. It deliberately contains no
# application code, model, credentials, or production configuration. The
# harness uses it to prove that the operational script reaches a real Docker
# Engine with the intended resource and networking constraints.
COPY flask /usr/local/bin/flask
RUN chmod 0755 /usr/local/bin/flask

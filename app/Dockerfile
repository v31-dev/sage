FROM python:3.14-slim

# Install dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
  curl rsync openssh-client \
  && rm -rf /var/lib/apt/lists/*

# Install Tailscale 
RUN curl -fsSL https://tailscale.com/install.sh | sh

WORKDIR /app
COPY ./requirements.txt /app/requirements.txt

RUN pip install --no-cache-dir --upgrade -r /app/requirements.txt

COPY . /app

ENV PORT=9000
ENV METRICS_PORT=9001
ENV PYTHONUNBUFFERED=1

EXPOSE 9000
EXPOSE 9001

CMD ["python", "main.py"]
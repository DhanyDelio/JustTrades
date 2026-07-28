FROM python:3.11-slim

# Set timezone to Asia/Jakarta and install some basic dependencies
ENV TZ=Asia/Jakarta
RUN apt-get update && apt-get install -y tzdata build-essential && \
    ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements first to leverage Docker cache
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application
COPY . .

# Entry point
CMD ["python", "swing_trade.py"]

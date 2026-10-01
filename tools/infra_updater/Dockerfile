# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

FROM python:3.11-slim

# Install git, ca-certificates, and curl for target repository syncing & network calls
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Allow git operations in any directory
RUN git config --global --add safe.directory "*"

WORKDIR /app/tools/infra_updater

# Copy requirements first to leverage Docker layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all application assets and code
COPY . /app/tools/infra_updater

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH="/app:/app/tools/infra_updater"
ENV UPDATER_WORKSPACE_DIR="/tmp/target_repo"
ENV PORT=8080

EXPOSE 8080

# The server runs a background poller (PR sync + Cloud Build status). On Cloud
# Run deploy with: --min-instances=1 --max-instances=1 --no-cpu-throttling
CMD ["python3", "server.py"]

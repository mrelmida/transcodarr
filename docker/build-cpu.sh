#!/bin/bash
set -e
docker build -t transcodarr:cpu -f docker/Dockerfile.cpu .

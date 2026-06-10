#!/bin/bash
set -e
docker build -t transcodarr:amd -f docker/Dockerfile.amd .

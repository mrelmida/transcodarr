#!/bin/bash
set -e
docker build -t transcodarr:intel -f docker/Dockerfile.intel .

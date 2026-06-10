#!/bin/bash
set -e
docker build -t transcodarr:nvidia -f docker/Dockerfile.nvidia .

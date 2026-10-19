#!/bin/sh
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec /bin/bash "$PROJECT_DIR/run-menubar.sh"

#!/bin/bash
# affected-tests.sh — Map changed source files to their test directories.
# Returns pytest path arguments to run only affected tests.
# Falls back to running everything if CI/infra files changed.
set -euo pipefail

# Get changed files vs master
CHANGED_FILES=$(git diff --name-only origin/master...HEAD 2>/dev/null || echo "FALLBACK")

# If we can't determine changes, run everything
if [ "$CHANGED_FILES" = "FALLBACK" ] || [ -z "$CHANGED_FILES" ]; then
    echo ""
    exit 0
fi

# Check if any "run everything" files changed
RUN_ALL_PATTERNS=(
    ".github/"
    "docker"
    "pyproject.toml"
    "uv.lock"
    "requirements"
    "pytest.ini"
    "conftest.py"
    "posthog/settings"
    "posthog/models"  # Core models affect many tests
    "common/"
)

for pattern in "${RUN_ALL_PATTERNS[@]}"; do
    if echo "$CHANGED_FILES" | grep -q "$pattern"; then
        # Infrastructure file changed — run everything
        echo ""
        exit 0
    fi
done

# Map source directories to test directories
declare -A DIR_MAP
DIR_MAP["posthog/api/"]="posthog/api/test/"
DIR_MAP["posthog/hogql/"]="posthog/hogql/test/"
DIR_MAP["posthog/hogql_queries/"]="posthog/hogql_queries/"
DIR_MAP["posthog/clickhouse/"]="posthog/clickhouse/"
DIR_MAP["posthog/batch_exports/"]="posthog/batch_exports/tests/"
DIR_MAP["posthog/tasks/"]="posthog/tasks/test/"
DIR_MAP["posthog/queries/"]="posthog/queries/test/"
DIR_MAP["posthog/caching/"]="posthog/caching/test/"
DIR_MAP["posthog/cdp/"]="posthog/cdp/test/"
DIR_MAP["posthog/warehouse/"]="posthog/warehouse/test/"
DIR_MAP["ee/api/"]="ee/api/test/"
DIR_MAP["ee/clickhouse/"]="ee/clickhouse/"
DIR_MAP["ee/hogai/"]="ee/hogai/"
DIR_MAP["ee/billing/"]="ee/billing/test/"
DIR_MAP["products/"]="products/"

# Collect unique test directories
TEST_DIRS=""
for dir in "${!DIR_MAP[@]}"; do
    if echo "$CHANGED_FILES" | grep -q "^$dir"; then
        TEST_DIRS="$TEST_DIRS ${DIR_MAP[$dir]}"
    fi
done

# Also add test files that were directly changed
CHANGED_TEST_FILES=$(echo "$CHANGED_FILES" | grep -E "test.*\.py$|tests.*\.py$" || true)
if [ -n "$CHANGED_TEST_FILES" ]; then
    TEST_DIRS="$TEST_DIRS $CHANGED_TEST_FILES"
fi

# Deduplicate and trim
TEST_DIRS=$(echo "$TEST_DIRS" | tr ' ' '\n' | sort -u | tr '\n' ' ' | xargs)

if [ -z "$TEST_DIRS" ]; then
    # No recognized source paths changed — run everything to be safe
    echo ""
    exit 0
fi

echo "$TEST_DIRS"

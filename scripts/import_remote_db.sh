#!/bin/bash

# Load the environment variables
source .env.sh

# Step 1: Create a remote database dump
# Get the current date in a specific format
current_date=$(date +"%Y-%m-%d")
DUMP_FILE="$current_date-$REMOTE_DB_NAME.sql.gz"
ssh $REMOTE_HOST "pg_dump $REMOTE_DB_NAME | gzip > $DUMP_FILE"

# Step 2: Download the dump to your local machine
scp $REMOTE_HOST:$DUMP_FILE /tmp/

# Step 3: Drop the local database (make sure to backup data if needed)
dropdb $LOCAL_DB_NAME

# Step 4: Create a new local database
createdb $LOCAL_DB_NAME

# Step 5: Import the dump into the local database
gunzip -c /tmp/$DUMP_FILE | psql summaree

# Step 6: Clean up - remove the downloaded dump file
rm /tmp/$DUMP_FILE

echo "Database dump and import completed."

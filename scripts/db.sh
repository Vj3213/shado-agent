#!/usr/bin/env bash
# Open the psql shell into the meal_agent database (Postgres must be running).
# Start Postgres if needed: LC_ALL="C" /usr/local/opt/postgresql@15/bin/postgres -D /usr/local/var/postgresql@15 &
export PATH="/usr/local/opt/postgresql@15/bin:$PATH"
exec psql -d meal_agent

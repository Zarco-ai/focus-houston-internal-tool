-- Runs once, the first time the container starts with an empty data volume.
-- focus_dev is created by POSTGRES_DB; this adds the separate test database
-- so running pytest can never touch development data.
CREATE DATABASE focus_test OWNER focus;

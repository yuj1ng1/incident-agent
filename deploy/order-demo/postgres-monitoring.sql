-- 由 setup_postgres_monitoring.py 执行，适用于已经有数据的 Demo。
-- 不修改 orders 表，不需要删除或重新初始化数据库卷。
BEGIN;
SET LOCAL log_statement = 'none';
SET LOCAL log_min_error_statement = 'panic';

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'postgres_monitor') THEN
        CREATE ROLE postgres_monitor LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
            NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 10;
    END IF;
END
$$;

GRANT pg_monitor TO postgres_monitor;
GRANT CONNECT ON DATABASE orders TO postgres_monitor;
ALTER ROLE postgres_monitor IN DATABASE orders
    SET default_transaction_read_only = 'on';
ALTER ROLE postgres_monitor IN DATABASE orders SET statement_timeout = '3000';

-- PASSWORD_SET_BY_SETUP
COMMIT;

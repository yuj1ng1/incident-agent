import socket

import psycopg


class DatabaseClient:
    def __init__(
        self,
        host,
        port,
        database,
        user,
        password
    ):
        self.host=host
        self.port=port
        self.database=database
        self.user=user
        self.password=password

    def health(self):
        result={
            "dns_resolved":False,
            "tcp_reachable":False,
            "database_connected":False,
            "query_ok":False
        }

        try:
            socket.getaddrinfo(
                self.host,
                self.port
            )

            result["dns_resolved"]=True

        except socket.gaierror as e:
            result["failure_stage"]="dns"
            result["error_type"]=type(e).__name__
            result["message"]=str(e)

            return result

        try:
            with socket.create_connection(
                (self.host,self.port),
                timeout=3
            ):
                result["tcp_reachable"]=True

        except OSError as e:
            result["failure_stage"]="tcp"
            result["error_type"]=type(e).__name__
            result["message"]=str(e)

            return result

        try:
            with psycopg.connect(
                host=self.host,
                port=self.port,
                dbname=self.database,
                user=self.user,
                password=self.password,
                connect_timeout=3
            ) as conn:

                result["database_connected"]=True

                with conn.cursor() as cursor:
                    cursor.execute("SELECT 1")
                    row=cursor.fetchone()

                    if row is not None and row[0]==1:
                        result["query_ok"]=True

        except psycopg.Error as e:
            result["failure_stage"]="database"
            result["error_type"]=type(e).__name__
            result["message"]=str(e)

            return result

        return result

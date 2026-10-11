"""Run with python -m web_api. Network binding is deliberately fixed to loopback."""


def main(argv=None):
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="Incident Agent 本地只读 API")
    parser.add_argument("--port", type=int, default=8010, help="本地监听端口（默认 8010）")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    uvicorn.run("web_api.app:app", host="127.0.0.1", port=args.port, proxy_headers=False)


if __name__ == "__main__":
    main()

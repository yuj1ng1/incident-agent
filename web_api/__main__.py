"""Run with python -m web_api. Network binding is deliberately fixed to loopback."""


def main():
    import uvicorn

    uvicorn.run("web_api.app:app", host="127.0.0.1", port=8002, proxy_headers=False)


if __name__ == "__main__":
    main()

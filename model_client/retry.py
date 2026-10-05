import time

def retry_wait(attempt):
    wait_second=2**attempt
    time.sleep(wait_second)
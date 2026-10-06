import hashlib

def generate_alert_fingerprint(alert):
    identity=(
        f"{alert.service}|"
        f"{alert.metric}"
    )
    fingerprint=hashlib.sha256(
        identity.encode("utf-8")
    ).hexdigest()
    return fingerprint
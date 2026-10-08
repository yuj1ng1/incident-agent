import hashlib
import json

def generate_alert_fingerprint(alert):
    if alert.source != "legacy":
        identity = [
            "v2",
            alert.source,
            alert.alertname,
            alert.service,
            {key: alert.labels[key] for key in sorted(alert.identity_labels)},
        ]
        return hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    return legacy_fingerprint(alert)


def legacy_fingerprint(alert):
    identity=(
        f"{alert.service}|"
        f"{alert.metric}"
    )
    fingerprint=hashlib.sha256(
        identity.encode("utf-8")
    ).hexdigest()
    return fingerprint

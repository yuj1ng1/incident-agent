from models.incident import Alert

def parse_prometheus_alert(raw_alert):
    if raw_alert.get("state")!="firing":
        return None

    labels=raw_alert.get("labels",{})
    annotations=raw_alert.get("annotations",{})

    return Alert(
        service=labels["service"],
        metric=labels["metric"],
        value=float(raw_alert["value"]),
        threshold=float(annotations["threshold"]),
        severity=labels["severity"]
    )
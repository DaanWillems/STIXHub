def merge_entities_with_priority(lower_indicator: dict, higher_indicator: dict) -> dict:
    sdo = {}

    for k, v in lower_indicator.items():
        if k == "extensions":
            continue
        if type(v) is list:
            try:
                sdo[k] = list(set(higher_indicator[k]) | set(v))
            finally:
                continue

        if k not in higher_indicator:
            sdo[k] = v
        else:
            sdo[k] = higher_indicator[k]

    return sdo

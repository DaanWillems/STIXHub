import argparse
import random
import time
from datetime import datetime

import httpx
from faker import Faker
from stix2 import Bundle, Identity, Indicator, IPv4Address, DomainName, URL, Relationship

fake = Faker()

LABELS = [
    "threat-actor",
    "malware",
    "vulnerability",
    "attack-pattern",
    "tool",
    "report",
    "campaign",
    "course-of-action",
    "identity",
    "indicator",
    "infrastructure",
    "location",
    "malware-analysis",
    "note",
    "observed-data",
    "opinion",
    "sighting",
    "threat-report",
]

RELATIONSHIP_TYPES = ["related-to", "indicates", "attributed-to", "targets", "uses"]

IDENTITY_CLASSES = ["individual", "group", "system", "organization", "class", "unknown"]
SECTORS = [
    "agriculture",
    "aerospace",
    "automotive",
    "communications",
    "construction",
    "defence",
    "education",
    "energy",
    "entertainment",
    "financial-services",
    "government",
    "healthcare",
    "hospitality",
    "information-technology",
    "manufacturing",
    "mining",
    "pharmaceuticals",
    "retail",
    "technology",
    "transportation",
    "utilities",
]

TLP_MARKINGS = [
    "marking-definition--94868c89-83c2-464b-929b-a1a8aa3c8487",  # TLP:CLEAR
    "marking-definition--bab4a63c-aed9-4cf5-a766-dfca5abac2bb",  # TLP:GREEN
    "marking-definition--34098fce-860f-48ae-8e50-ebd3cc5e41da",  # TLP:AMBER
    "marking-definition--939a9414-2ddd-4d32-a0cd-375ea402b003",  # TLP:AMBER+STRICT
    "marking-definition--e828b379-4e03-4974-9ac4-e53a884c97c1",  # TLP:RED
]

current_marking_index = 0


def _generate_identity_name(identity_class: str) -> str:
    if identity_class == "individual":
        return fake.name()
    elif identity_class == "organization":
        return fake.company()
    elif identity_class == "system":
        return f"{fake.word().capitalize()} {fake.word().capitalize()} System"
    elif identity_class == "group":
        return f"{fake.word().capitalize()} {fake.word().capitalize()} Group"
    elif identity_class == "class":
        return f"{fake.word().capitalize()} Class"
    else:  # unknown
        return f"Unknown {fake.word().capitalize()} Identity"


def create_random_stix_object(common_args):
    """Create a random STIX object."""
    object_type = random.choice(
        ["indicator", "ipv4-addr", "domain-name", "url", "identity"]
    )
    labels = random.choices(LABELS, k=random.randint(1, 3))

    if object_type == "indicator":
        pattern_type = random.choice(["ipv4-addr", "domain-name", "url"])
        if pattern_type == "ipv4-addr":
            value = fake.ipv4()
            pattern = f"[ipv4-addr:value = '{value}']"
        elif pattern_type == "domain-name":
            value = fake.domain_name()
            pattern = f"[domain-name:value = '{value}']"
        else:
            value = fake.url()
            pattern = f"[url:value = '{value}']"

        return Indicator(
            pattern_type="stix",
            pattern=pattern,
            valid_from=datetime.now(),
            labels=labels,
            **common_args,
        )

    if object_type == "ipv4-addr":
        return IPv4Address(value=fake.ipv4(), **common_args)
    if object_type == "domain-name":
        return DomainName(value=fake.domain_name(), **common_args)
    if object_type == "url":
        return URL(value=fake.url(), **common_args)
    if object_type == "identity":
        identity_class = random.choice(IDENTITY_CLASSES)
        sectors = random.choices(SECTORS, k=random.randint(1, 3))
        name = _generate_identity_name(identity_class)
        return Identity(
            name=name,
            identity_class=identity_class,
            sectors=sectors,
            contact_information=fake.email(),
            description=fake.sentence(),
            labels=labels,
            **common_args,
        )


def create_random_relationship(source, target, common_args):
    """Create a random relationship between two STIX objects."""
    relationship_type = random.choice(RELATIONSHIP_TYPES)
    return Relationship(
        source_ref=source.id,
        target_ref=target.id,
        relationship_type=relationship_type,
        **common_args,
    )


def create_random_bundle(batch_size, recent_objects, max_recent_objects):
    """Create a bundle containing a batch of random STIX objects and relationships."""
    global current_marking_index

    bundle_objects = []
    for _ in range(batch_size):
        marking_stix_id = TLP_MARKINGS[current_marking_index]
        current_marking_index = (current_marking_index + 1) % len(TLP_MARKINGS)
        common_args = {"object_marking_refs": [marking_stix_id]}

        stix_object = create_random_stix_object(common_args)
        if stix_object:
            bundle_objects.append(stix_object)
            recent_objects.append(stix_object)
            if len(recent_objects) > max_recent_objects:
                recent_objects.pop(0)

        if len(recent_objects) >= 2 and random.random() < 0.5:
            source, target = random.sample(recent_objects, 2)
            relationship = create_random_relationship(source, target, common_args)
            bundle_objects.append(relationship)

    if not bundle_objects:
        return None
    return Bundle(*bundle_objects, allow_custom=True)


def send_bundle(client: httpx.Client, objects_url: str, bundle: Bundle) -> None:
    """POST a STIX bundle to a TAXII 2.1 collection's objects endpoint."""
    response = client.post(objects_url, content=bundle.serialize())
    response.raise_for_status()
    status = response.json()
    print(
        f"Sent bundle {bundle.id} ({len(bundle.objects)} objects) -> "
        f"{status.get('status')} "
        f"(success={status.get('success_count')}, failure={status.get('failure_count')})"
    )


def main():
    """Main function to generate STIX bundles and push them to a TAXII collection."""
    parser = argparse.ArgumentParser(description="Mock STIX connector.")
    parser.add_argument(
        "--interval",
        type=int,
        default=5000,
        help="Interval in milliseconds between bundle submissions.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=5,
        help="Number of STIX objects to generate per bundle.",
    )
    parser.add_argument(
        "--taxii-root",
        default="http://localhost:8000/taxii2/root",
        help="Base URL of the TAXII2 API root the collection lives under.",
    )
    parser.add_argument(
        "--collection-id",
        required=True,
        help="ID of the (write-enabled) TAXII collection to push bundles to.",
    )
    parser.add_argument(
        "--api-key",
        required=True,
        help="Bearer API key of a user with write access to the collection.",
    )
    args = parser.parse_args()

    objects_url = f"{args.taxii_root.rstrip('/')}/collections/{args.collection_id}/objects/"
    headers = {
        "Authorization": f"Bearer {args.api_key}",
        "Content-Type": "application/taxii+json;version=2.1",
        "Accept": "application/taxii+json;version=2.1",
    }

    recent_objects = []
    max_recent_objects = 20

    with httpx.Client(headers=headers, timeout=10.0) as client:
        try:
            while True:
                bundle = create_random_bundle(
                    args.batch_size, recent_objects, max_recent_objects
                )
                if bundle is not None:
                    try:
                        send_bundle(client, objects_url, bundle)
                    except httpx.HTTPError as exc:
                        print(f"Failed to send bundle to {objects_url}: {exc}")

                time.sleep(args.interval / 1000)
        except KeyboardInterrupt:
            print("Stopping connector.")


if __name__ == "__main__":
    main()

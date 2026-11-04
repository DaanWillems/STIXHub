import uuid as uuid_module
from datetime import datetime
from typing import Optional
from sqlalchemy.ext.mutable import MutableDict

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    String,
    UniqueConstraint,
    UUID as SAUUID,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql import func

from enum import Enum
from models.domain import BucketMode, MispEventOrigin, ProcessingStatus


class StixType(Enum):
    # --- SDOs ---
    AttackPattern = "attack-pattern"
    Campaign = "campaign"
    CourseOfAction = "course-of-action"
    Grouping = "grouping"
    Identity = "identity"
    Indicator = "indicator"
    Infrastructure = "infrastructure"
    IntrusionSet = "intrusion-set"
    Location = "location"
    Malware = "malware"
    MalwareAnalysis = "malware-analysis"
    Note = "note"
    ObservedData = "observed-data"
    Opinion = "opinion"
    Report = "report"
    ThreatActor = "threat-actor"
    Tool = "tool"
    Vulnerability = "vulnerability"

    # --- SCOs ---
    Artifact = "artifact"
    AutonomousSystem = "autonomous-system"
    Directory = "directory"
    DomainName = "domain-name"
    EmailAddr = "email-addr"
    EmailMessage = "email-message"
    File = "file"
    IPV4Addr = "ipv4-addr"
    IPV6Addr = "ipv6-addr"
    MACAddr = "mac-addr"
    Mutex = "mutex"
    NetworkTraffic = "network-traffic"
    Process = "process"
    Software = "software"
    URL = "url"
    UserAccount = "user-account"
    WindowsRegistryKey = "windows-registry-key"
    X509Certificate = "x509-certificate"

    # --- SMOs ---
    MarkingDefinition = "marking-definition"

    # --- SROs ---
    Relationship = "relationship"
    Sighting = "sighting"

    # Custom ``x-*`` types (e.g. MISP's x-misp-attribute) are stored as well,
    # but are not listed here.

    # --- Platform-internal meta types ---
    Object = "object"
    Observable = "observable"
    Entity = "entity"


SDO_TYPES = [
    StixType.AttackPattern,
    StixType.Campaign,
    StixType.CourseOfAction,
    StixType.Grouping,
    StixType.Identity,
    StixType.Indicator,
    StixType.Infrastructure,
    StixType.IntrusionSet,
    StixType.Location,
    StixType.Malware,
    StixType.MalwareAnalysis,
    StixType.Note,
    StixType.ObservedData,
    StixType.Opinion,
    StixType.Report,
    StixType.ThreatActor,
    StixType.Tool,
    StixType.Vulnerability,
]

SCO_TYPES = [
    StixType.Artifact,
    StixType.AutonomousSystem,
    StixType.Directory,
    StixType.DomainName,
    StixType.EmailAddr,
    StixType.EmailMessage,
    StixType.File,
    StixType.IPV4Addr,
    StixType.IPV6Addr,
    StixType.MACAddr,
    StixType.Mutex,
    StixType.NetworkTraffic,
    StixType.Process,
    StixType.Software,
    StixType.URL,
    StixType.UserAccount,
    StixType.WindowsRegistryKey,
    StixType.X509Certificate,
]


class Base(DeclarativeBase):
    """Base is a class to define base for our models"""


class BucketModel(Base):
    __tablename__ = "bucket_model"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(nullable=False)
    mode: Mapped[str] = mapped_column(
        SAEnum(
            BucketMode,
            values_callable=lambda x: [e.value for e in x],
            name="bucket_mode",
        ),
        nullable=False,
        default=BucketMode.append.value,
        server_default=BucketMode.append.value,
    )


class StixEntityModel(Base):
    __tablename__ = "stix_entity_model"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    bucket_id: Mapped[int] = mapped_column(ForeignKey("bucket_model.id"))
    stix_id: Mapped[str]
    # Wide enough for custom type names such as x-misp-galaxy-cluster.
    type: Mapped[str] = mapped_column(String(128))
    spec_version: Mapped[str] = mapped_column(String(30))
    creator: Mapped[str] = mapped_column(nullable=False)
    value: Mapped[str] = mapped_column(nullable=False)
    platform_modified: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    platform_created: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    status: Mapped[str] = mapped_column(
        SAEnum(
            ProcessingStatus,
            values_callable=lambda x: [e.value for e in x],
            name="processing_status",
        ),
        default=ProcessingStatus.pending.value,
        server_default=ProcessingStatus.pending.value,
        nullable=False,
    )
    object = mapped_column(MutableDict.as_mutable(JSONB))
    other_stix_ids: Mapped[Optional[list[str]]] = mapped_column(
        JSONB, nullable=True, default=None
    )
    # Name of the last pipeline that wrote this entity; NULL when ingested directly.
    pipeline: Mapped[Optional[str]] = mapped_column(nullable=True, default=None)
    # When the row was written, by the database's clock (not the transaction
    # start like now()): readers use it to tell rows they may safely pass.
    inserted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp(), nullable=False
    )


class CollectorModel(Base):
    __tablename__ = "collector_model"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(unique=True, nullable=False)
    bucket_id: Mapped[int] = mapped_column(
        ForeignKey("bucket_model.id"), nullable=False
    )
    cursor: Mapped[Optional[str]] = mapped_column(nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class MispEventSyncModel(Base):
    """Which events of a MISP instance STIXHub has seen, and who owns them."""

    __tablename__ = "misp_event_sync"
    __table_args__ = (UniqueConstraint("instance", "event_uuid"),)
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # The MISP instance's normalised URL, shared by all its connectors.
    instance: Mapped[str] = mapped_column(nullable=False)
    event_uuid: Mapped[str] = mapped_column(nullable=False)
    origin: Mapped[str] = mapped_column(
        SAEnum(
            MispEventOrigin,
            values_callable=lambda x: [e.value for e in x],
            name="misp_event_origin",
        ),
        nullable=False,
    )
    misp_timestamp: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    creator_org: Mapped[Optional[str]] = mapped_column(nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class UserModel(Base):
    __tablename__ = "user_model"
    id: Mapped[uuid_module.UUID] = mapped_column(
        SAUUID(as_uuid=True), primary_key=True, default=uuid_module.uuid4
    )
    email: Mapped[str] = mapped_column(unique=True, nullable=False)
    roles: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    api_key_hash: Mapped[str] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

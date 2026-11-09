"""Local public-packet sources for reviewed observation memory.

No benchmark answer, model prediction or arbitrary supplied path is read. The
existing packet/audit validator establishes exact frozen-summary lineage; these
are compact observations with omissions, not complete original telemetry.
"""
from __future__ import annotations
import copy
import hashlib
import json
from pathlib import Path
import re

from .contracts import ROOT


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


class PublicEpisodeSources:
    def __init__(self, root=ROOT, group_map=None):
        self.root = Path(root).resolve()
        self.packet_manifest = self.root / "verification/full-line-packets-20261103-r1/manifest.json"
        self.view_manifest = self.root / "verification/full-line-common-views-20261103/manifest.json"
        self.data_manifest = self.root / "data/full-re2ob-analysis/manifest.json"
        self.groups = dict(group_map or {})
        self._manifest_hashes = dict(zip((self.packet_manifest, self.view_manifest, self.data_manifest), (
            "884c5d103baae3ec0b29bd5d96d2871c1276ab8b20852ecf42882f53fea0b2b7",
            "b528dfff2bc85192d0b160002c2cfd5cfc60d2da0d2f37b02162fc70d5d2697b",
            "61c6dd7e6ff4e482c593b7891a8a402bd57a9788cba8940727e593b5e5f42640")))
        if any(hashlib.sha256(path.read_bytes()).hexdigest() != expected for path, expected in self._manifest_hashes.items()):
            raise ValueError("Public source manifest differs from the audited frozen catalog")
        self.view_entries = {entry["case_id"]: entry for entry in json.loads(self.view_manifest.read_bytes())["cases"]}
        self.data_revision = json.loads(self.data_manifest.read_bytes())["data_revision"]

    def load_case(self, case_id):
        if not isinstance(case_id, str) or not re.fullmatch(r"incident-[0-9]{4}", case_id) or case_id not in self.view_entries:
            raise ValueError("Public memory source is outside the fixed neutral case catalog")
        for path, expected in self._manifest_hashes.items():
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError("Public source manifest changed after qualification")
        # Reuse the source-qualified reader: fixed packet path, original SHA,
        # stream-resolved audit summary, exact reversible compact representation.
        from evaluation.w08_model_view import load_packet_view, expand_view
        bundle = load_packet_view(self.packet_manifest, case_id)
        view = bundle["view"]
        entry = self.view_entries[case_id]
        view_path = self.view_manifest.parent / (case_id + ".json")
        if (entry["path"] != view_path.name or hashlib.sha256(view_path.read_bytes()).hexdigest() != entry["sha256"]
                or encoded(view) != encoded(json.loads(view_path.read_bytes()))
                or entry["source_packet_sha256"] != bundle["source_packet_sha256"]
                or entry["source_audit_sha256"] != bundle["source_audit_sha256"]):
            raise ValueError("Public compact view and independently checked packet/audit differ")
        packet = expand_view(view)
        for modality in ("metrics", "logs", "traces"):
            lookup = view["lookup"][modality]
            if lookup is None:
                continue
            source = lookup["source"]
            expected_relative = case_id + "/" + modality + ".parquet"
            if source["path"] != expected_relative:
                raise ValueError("Public source locator differs from the fixed neutral layout")
            original = (self.root / "data/full-re2ob-analysis" / expected_relative).resolve(strict=True)
            if not original.is_relative_to((self.root / "data/full-re2ob-analysis").resolve()):
                raise ValueError("Public source locator escapes its declared corpus")
            sha = hashlib.sha256()
            with original.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    sha.update(chunk)
            if sha.hexdigest() != source["sha256"] or original.stat().st_size != source["bytes"]:
                raise ValueError("Original public telemetry does not match the admitted observation lineage")
        return bundle, packet

    def __call__(self, episode):
        source_hint = episode.get("source", {}) if isinstance(episode, dict) else {}
        if source_hint.get("kind") != "public_packet":
            raise ValueError("This loader only verifies fixed local public packets")
        case_id = source_hint.get("incident_id")
        bundle, packet = self.load_case(case_id)
        facts, references, digests = [], [], set()
        for modality in ("metrics", "logs", "traces"):
            for row in packet[modality]:
                record = copy.deepcopy({key: value for key, value in row.items() if key not in {"evidence_id", "source", "modality"}})
                service = row.get("service", row.get("child_service"))
                if not isinstance(service, str) or not service:
                    raise ValueError("A public source observation has no explicit service")
                artifact = row["source"]["sha256"]
                locator = "audit-summary:" + row["evidence_id"]
                fact = {"modality": modality, "service_id": service,
                        "evidence_id": row["evidence_id"], "record": record,
                        "unit": row.get("unit", row.get("duration_unit", "unknown")),
                        "source_artifact_digest": artifact, "record_locator": locator}
                facts.append(fact)
                references.append({"evidence_id": row["evidence_id"], "source_artifact_digest": artifact,
                                   "record_locator": locator, "content_digest": hashlib.sha256(encoded(record)).hexdigest()})
                digests.add(artifact)
        digests.update((bundle["source_packet_sha256"], bundle["source_audit_sha256"], bundle["source_manifest_sha256"], self.view_entries[case_id]["sha256"]))
        return {"source": {"kind": "public_packet", "investigation_id": None, "run_id": None,
                           "incident_id": case_id, "data_revision": self.data_revision,
                           "group_id": self.groups.get(case_id), "source_artifact_digests": sorted(digests),
                           "evidence_refs": references},
                "scope": {"system_id": "rcaeval_re2ob", "service_ids": sorted({f["service_id"] for f in facts}), "data_revision": self.data_revision},
                "facts": facts}

    def probe_context(self, case_id):
        """Read-only compact current evidence for paired trials, no private gold."""
        bundle, packet = self.load_case(case_id)
        return {"incident_id": case_id, "view": bundle["view"], "evidence_ids": bundle["selected_evidence_ids"],
                "service_ids": sorted(packet["coverage"]), "time_window": packet["time_window"],
                "current_source_artifact_digests": sorted({r["source"]["sha256"] for m in ("metrics", "logs", "traces") for r in packet[m]}),
                "source_packet_sha256": bundle["source_packet_sha256"], "source_audit_sha256": bundle["source_audit_sha256"]}


def fixed_seed_cases(root=ROOT):
    path = Path(root)/"verification/growing-rag-preparation-20261105-r1/data-plan.json"
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != "9d1fa75ec05bb86eb6a451ddeeb6fb2dbccede58c3682669cf43902006d7f5bb":
        raise ValueError("Public bootstrap plan differs from the reviewed seed/query split")
    plan = json.loads(raw)
    cases = [entry["case_id"] for entry in plan["seed_cases"]]
    if len(cases) != 24 or len(set(cases)) != 24 or set(cases) & {q["case_id"] for q in plan["query_cases"]}:
        raise ValueError("Public bootstrap cases overlap query data or have invalid cardinality")
    return cases

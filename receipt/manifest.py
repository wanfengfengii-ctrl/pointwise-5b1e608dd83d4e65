from dataclasses import asdict
from hashlib import sha256
import json

from candidate.snapshot import canonical_json, encode_snapshot
from inspection.receipts import encode_check_receipt


def build_manifest(issue):
    receipt = asdict(issue.receipt)
    report = json.loads(encode_snapshot(issue.report))
    if issue.span_evidence is None:
        del receipt['span_evidence_digest']
        return {'version': 1, 'report': report, 'receipt': receipt}
    return {
        'version': 2,
        'report': report,
        'receipt': receipt,
        'span_evidence': json.loads(encode_check_receipt(issue.span_evidence)),
    }


def encode_manifest(manifest):
    return canonical_json(manifest)


def package_digest(manifest):
    return sha256(encode_manifest(manifest).encode('utf-8')).hexdigest()

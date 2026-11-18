from candidate.snapshot import canonical_json, decode_snapshot, freeze_snapshot, snapshot_digest
from inspection.receipts import check_receipt_digest, decode_check_receipt, evaluate_check
from publication.ledger import IssueReceipt, issue_number
from receipt.manifest import encode_manifest, package_digest
from source.registry import require_name


def verify_provenance(snapshot):
    try:
        checked = freeze_snapshot(
            snapshot.candidate_id, snapshot.station, snapshot.day, snapshot.revision_id, snapshot.rows,
            corrects=snapshot.corrects,
        )
        return checked == snapshot
    except (AttributeError, TypeError, ValueError):
        return False


def verify_span_evidence(manifest, report, receipt):
    evidence = decode_check_receipt(canonical_json(manifest['span_evidence']))
    return (evidence.kind == 'span'
            and evidence.standard.low_trial is not None
            and evidence.passed
            and evidence.receipt_id == receipt.span_receipt_id
            and evidence.candidate_id == report.candidate_id
            and evidence.input_digest == snapshot_digest(report)
            and receipt.span_evidence_digest == check_receipt_digest(evidence)
            and evaluate_check(report, evidence.standard) == evidence.finding)


def verify_package(package):
    try:
        if not isinstance(package, dict) or set(package) != {'manifest', 'digest'}:
            return False
        manifest = package['manifest']
        if not isinstance(manifest, dict):
            return False
        version = manifest.get('version')
        if type(version) is not int or version not in (1, 2):
            return False
        expected = {'version', 'report', 'receipt'} | ({'span_evidence'} if version == 2 else set())
        if set(manifest) != expected:
            return False
        if package_digest(manifest) != package['digest']:
            return False
        report = decode_snapshot(encode_manifest(manifest['report']))
        receipt = IssueReceipt(**manifest['receipt'])
        evidence_ids = (require_name(receipt.zero_receipt_id), require_name(receipt.span_receipt_id))
        if not (receipt.issue_id == issue_number(report.candidate_id)
                and receipt.candidate_id == report.candidate_id
                and receipt.input_digest == snapshot_digest(report)
                and evidence_ids[0] != evidence_ids[1]
                and verify_provenance(report)):
            return False
        if version == 1:
            return receipt.span_evidence_digest is None
        return verify_span_evidence(manifest, report, receipt)
    except (KeyError, TypeError, ValueError):
        return False

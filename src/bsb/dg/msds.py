"""SDS section 9/14 parser for the dangerous goods module (Phase 1).

OR26BZQN0001 contains no DG categories, so this does not block Phase 0.
The PG-vs-flash-point validation table itself lives in validate/guide.py and
is tested now.
"""


def parse_sds(pdf_path: str) -> dict:
    raise NotImplementedError("Phase 1: SDS parser not implemented yet")

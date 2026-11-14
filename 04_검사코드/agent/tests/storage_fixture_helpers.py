"""Fixture-only adapters for asserting logical behavior across storage versions."""
from pathlib import Path
import record_store as R
import storage_objects as O


def replace_committed_fixture(path,value):
    """Reissue a synthetic logical fixture; corruption tests must use raw writes."""
    path=Path(path)
    path.unlink()
    R.commit_path(path).unlink(missing_ok=True)
    R.publish(path,value,kind='synthetic_test_fixture')


def full_judgments(server,**filters):
    result=server.get_ai_judgments(**filters)
    result['judgments']=[server.get_judgment_detail(row['judgment_id'])['item'] for row in result['judgments']]
    return result

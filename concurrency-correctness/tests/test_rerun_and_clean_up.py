"""A run after a clean-up or over kept tables, and a clean-up that meets a refusal. Offline: moto, with the answers
moto does not give (a table still being deleted, a refused call) put in by hand."""

import boto3
from moto import mock_aws

import clean_up
import tips
import trips

REGION, PREFIX = "ca-central-1", "recipe-test"
TABLES = [f"{PREFIX}-race-requests", f"{PREFIX}-race-trips"]


def refuse(client, operation, code, times=1):
    """Make the next `times` calls of client.<operation> fail with this error code, then behave as before."""
    real, left = getattr(client, operation), [times]

    def call(**kw):
        if left[0] > 0:
            left[0] -= 1
            raise client.exceptions.from_code(code)({"Error": {"Code": code, "Message": "put in by the test"}},
                                                    operation)
        return real(**kw)
    setattr(client, operation, call)


class OneSession:
    def __init__(self):
        self.ddb = boto3.client("dynamodb", region_name=REGION)

    def client(self, name):
        return self.ddb


def create_both(ddb):
    trips.create_table(ddb, trips.table_name(PREFIX), poll_s=0)
    tips.create_requests_table(ddb, tips.requests_table_name(PREFIX), poll_s=0)


@mock_aws
def test_clean_up_leaves_nothing_and_a_run_after_it_works():
    s = OneSession()
    assert s.ddb.list_tables()["TableNames"] == []
    create_both(s.ddb)
    assert sorted(s.ddb.list_tables()["TableNames"]) == TABLES
    steps = clean_up.clean(s, PREFIX)
    assert steps.failed == [] and len(steps.removed) == 2
    assert s.ddb.list_tables()["TableNames"] == []
    create_both(s.ddb)                                       # a run right after the clean-up
    assert clean_up.clean(s, PREFIX).failed == []
    again = clean_up.clean(s, PREFIX)                        # and a second clean-up finds nothing
    assert again.removed == [] and again.failed == [] and len(again.absent) == 2


@mock_aws
def test_a_second_run_over_kept_tables_works():
    """DynamoDB refuses to enable time to live twice (moto does not): the second call must not be made."""
    ddb = boto3.client("dynamodb", region_name=REGION)
    create_both(ddb)
    refuse(ddb, "update_time_to_live", "ValidationException", times=99)
    create_both(ddb)
    assert sorted(ddb.list_tables()["TableNames"]) == TABLES
    assert ddb.describe_time_to_live(TableName=TABLES[0])["TimeToLiveDescription"]["TimeToLiveStatus"] == "ENABLED"


@mock_aws
def test_a_table_that_is_still_being_deleted_is_waited_out_and_created_again():
    for create, name in ((trips.create_table, TABLES[1]), (tips.create_requests_table, TABLES[0])):
        ddb = boto3.client("dynamodb", region_name=REGION)
        refuse(ddb, "create_table", "ResourceInUseException")
        real_describe, real_waiter, waited = ddb.describe_table, ddb.get_waiter, []
        ddb.describe_table = lambda **kw: real_describe(**kw) if waited else {"Table": {"TableStatus": "DELETING"}}

        class Gone:
            def wait(self, **kw):
                waited.append(kw["TableName"])
        ddb.get_waiter = lambda n: Gone() if n == "table_not_exists" else real_waiter(n)
        create(ddb, name, poll_s=0)
        assert waited == [name] and name in ddb.list_tables()["TableNames"]


@mock_aws
def test_a_refused_delete_is_reported_and_the_other_table_is_still_removed(capsys):
    s = OneSession()
    create_both(s.ddb)
    refuse(s.ddb, "delete_table", "AccessDeniedException", times=1)
    steps = clean_up.clean(s, PREFIX)
    assert len(steps.failed) == 1 and steps.failed[0].startswith(f"table {TABLES[1]}: AccessDenied")
    assert s.ddb.list_tables()["TableNames"] == [TABLES[1]]           # the second table went all the same
    out = capsys.readouterr().out
    assert "FAILED" in out and "NOT complete" in out


@mock_aws
def test_a_table_that_is_already_being_deleted_is_not_deleted_again():
    s = OneSession()
    real_waiter, waited = s.ddb.get_waiter, []
    s.ddb.describe_table = lambda **kw: {"Table": {"TableStatus": "DELETING"}}
    # DeleteTable on a table in DELETING returns no error (DynamoDB API reference, DeleteTable)
    s.ddb.delete_table = lambda **kw: {"TableDescription": {"TableName": kw["TableName"], "TableStatus": "DELETING"}}

    class Gone:
        def wait(self, **kw):
            waited.append(kw["TableName"])
    s.ddb.get_waiter = lambda n: Gone() if n == "table_not_exists" else real_waiter(n)
    steps = clean_up.clean(s, PREFIX)
    assert steps.failed == [] and sorted(waited) == TABLES

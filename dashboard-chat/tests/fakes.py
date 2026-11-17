"""Stand-ins for AWS behaviour that moto does not play. Used by tests/ and by features/ (behave).

Each stand-in says which AWS page its behaviour is taken from, with the date it was read. A stand-in written from
memory can teach the code the wrong thing: check the page before you change one.
"""

from __future__ import annotations


class StillDeleting:
    """A DynamoDB client where the named tables are being deleted, as in the time right after DeleteTable.

    moto deletes a table at once, so the DELETING state is played here; every other call goes to moto. What the
    DeleteTable page of the API reference says about a table in this state (read 2026-11-13,
    https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_DeleteTable.html):
    - DescribeTable shows TableStatus DELETING until the deletion is complete;
    - a second DeleteTable returns no error (ResourceInUseException is for CREATING and UPDATING);
    - reads and writes might still be accepted until the deletion is complete.
    """

    def __init__(self, real, deleting):
        self.real, self.deleting, self.waited, self.deletes = real, set(deleting), [], []
        self.exceptions = real.exceptions

    def describe_table(self, TableName):
        if TableName in self.deleting:
            return {"Table": {"TableName": TableName, "TableStatus": "DELETING"}}
        return self.real.describe_table(TableName=TableName)

    def delete_table(self, TableName):
        self.deletes.append(TableName)
        if TableName in self.deleting:  # already being deleted: no error, and nothing new happens
            return {"TableDescription": {"TableName": TableName, "TableStatus": "DELETING"}}
        return self.real.delete_table(TableName=TableName)

    def get_waiter(self, name):
        outer = self

        class Waiter:
            def wait(self, TableName):
                outer.waited.append((name, TableName))
                outer.deleting.discard(TableName)  # the delete finished while we waited

        return Waiter() if name == "table_not_exists" else self.real.get_waiter(name)

    def __getattr__(self, name):
        return getattr(self.real, name)

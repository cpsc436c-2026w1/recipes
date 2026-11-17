"""The ownership check reads tags with ListTags: GetFunction leaves them out for a caller without lambda:ListTags."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "main"))
from limits import RECIPE_TAG, function_is_ours  # noqa: E402


class _Lambda:
    """GetFunction as a caller without lambda:ListTags sees it: no Tags, a TagsError instead."""

    def __init__(self, tags):
        self.tags = tags

    def get_function(self, FunctionName):
        return {"Configuration": {"FunctionArn": f"arn:aws:lambda:ca-central-1:111122223333:function:{FunctionName}"},
                "TagsError": {"ErrorCode": "AccessDeniedException", "Message": "not allowed"}}

    def list_tags(self, Resource):
        return {"Tags": self.tags}


def test_own_function_is_ours_when_get_function_leaves_tags_out():
    assert function_is_ours(_Lambda({RECIPE_TAG["Key"]: RECIPE_TAG["Value"]}), "recipe-app")


def test_untagged_function_is_not_ours():
    assert not function_is_ours(_Lambda({}), "recipe-app")

"""Steps of features/vector_search.feature that the other features do not need."""

from behave import given


@given("a course token and a gateway address are set")
def step_token(context):
    # made-up values: the scenarios that use this step run with DRY=1, which does not call the gateway
    context.env |= {"KB_MCP_TOKEN": "test-token", "COURSE_GATEWAY_URL": "http://127.0.0.1:9/v1"}


@given('WAIT_MINUTES is "{value}"')
def step_wait(context, value):
    context.env["WAIT_MINUTES"] = value
